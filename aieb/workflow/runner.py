"""WorkflowRunner：对外唯一入口（RPC / CLI / 测试 / 基准都走这里）。

为什么要有一个"专用事件循环线程"
--------------------------------
节点是 `async def`（要 await LLM），而 `SqliteSaver` 不支持异步、`AsyncSqliteSaver`
又绑定在创建它的那个事件循环上。既要在 asyncio 服务里用，又要在同步测试/基准里用，
最稳的做法是：**一个专用 loop 线程 + `run_coroutine_threadsafe`**。

好处不只是兼容：

* 同一个 `thread_id` 的图执行天然串行，避免同进程内两次 resume 同时跑同一张图；
* aiosqlite 连接只在这个 loop 里被访问，不存在跨线程共享连接的问题。

三条硬约束：

1. **状态只从 Checkpoint 读**：`status()` 不依赖进程内存，重启后照样能查到"卡在哪个
   节点、等谁审批、草稿哪一版"。
2. **重复恢复不会重复产生副作用**：副作用节点自带幂等键，Runner 只负责判断"是否还有
   待恢复的中断"。
3. **多进程/多实例不在本阶段承诺范围内**：这里只保证单进程内的串行与幂等。
"""

from __future__ import annotations

import asyncio
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Coroutine

import aiosqlite
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.types import Command

from aieb.brain.chunkkey import content_hash
from aieb.workflow.checkpointer import checkpoints_path, thread_config
from aieb.workflow.deps import WorkflowDeps
from aieb.workflow.graph import NODE_LABELS, build_graph
from aieb.workflow.persistence import WorkflowStore, effect_key
from aieb.workflow.state import ApprovalRequest, ResumeCommand, WorkflowSnapshot


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class WorkflowError(RuntimeError):
    """工作流层面的可读错误（RPC 会把它翻译成用户提示）。"""


