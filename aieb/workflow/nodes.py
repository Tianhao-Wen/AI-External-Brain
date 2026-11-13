"""Graph 节点实现。

设计约束（这几条决定了"不是把老流程塞进一个 Node"）：

1. **每个节点只做一件事**：加载 → 分析 → 校验风险 → 抽取待办 → 合并 → 落库 → 生成草稿
   → 人工审批 → 发送 → 审计；不是"一个大函数包住整个流程"。
2. **并行分支写不同字段**：`validate_risks` 只写 `validated_risks/dropped_risks`，
   `extract_todos` 只写 `action_items`；两者都不碰对方的键，合并规则因此是确定的。
3. **节点内不做不可重复的外部副作用**：`human_approval` 里只有读 + `interrupt()`；
   邮件真正发送在独立的 `send_email` 节点，并且有幂等键保护。
4. **业务写入全部走已有 `BrainStore`**：节点不发明新的真相来源。
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from aieb.brain import risk_rules
from aieb.workflow.deps import WorkflowDeps
from aieb.workflow.persistence import effect_key
from aieb.workflow.state import ApprovalRequest, ResumeCommand, WorkflowState

try:  # langgraph 只在装了可选依赖时存在；没装时模块仍可被静态分析
    from langgraph.errors import GraphBubbleUp, GraphInterrupt
except ImportError:  # pragma: no cover
    class GraphInterrupt(Exception):  # type: ignore[no-redef]
        """占位：未安装 langgraph 时永远不会被触发。"""

    class GraphBubbleUp(Exception):  # type: ignore[no-redef]
        """占位：仅用于类型/可读性。"""

NodeFn = Callable[[WorkflowState], Awaitable[dict[str, Any]]]
PENDING = "待确认"


async def with_retry(
    fn: Callable[[], Awaitable[Any]],
    *,
    attempts: int,
    on_error: Callable[[int, Exception], None] | None = None,
) -> tuple[Any, int]:
    """节点内的**可控重试**（返回 (结果, 实际重试次数)）。

    为什么不用框架的 `RetryPolicy`：在 langgraph 1.2.20 上实测节点级 retry_policy
    不会触发（见 `docs/LANGGRAPH_WORKFLOW.md` 的验证记录），而且在业务上我们更希望对
    “哪些错误值得重试”有明确控制 —— LLM 超时可以重试，"审批过期"重试是错的。
    """
    last: Exception | None = None
    for attempt in range(1, max(1, attempts) + 1):
        try:
            return await fn(), attempt - 1
        except Exception as exc:  # noqa: BLE001 - 由调用方决定是否继续重试
            last = exc
            if on_error is not None:
                on_error(attempt, exc)
            if attempt >= attempts:
                break
    assert last is not None
    raise last


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _squash(text: str) -> str:
    return risk_rules.squash(text or "")


def _grounded(value: str, raw_squashed: str) -> bool:
    candidate = _squash(value or "")
    if not candidate:
        return False
    return candidate in raw_squashed


# ---------------------------------------------------------------------------
# Traced 包装：节点级 Trace（本地 SQLite，不上传任何外部平台）
# ---------------------------------------------------------------------------


def traced(name: str, deps: WorkflowDeps, fn: NodeFn) -> NodeFn:
    async def wrapper(state: WorkflowState) -> dict[str, Any]:
        workflow_id = str(state.get("workflow_id") or "")
        if deps.fault is not None:
            # 故障注入点：可以在这里抛异常或直接 os._exit() 制造真正的进程崩溃
            deps.fault(name)
        started_at = _now()
        started = time.perf_counter()
        try:
            update = await fn(state)
        except (GraphInterrupt, GraphBubbleUp) as signal:
            # `interrupt()` 用异常把控制权交回调用方 —— 这是**正常控制流**，不是节点失败。
            # 早期把它记成 status=error，导致 Trace 里“失败节点”总是多出一个
            # human_approval，会误导排障；这里单独记为 interrupted。
            duration = (time.perf_counter() - started) * 1000
            deps.wf.append_event(
                workflow_id,
                node=name,
                event="interrupt",
                status="interrupted",
                started_at=started_at,
                ended_at=_now(),
                duration_ms=round(duration, 2),
                detail={"signal": type(signal).__name__, "reason": "等待人工审批"},
            )
            raise
        except Exception as exc:  # noqa: BLE001 - 失败必须留痕并如实上报
            duration = (time.perf_counter() - started) * 1000
            deps.wf.append_event(
                workflow_id,
                node=name,
                event="node",
                status="error",
                started_at=started_at,
                ended_at=_now(),
                duration_ms=round(duration, 2),
                error_type=type(exc).__name__,
                detail={"error": str(exc)[:400]},
            )
            raise
        duration = (time.perf_counter() - started) * 1000
        usage = (update.pop("_usage", None) or {}) if isinstance(update, dict) else {}
        deps.wf.append_event(
            workflow_id,
            node=name,
            event="node",
            status="ok",
            started_at=started_at,
            ended_at=_now(),
            duration_ms=round(duration, 2),
            # 只有真正调用模型的节点才写 model 名，否则 Trace 会把每个节点都
            # 标成同一个模型，既误导统计也无法区分“模型失败”与“库里写失败”。
            model=str(usage.get("model") or ""),
            input_tokens=int(usage.get("input_tokens") or 0),
            output_tokens=int(usage.get("output_tokens") or 0),
            retry=int(usage.get("retry") or 0),
            detail=usage.get("detail") or {},
        )
        update["node_trace"] = [
            {"node": name, "duration_ms": round(duration, 2), "at": started_at}
        ]
        return update

    return wrapper


# ---------------------------------------------------------------------------
# 节点
# ---------------------------------------------------------------------------


def build_nodes(deps: WorkflowDeps) -> dict[str, NodeFn]:
    store = deps.store

    async def load_meeting(state: WorkflowState) -> dict[str, Any]:
        """读取暂存原文，并判断"这份纪要是不是已经处理过"。"""
        document_id = str(state.get("document_id") or "")
        text = deps.read_text(document_id)
        text_hash = str(state.get("text_hash") or "")
        existing = store.find_meeting_by_text(text)
        update: dict[str, Any] = {
            "current_step": "load_meeting",
            "execution_status": "running",
            "text_hash": text_hash,
            "started_at": state.get("started_at") or _now(),
        }
        if existing is not None:
            # 文本级去重：同一份纪要不会因为"重启后重跑"再建一次会议/风险/待办
            update["meeting_id"] = existing["id"]
            update["meeting_summary"] = existing.get("summary") or ""
            update["meeting_title"] = existing.get("title") or ""
            update["_usage"] = {"detail": {"reused_meeting": existing["id"]}}
        return update

    async def analyze_meeting(state: WorkflowState) -> dict[str, Any]:
        text = deps.read_text(str(state["document_id"]))
        workflow_id = str(state.get("workflow_id") or "")

        def _note(attempt: int, exc: Exception) -> None:
            deps.wf.append_event(
                workflow_id,
                node="analyze_meeting",
                event="retry",
                status="error",
                retry=attempt,
                error_type=type(exc).__name__,
                detail={"error": str(exc)[:200]},
            )

        outcome, retries = await with_retry(
            lambda: deps.analyzer(str(state.get("source_name") or ""), text),
            attempts=max(1, int(deps.retry_attempts)),
            on_error=_note,
        )
        analysis = outcome.analysis
        candidates = [
            {
                "id": f"cand_{i}",
                "title": r.title.strip() or "未命名风险",
                "level": r.level,
                "risk_type": (r.risk_type or "").strip(),
                "status": r.status,
                "description": r.description.strip(),
                "evidence": r.evidence,
                "suggestion": r.suggestion.strip(),
            }
            for i, r in enumerate(analysis.risks, start=1)
        ]
        todos = [
            {
                "id": f"todo_{i}",
                "content": t.content.strip(),
                "owner": t.owner.strip(),
                "due_date": t.due_date.strip(),
                "priority": t.priority,
            }
            for i, t in enumerate(analysis.todos, start=1)
            if t.content.strip()
        ]
        return {
            "current_step": "analyze_meeting",
            "meeting_title": analysis.title.strip() or str(state.get("source_name") or "未命名会议"),
            "meeting_summary": analysis.summary.strip(),
            "decisions": list(analysis.decisions),
            "risk_candidates": candidates,
            "todo_candidates": todos,
            "_usage": {
                "model": outcome.model,
                "input_tokens": outcome.input_tokens,
                "output_tokens": outcome.output_tokens,
                "retry": retries,
                "detail": {
                    "risk_candidates": len(candidates),
                    "todo_candidates": len(todos),
                    "retry": retries,
                },
            },
        }

    async def validate_risks(state: WorkflowState) -> dict[str, Any]:
        """确定性风险校验：证据必须能在原文里找到 + 业务规则过滤 + 去重。"""
        raw_squashed = _squash(deps.read_text(str(state["document_id"])))
        candidates = []
        for item in state.get("risk_candidates") or []:
            evidence = str(item.get("evidence") or "")
            grounded = bool(evidence) and _grounded(evidence, raw_squashed)
            candidates.append({**item, "evidence": evidence if grounded else ""})
        kept, dropped = risk_rules.filter_risks(candidates, deps.read_text(str(state["document_id"])))
        return {
            # 注意：并行分支**不能**写 current_step 这类标量键，
            # 否则同一 super-step 内两个节点会争抢同一个 channel（LangGraph 会报
            # InvalidUpdateError）。步数由串行节点与 node_trace 表达。
            "validated_risks": kept,
            "dropped_risks": [
                {"id": f"drop_{i}", "title": d.get("title", ""), "reason": d.get("dropped_reason", "")}
                for i, d in enumerate(dropped, start=1)
            ],
            "_usage": {"detail": {"kept": len(kept), "dropped": len(dropped)}},
        }

    async def extract_todos(state: WorkflowState) -> dict[str, Any]:
        """待办接地：负责人/截止时间在原文里找不到就退回"待确认"。"""
        raw_squashed = _squash(deps.read_text(str(state["document_id"])))
        items: list[dict[str, Any]] = []
        for item in state.get("todo_candidates") or []:
            owner = str(item.get("owner") or "")
            if owner and not _grounded(owner, raw_squashed):
                owner = ""
            due = str(item.get("due_date") or "")
            if due and not (_grounded(due, raw_squashed) or _grounded(due.rstrip("日号"), raw_squashed)):
                due = ""
            items.append({**item, "owner": owner or PENDING, "due_date": due or PENDING})
        return {
            # 同上：并行分支只写自己的字段
            "action_items": items,
            "_usage": {"detail": {"todos": len(items)}},
        }

    async def merge_results(state: WorkflowState) -> dict[str, Any]:
        risks = list(state.get("validated_risks") or [])
        todos = list(state.get("action_items") or [])
        return {
            "current_step": "merge_results",
            "execution_status": "running",
            "_usage": {
                "detail": {
                    "risks": len(risks),
                    "todos": len(todos),
                    "dropped": len(state.get("dropped_risks") or []),
                }
            },
        }

    async def persist_business_state(state: WorkflowState) -> dict[str, Any]:
        """把会议 + 风险 + 待办 + 记忆写入业务库 —— **整个工作流唯一的一次业务写入**。"""
        workflow_id = str(state["workflow_id"])
        if state.get("meeting_id"):
            return {"current_step": "persist_business_state"}

        text = deps.read_text(str(state["document_id"]))
        key = effect_key(workflow_id, "persist_meeting", str(state.get("text_hash") or ""), 0)
        allowed, record = deps.wf.begin_effect(
            key, workflow_id=workflow_id, action="persist_meeting",
            object_id=str(state.get("text_hash") or ""),
        )
        if not allowed:
            if record.get("status") == "succeeded":
                import json

                payload = json.loads(record.get("result") or "{}")
                return {
                    "current_step": "persist_business_state",
                    "meeting_id": str(payload.get("meeting_id") or ""),
                    "_usage": {"detail": {"idempotent_reuse": True}},
                }
            raise RuntimeError(
                f"业务写入状态异常（{record.get('status')}），为避免重复副作用已停止"
            )

        try:
            existing = store.find_meeting_by_text(text)
            if existing is not None:
                meeting = existing
            else:
                meeting = store.add_meeting(
                    title=str(state.get("meeting_title") or "未命名会议"),
                    summary=str(state.get("meeting_summary") or ""),
                    decisions=list(state.get("decisions") or []),
                    participants=[],
                    discussion=[],
                    source_name=str(state.get("source_name") or ""),
                    raw_text=text,
                    risks=[
                        {k: v for k, v in r.items() if k != "id"}
                        for r in (state.get("validated_risks") or [])
                    ],
                    todos=[
                        {
                            "content": t.get("content", ""),
                            "owner": "" if t.get("owner") == PENDING else t.get("owner", ""),
                            "due_date": "" if t.get("due_date") == PENDING else t.get("due_date", ""),
                            "priority": t.get("priority", "medium"),
                        }
                        for t in (state.get("action_items") or [])
                    ],
                )
                if deps.activity is not None:
                    deps.activity(
                        "meeting",
                        f"完成会议分析：{meeting.get('title', '')}",
                        f"{len(state.get('validated_risks') or [])} 条风险 / "
                        f"{len(state.get('action_items') or [])} 条待办",
                    )
        except Exception as exc:
            deps.wf.finish_effect(key, "failed", error=str(exc)[:300])
            raise
        deps.wf.finish_effect(key, "succeeded", result={"meeting_id": meeting["id"]})
        return {
            "current_step": "persist_business_state",
            "meeting_id": meeting["id"],
            "_usage": {"detail": {"meeting_id": meeting["id"]}},
        }

    async def generate_email_draft(state: WorkflowState) -> dict[str, Any]:
        """生成草稿（可重复执行，但不会重复建草稿）。"""
        workflow_id = str(state["workflow_id"])
        existing_id = str(state.get("email_draft_id") or "")
        if existing_id:
            email = deps.get_email(existing_id)
            if email is not None:
                return {
                    "current_step": "generate_email_draft",
                    "email_version": int(email.get("version") or 1),
                    "email_draft_hash": str(email.get("draft_hash") or ""),
                }

        meeting = store.get_meeting(str(state["meeting_id"])) or {}
        to_addr = str(state.get("to_addr") or "")
        key = effect_key(workflow_id, "draft_email", str(state["meeting_id"]), 1)
        allowed, record = deps.wf.begin_effect(
            key, workflow_id=workflow_id, action="draft_email",
            object_id=str(state["meeting_id"]), version=1,
        )
        if not allowed:
            if record.get("status") == "succeeded":
                import json

                payload = json.loads(record.get("result") or "{}")
                return {
                    "current_step": "generate_email_draft",
                    "email_draft_id": str(payload.get("email_id") or ""),
                    "email_version": int(payload.get("version") or 1),
                    "email_draft_hash": str(payload.get("draft_hash") or ""),
                    "_usage": {"detail": {"idempotent_reuse": True}},
                }
            raise RuntimeError(f"草稿生成状态异常（{record.get('status')}）")

        try:
            outcome, retries = await with_retry(
                lambda: deps.drafter(
                    meeting, to_addr, str(state.get("extra_instruction") or "")
                ),
                attempts=max(1, int(deps.retry_attempts)),
                on_error=lambda attempt, exc: deps.wf.append_event(
                    workflow_id,
                    node="generate_email_draft",
                    event="retry",
                    status="error",
                    retry=attempt,
                    error_type=type(exc).__name__,
                    detail={"error": str(exc)[:200]},
                ),
            )
            draft_hash = deps.draft_hash(to_addr, outcome.subject, outcome.body)
            email = store.create_email(
                to_addr=to_addr,
                subject=outcome.subject,
                body=outcome.body,
                meeting_id=str(state["meeting_id"]),
                kind="reminder",
                version=1,
                draft_hash=draft_hash,
                workflow_id=workflow_id,
            )
        except Exception as exc:
            deps.wf.finish_effect(key, "failed", error=str(exc)[:300])
            raise
        deps.wf.finish_effect(
            key,
            "succeeded",
            result={"email_id": email["id"], "version": 1, "draft_hash": draft_hash},
        )
        return {
            "current_step": "generate_email_draft",
            "email_draft_id": email["id"],
            "email_version": 1,
            "email_draft_hash": draft_hash,
            "approval_status": "pending",
            "execution_status": "waiting_approval",
            "_usage": {
                "model": outcome.model,
                "input_tokens": outcome.input_tokens,
                "output_tokens": outcome.output_tokens,
                "retry": retries,
                "detail": {"email_id": email["id"], "fallback": outcome.fallback},
            },
        }

    async def human_approval(state: WorkflowState) -> dict[str, Any]:
        """暂停点。**这里绝不能有副作用** —— 恢复时会重新进入本节点。"""
        from langgraph.types import interrupt

        email = deps.get_email(str(state.get("email_draft_id") or ""))
        if email is None:
            raise RuntimeError("待审批的邮件草稿不存在")
        version = int(email.get("version") or 1)
        draft_hash = str(email.get("draft_hash") or "") or deps.draft_hash(
            str(email.get("to_addr") or ""), str(email.get("subject") or ""), str(email.get("body") or "")
        )
        request = ApprovalRequest(
            workflow_id=str(state["workflow_id"]),
            email_draft_id=str(email["id"]),
            version=version,
            draft_hash=draft_hash,
            to_addr=str(email.get("to_addr") or ""),
            subject=str(email.get("subject") or ""),
            body=str(email.get("body") or ""),
            created_at=_now(),
        )
        resume_value = interrupt(request.model_dump())
        command = ResumeCommand.model_validate(resume_value or {})

        # 恢复后必须重新读库：期间草稿可能被改过
        current = deps.get_email(str(email["id"])) or {}
        current_version = int(current.get("version") or 1)
        current_hash = str(current.get("draft_hash") or "") or deps.draft_hash(
            str(current.get("to_addr") or ""),
            str(current.get("subject") or ""),
            str(current.get("body") or ""),
        )
        update: dict[str, Any] = {
            "current_step": "human_approval",
            "approval_version": command.version,
            "approval_note": command.note,
            "_usage": {
                "detail": {
                    "decision": command.decision,
                    "approved_version": command.version,
                    "current_version": current_version,
                }
            },
        }
        if command.version != current_version or command.draft_hash != current_hash:
            update.update(
                approval_status="invalid",
                execution_status="failed",
                errors=[
                    {
                        "node": "human_approval",
                        "type": "StaleApproval",
                        "message": (
                            f"审批绑定的是 v{command.version}/{command.draft_hash[:8]}，"
                            f"当前草稿是 v{current_version}/{current_hash[:8]}，拒绝按旧审批执行"
                        ),
                    }
                ],
            )
            return update
        if command.decision == "reject":
            update.update(approval_status="rejected", execution_status="cancelled")
        elif command.decision == "edit":
            update.update(approval_status="edited", execution_status="waiting_approval")
        else:
            update.update(approval_status="approved", execution_status="running")
        return update

    async def update_draft(state: WorkflowState) -> dict[str, Any]:
        """应用用户修改并**升级版本**；修改后必须重新审批一次。"""
        email_id = str(state.get("email_draft_id") or "")
        email = deps.get_email(email_id)
        if email is None:
            raise RuntimeError("待修改的邮件草稿不存在")
        to_addr = str(state.get("edit_to_addr") or email.get("to_addr") or "")
        subject = str(state.get("edit_subject") or email.get("subject") or "")
        body = str(state.get("edit_body") or email.get("body") or "")
        saved = deps.save_draft(
            email_id, to_addr=to_addr, subject=subject, body=body, bump_version=True
        )
        return {
            "current_step": "update_draft",
            "email_version": int(saved.get("version") or 2),
            "email_draft_hash": str(saved.get("draft_hash") or ""),
            "approval_status": "pending",
            "execution_status": "waiting_approval",
            "_usage": {"detail": {"version": int(saved.get("version") or 2)}},
        }

    async def send_email(state: WorkflowState) -> dict[str, Any]:
        """真正的副作用节点：幂等键 + 状态机 + uncertain 语义。"""
        workflow_id = str(state["workflow_id"])
        email_id = str(state.get("email_draft_id") or "")
        email = deps.get_email(email_id)
        if email is None:
            raise RuntimeError("待发送的邮件草稿不存在")
        if email.get("status") == "sent":
            return {
                "current_step": "send_email",
                "execution_status": "completed",
                "completed_at": _now(),
                "_usage": {"detail": {"idempotent": "already_sent"}},
            }

        version = int(email.get("version") or 1)
        if not str(email.get("to_addr") or "").strip():
            # 空收件人：快速失败并给出可操作提示，而不是等到 SMTP 层报“格式不正确”。
            # 不能用 uncertain：这里**确定**没有发出去，属于可以人工重试的失败。
            store.update_email(email_id, status="failed", error="收件人为空，未发送")
            return {
                "current_step": "send_email",
                "execution_status": "failed",
                "errors": [
                    {
                        "node": "send_email",
                        "type": "InvalidRecipient",
                        "message": "收件人为空：请在审批卡片里填写收件人，保存修改后重新确认",
                    }
                ],
                "_usage": {"detail": {"guard": "empty_recipient"}},
            }
        key = effect_key(workflow_id, "send_email", email_id, version)
        allowed, record = deps.wf.begin_effect(
            key, workflow_id=workflow_id, action="send_email", object_id=email_id, version=version
        )
        if not allowed:
            status = str(record.get("status") or "")
            if status == "succeeded":
                return {
                    "current_step": "send_email",
                    "execution_status": "completed",
                    "completed_at": _now(),
                    "_usage": {"detail": {"idempotent": "succeeded"}},
                }
            # executing / uncertain：上次可能已经把信交给 SMTP，绝不自动重发
            store.update_email(email_id, status="uncertain", error=f"幂等键状态 {status}")
            return {
                "current_step": "send_email",
                "execution_status": "failed",
                "errors": [
                    {
                        "node": "send_email",
                        "type": "UncertainDelivery",
                        "message": (
                            f"上一次发送（v{version}）状态为 {status}，SMTP 可能已接收，"
                            "为避免重复投递不会自动重试，请人工核实后处理"
                        ),
                    }
                ],
                "_usage": {"detail": {"idempotent": status}},
            }

        try:
            outcome = deps.sender(
                str(email.get("to_addr") or ""),
                str(email.get("subject") or ""),
                str(email.get("body") or ""),
            )
        except Exception as exc:  # noqa: BLE001 - 未知是否已投递 → uncertain
            deps.wf.finish_effect(key, "uncertain", error=f"{type(exc).__name__}: {exc}"[:300])
            store.update_email(email_id, status="uncertain", error=str(exc)[:300])
            return {
                "current_step": "send_email",
                "execution_status": "failed",
                "errors": [
                    {
                        "node": "send_email",
                        "type": "UncertainDelivery",
                        "message": f"发送异常（{type(exc).__name__}）：{exc}，需人工核实是否已投递",
                    }
                ],
            }

        if outcome.ok:
            store.update_email(email_id, status="sent", error="", sent_at=_now())
            deps.wf.finish_effect(key, "succeeded", result={"message": outcome.message})
            return {
                "current_step": "send_email",
                "execution_status": "completed",
                "completed_at": _now(),
                "_usage": {"detail": {"message": outcome.message}},
            }
        if outcome.uncertain:
            store.update_email(email_id, status="uncertain", error=outcome.message[:300])
            deps.wf.finish_effect(key, "uncertain", error=outcome.message[:300])
            return {
                "current_step": "send_email",
                "execution_status": "failed",
                "errors": [
                    {
                        "node": "send_email",
                        "type": "UncertainDelivery",
                        "message": f"发送结果不确定：{outcome.message}",
                    }
                ],
            }
        store.update_email(email_id, status="failed", error=outcome.message[:300])
        deps.wf.finish_effect(key, "failed", error=outcome.message[:300])
        return {
            "current_step": "send_email",
            "execution_status": "failed",
            "errors": [
                {
                    "node": "send_email",
                    "type": "SendFailed",
                    "message": outcome.message or "发送失败",
                }
            ],
        }

    async def cancelled(state: WorkflowState) -> dict[str, Any]:
        email_id = str(state.get("email_draft_id") or "")
        email = deps.get_email(email_id)
        if email is not None and email.get("status") not in {"sent"}:
            store.update_email(email_id, status="cancelled", error="用户取消发送")
        return {
            "current_step": "cancelled",
            "approval_status": "rejected",
            "execution_status": "cancelled",
            "completed_at": _now(),
        }

    async def validation_error(state: WorkflowState) -> dict[str, Any]:
        return {
            "current_step": "validation_error",
            "execution_status": "failed",
            "completed_at": _now(),
        }

    async def record_audit(state: WorkflowState) -> dict[str, Any]:
        """收尾：写审计、清掉暂存原文（业务库里的 raw_text 才是长期存放点）。"""
        document_id = str(state.get("document_id") or "")
        if document_id:
            deps.drop_text(document_id)
        email = deps.get_email(str(state.get("email_draft_id") or ""))
        detail = (
            f"会议 {state.get('meeting_id') or '-'} / "
            f"风险 {len(state.get('validated_risks') or [])} / "
            f"待办 {len(state.get('action_items') or [])} / "
            f"邮件 {email.get('status') if email else '-'}"
        )
        deps.record_activity(f"工作流结束：{state.get('meeting_title') or state.get('workflow_id')}", detail)
        return {
            "current_step": "record_audit",
            "completed_at": state.get("completed_at") or _now(),
            "_usage": {"detail": {"meeting_id": state.get("meeting_id", "")}},
        }

    raw: dict[str, NodeFn] = {
        "load_meeting": load_meeting,
        "analyze_meeting": analyze_meeting,
        "validate_risks": validate_risks,
        "extract_todos": extract_todos,
        "merge_results": merge_results,
        "persist_business_state": persist_business_state,
        "generate_email_draft": generate_email_draft,
        "human_approval": human_approval,
        "update_draft": update_draft,
        "send_email": send_email,
        "cancelled": cancelled,
        "validation_error": validation_error,
        "record_audit": record_audit,
    }
    return {name: traced(name, deps, fn) for name, fn in raw.items()}