class WorkflowRunner:
    def __init__(
        self,
        *,
        deps: WorkflowDeps,
        workspace: Path | str | None = None,
        retry_max_attempts: int = 2,
        checkpointer_path: Path | str | None = None,
        call_timeout: float = 300.0,
    ) -> None:
        self.deps = deps
        self.workspace = Path(workspace or deps.workspace)
        self.call_timeout = float(call_timeout)
        self.retry_max_attempts = int(retry_max_attempts)
        self._closed = False
        self._db_path = (
            Path(checkpointer_path) if checkpointer_path else checkpoints_path(self.workspace)
        )
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, name="workflow-loop", daemon=True)
        self._thread.start()
        self._conn: aiosqlite.Connection | None = None
        self.saver: AsyncSqliteSaver | None = None
        self.graph: Any = None
        self._submit(self._asetup())

    # -- loop 基础设施 --------------------------------------------------

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def _submit(self, coro: Coroutine[Any, Any, Any]) -> Any:
        """同步调用：把协程丢进专用 loop 并等结果。"""
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(self.call_timeout)

    async def _asubmit(self, coro: Coroutine[Any, Any, Any]) -> Any:
        """异步调用：不阻塞调用方事件循环。"""
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return await asyncio.wrap_future(future)

    async def _asetup(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(str(self._db_path))
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.commit()
        self.saver = AsyncSqliteSaver(self._conn)
        await self.saver.setup()
        self.graph = build_graph(
            self.deps, checkpointer=self.saver, retry_max_attempts=self.retry_max_attempts
        )

    def close(self) -> None:
        """幂等关闭：loop 已经停了就不要再往里提交协程（否则会阻塞到超时）。"""
        if self._closed:
            return
        self._closed = True
        try:
            if self._loop.is_running():
                self._submit(self._aclose())
        except Exception:  # noqa: BLE001 - 关闭失败不应阻塞退出
            pass
        try:
            if self._loop.is_running():
                self._loop.call_soon_threadsafe(self._loop.stop)
        except RuntimeError:
            pass
        self._thread.join(timeout=5)

    async def _aclose(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    # -- 启动 -----------------------------------------------------------

    async def start(
        self,
        *,
        name: str,
        text: str,
        to_addr: str = "",
        extra_instruction: str = "",
        session_id: str = "",
        workflow_id: str | None = None,
        allow_reuse: bool = True,
    ) -> WorkflowSnapshot:
        return await self._asubmit(
            self._start(
                name=name,
                text=text,
                to_addr=to_addr,
                extra_instruction=extra_instruction,
                session_id=session_id,
                workflow_id=workflow_id,
                allow_reuse=allow_reuse,
            )
        )

    def start_sync(self, **kwargs: Any) -> WorkflowSnapshot:
        return self._submit(self._start(**kwargs))

    async def _start(
        self,
        *,
        name: str,
        text: str,
        to_addr: str = "",
        extra_instruction: str = "",
        session_id: str = "",
        workflow_id: str | None = None,
        allow_reuse: bool = True,
    ) -> WorkflowSnapshot:
        """启动一个工作流；同一份会议纪要默认复用已有工作流（不重复建业务数据）。"""
        body = (text or "").strip()
        if len(body) < 30:
            raise WorkflowError("会议记录内容过短（少于 30 字），无法分析")
        text_hash = content_hash(body)

        if workflow_id is None:
            existing = self.deps.wf.find_run_by_text(text_hash) if allow_reuse else None
            if existing is not None and existing.get("status") not in {"completed", "cancelled"}:
                # 同文本已有未完成的工作流：直接返回它，避免"点两次开两条线"
                return await self._status(existing["workflow_id"])
            workflow_id = f"wf_{uuid.uuid4().hex[:12]}"

        document_id = self.deps.stage(workflow_id, body)
        self.deps.wf.create_run(
            workflow_id,
            thread_id=workflow_id,
            session_id=session_id,
            source_name=name,
            text_hash=text_hash,
            status="running",
            current_step="queued",
        )
        initial = {
            "workflow_id": workflow_id,
            "thread_id": workflow_id,
            "session_id": session_id,
            "document_id": document_id,
            "source_name": name,
            "text_hash": text_hash,
            "to_addr": to_addr,
            "extra_instruction": extra_instruction,
            "execution_status": "running",
            "current_step": "start",
            "started_at": _now(),
            "retry_count": 0,
        }
        return await self._invoke(workflow_id, initial, event="start")

    # -- 恢复 / 取消 ----------------------------------------------------

    async def resume(
        self,
        workflow_id: str,
        command: ResumeCommand,
        *,
        edit: dict[str, Any] | None = None,
    ) -> WorkflowSnapshot:
        return await self._asubmit(self._resume(workflow_id, command, edit=edit))

    def resume_sync(
        self, workflow_id: str, command: ResumeCommand, *, edit: dict[str, Any] | None = None
    ) -> WorkflowSnapshot:
        return self._submit(self._resume(workflow_id, command, edit=edit))

    async def _resume(
        self,
        workflow_id: str,
        command: ResumeCommand,
        *,
        edit: dict[str, Any] | None = None,
    ) -> WorkflowSnapshot:
        run = self.deps.wf.get_run(workflow_id)
        if run is None:
            raise WorkflowError("工作流不存在")
        if run.get("status") not in {"waiting_approval", "running", "failed"}:
            # 已终态：重复确认/取消都当成幂等请求，返回当前快照
            return await self._status(workflow_id)
        pending = await self._pending(workflow_id)
        if pending is None:
            # 没有待恢复的中断（可能是重复点击、或已恢复过）：不要再触发任何副作用
            return await self._status(workflow_id)
        if pending.version != command.version or pending.draft_hash != command.draft_hash:
            raise WorkflowError(
                f"审批已过期：你确认的是 v{command.version}，当前草稿是 v{pending.version}，"
                "请刷新后重新确认"
            )
        # 第二道校验：直接和**业务库里的当前草稿**比。审批期间可能有人绕过工作流改草稿
        # （例如调用旧的 email.update 接口），此时即使中断载荷对得上也不能发。
        email = self.deps.get_email(pending.email_draft_id) or {}
        db_version = int(email.get("version") or 0)
        db_hash = str(email.get("draft_hash") or "")
        if db_version != command.version or db_hash != command.draft_hash:
            raise WorkflowError(
                f"草稿已被改动（库里是 v{db_version}），你确认的是 v{command.version}，"
                "请刷新后重新审批"
            )
        update = dict(edit or {})
        payload = (
            Command(resume=command.model_dump(), update=update)
            if update
            else Command(resume=command.model_dump())
        )
        return await self._invoke(
            workflow_id, payload, event="resume", resumed_decision=command.decision
        )

    async def cancel(
        self, workflow_id: str, *, note: str = "", decided_by: str = "user"
    ) -> WorkflowSnapshot:
        return await self._asubmit(self._cancel(workflow_id, note=note, decided_by=decided_by))

    def cancel_sync(self, workflow_id: str, *, note: str = "") -> WorkflowSnapshot:
        return self._submit(self._cancel(workflow_id, note=note))

    def continue_run_sync(self, workflow_id: str) -> WorkflowSnapshot:
        """从最后一个 Checkpoint 继续执行（节点失败后重跑 / 断点续跑）。

        与 `resume` 的区别：`resume` 用于“等人工审批”的中断（要带 Command(resume=...)），
        `continue_run` 用于“执行中途崩了”（带着 `None` 输继续跑剩余待执行节点）。
        """
        return self._submit(self._continue(workflow_id))

    async def retry_send(self, workflow_id: str) -> WorkflowSnapshot:
        """人工确认后重试发送（仅允许“确定失败”，`uncertain` 一律拒绝）。"""
        return await self._asubmit(self._retry_send(workflow_id))

    def retry_send_sync(self, workflow_id: str) -> WorkflowSnapshot:
        return self._submit(self._retry_send(workflow_id))

    async def _retry_send(self, workflow_id: str) -> WorkflowSnapshot:
        run = self.deps.wf.get_run(workflow_id)
        if run is None:
            raise WorkflowError("工作流不存在")
        email = self.deps.get_email(str(run.get("email_draft_id") or ""))
        if email is None:
            raise WorkflowError("没有可重试的邮件草稿")
        if email.get("status") == "sent":
            return await self._status(workflow_id)
        if email.get("status") == "uncertain":
            raise WorkflowError(
                "上一次发送的结果不确定（SMTP 可能已接收），为避免重复投递不会自动重试，"
                "请人工核实后再处理"
            )
        return await self._invoke(
            workflow_id, Command(goto="send_email"), event="retry_send"
        )

    async def continue_run(self, workflow_id: str) -> WorkflowSnapshot:
        return await self._asubmit(self._continue(workflow_id))

    async def _continue(self, workflow_id: str) -> WorkflowSnapshot:
        if self.deps.wf.get_run(workflow_id) is None:
            raise WorkflowError("工作流不存在")
        pending = await self._pending(workflow_id)
        if pending is not None:
            # 正在等审批：这种“续跑”没意义，引导调用方走 resume
            return await self._status(workflow_id)
        return await self._invoke(workflow_id, None, event="continue")

    async def _cancel(
        self, workflow_id: str, *, note: str = "", decided_by: str = "user"
    ) -> WorkflowSnapshot:
        pending = await self._pending(workflow_id)
        if pending is None:
            if self.deps.wf.get_run(workflow_id) is None:
                raise WorkflowError("工作流不存在")
            return await self._status(workflow_id)
        command = ResumeCommand(
            decision="reject",
            version=pending.version,
            draft_hash=pending.draft_hash,
            note=note,
            decided_by=decided_by,
        )
        return await self._resume(workflow_id, command)

    # -- 查询 -----------------------------------------------------------

    async def pending_approval(self, workflow_id: str) -> ApprovalRequest | None:
        return await self._asubmit(self._pending(workflow_id))

    def pending_approval_sync(self, workflow_id: str) -> ApprovalRequest | None:
        return self._submit(self._pending(workflow_id))

    async def _pending(self, workflow_id: str) -> ApprovalRequest | None:
        snapshot = await self.graph.aget_state(thread_config(workflow_id))
        for task in getattr(snapshot, "tasks", ()) or ():
            interrupts = getattr(task, "interrupts", ()) or ()
            for item in interrupts:
                value = getattr(item, "value", None)
                if isinstance(value, dict) and value.get("email_draft_id"):
                    try:
                        return ApprovalRequest.model_validate(value)
                    except Exception:  # noqa: BLE001 - 老版本 payload 兼容
                        continue
        return None

    async def status(self, workflow_id: str) -> WorkflowSnapshot:
        return await self._asubmit(self._status(workflow_id))

    def status_sync(self, workflow_id: str) -> WorkflowSnapshot:
        return self._submit(self._status(workflow_id))

    async def _status(self, workflow_id: str) -> WorkflowSnapshot:
        run = self.deps.wf.get_run(workflow_id) or {}
        snapshot = await self.graph.aget_state(thread_config(workflow_id))
        values: dict[str, Any] = dict(getattr(snapshot, "values", {}) or {})
        pending = await self._pending(workflow_id)
        next_nodes = [str(n) for n in (getattr(snapshot, "next", ()) or ())]
        email = self.deps.get_email(
            str(values.get("email_draft_id") or run.get("email_draft_id") or "")
        ) or {}
        config = getattr(snapshot, "config", {}) or {}
        return WorkflowSnapshot(
            workflow_id=workflow_id,
            thread_id=str(run.get("thread_id") or workflow_id),
            execution_status=str(run.get("status") or "pending"),
            current_step=str(values.get("current_step") or run.get("current_step") or ""),
            approval_status=str(
                values.get("approval_status") or run.get("approval_status") or "none"
            ),
            approval=pending,
            meeting_id=str(values.get("meeting_id") or run.get("meeting_id") or ""),
            email_draft_id=str(values.get("email_draft_id") or ""),
            email_version=int(email.get("version") or values.get("email_version") or 0),
            summary=str(values.get("meeting_summary") or ""),
            risks=list(values.get("validated_risks") or []),
            todos=list(values.get("action_items") or []),
            errors=list(values.get("errors") or []),
            started_at=str(values.get("started_at") or run.get("created_at") or ""),
            completed_at=str(values.get("completed_at") or run.get("completed_at") or ""),
            resumable=pending is not None,
            next_nodes=[NODE_LABELS.get(n, n) for n in next_nodes],
            checkpoint_id=str((config.get("configurable", {}) or {}).get("checkpoint_id", "")),
        )

    async def history_async(self, workflow_id: str, *, limit: int = 200) -> dict[str, Any]:
        run = self.deps.wf.get_run(workflow_id)
        if run is None:
            raise WorkflowError("工作流不存在")
        snapshot = await self._status(workflow_id)
        return {
            "run": run,
            "events": self.deps.wf.list_events(workflow_id, limit=limit),
            "effects": self.deps.wf.list_effects(workflow_id),
            "tokens": self.deps.wf.token_totals(workflow_id),
            "snapshot": snapshot.model_dump(),
        }

    def history(self, workflow_id: str, *, limit: int = 200) -> dict[str, Any]:
        return self._submit(self.history_async(workflow_id, limit=limit))

    def list(self, *, limit: int = 50) -> list[dict[str, Any]]:
        return self.deps.wf.list_runs(limit=limit)
    # -- 内部 -----------------------------------------------------------

    async def _invoke(
        self,
        workflow_id: str,
        payload: Any,
        *,
        event: str,
        resumed_decision: str = "",
    ) -> WorkflowSnapshot:
        config = thread_config(workflow_id)
        started = _now()
        try:
            await self.graph.ainvoke(payload, config)
        except Exception as exc:  # noqa: BLE001 - 失败要落库并如实上报
            self.deps.wf.update_run(
                workflow_id,
                status="failed",
                error=f"{type(exc).__name__}: {exc}"[:500],
                current_step="failed",
            )
            self.deps.wf.append_event(
                workflow_id,
                node="graph",
                event=event,
                status="error",
                started_at=started,
                ended_at=_now(),
                error_type=type(exc).__name__,
                detail={"error": str(exc)[:400], "decision": resumed_decision},
            )
            raise WorkflowError(f"工作流执行失败：{exc}") from exc

        snapshot = await self._status(workflow_id)
        final_status = (
            "waiting_approval" if snapshot.resumable else await self._terminal_status(workflow_id)
        )
        self.deps.wf.update_run(
            workflow_id,
            status=final_status,
            current_step=snapshot.current_step or final_status,
            meeting_id=snapshot.meeting_id,
            email_draft_id=snapshot.email_draft_id,
            email_version=snapshot.email_version,
            approval_status=snapshot.approval_status,
            completed_at=snapshot.completed_at if final_status != "waiting_approval" else "",
        )
        state_snapshot = await self.graph.aget_state(config)
        checkpoint_id = str(
            ((getattr(state_snapshot, "config", {}) or {}).get("configurable", {}) or {}).get(
                "checkpoint_id", ""
            )
        )
        self.deps.wf.append_event(
            workflow_id,
            node="graph",
            event=event,
            status="ok",
            started_at=started,
            ended_at=_now(),
            checkpoint_id=checkpoint_id,
            detail={
                "decision": resumed_decision,
                "status": final_status,
                "next": list(getattr(state_snapshot, "next", ()) or ()),
            },
        )
        return await self._status(workflow_id)

    async def _terminal_status(self, workflow_id: str) -> str:
        """从最终 state 推出终态（完成 / 取消 / 失败 / 仍在跑）。"""
        snapshot = await self.graph.aget_state(thread_config(workflow_id))
        values = dict(getattr(snapshot, "values", {}) or {})
        execution = str(values.get("execution_status") or "")
        if execution in {"completed", "cancelled", "failed"}:
            return execution
        next_nodes = list(getattr(snapshot, "next", ()) or ())
        if not next_nodes:
            return "completed" if values.get("completed_at") else "failed"
        return "running"


def idempotency_preview(workflow_id: str, email_id: str, version: int) -> str:
    """给测试/文档用的幂等键展示。"""
    return effect_key(workflow_id, "send_email", email_id, version)


def store_for(workspace: Path) -> WorkflowStore:  # pragma: no cover - 便捷函数
    return WorkflowStore(Path(workspace) / ".aieb" / "workflow" / "workflow.sqlite3")
