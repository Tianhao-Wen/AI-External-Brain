"""WebSocket 业务操作分发：前端所有页面数据都通过 ``brain_request`` 消息获取。

为什么不用 HTTP POST 上传：服务端的 WebSocket 库只在握手阶段暴露请求头，
拿不到 multipart 正文。把业务操作统一放进已有的 WebSocket 通道，
可以复用现有的 Origin 校验与 token 认证，也不必额外开放端口。
"""

from __future__ import annotations

import base64
import binascii
import logging
from typing import Any, Callable

from aieb.brain import knowledge, mailer
from aieb.brain.service import BrainService

log = logging.getLogger(__name__)

MAX_UPLOAD_BYTES = 15 * 1024 * 1024  # base64 后约 20MB，仍低于 32MB 的 WS 上限
MAX_TEXT_CHARS = 400_000


class BrainOpsError(RuntimeError):
    """可直接展示给用户的业务错误。"""


def _workflow_message(exc: Exception) -> str:
    """把工作流异常翻译成用户能读懂的提示（不带堆栈）。"""
    text = str(exc) or exc.__class__.__name__
    if "审批已过期" in text or "草稿已被改动" in text:
        return text
    if "工作流不存在" in text:
        return "工作流不存在或已被清理"
    if "langgraph" in text.lower() or "ImportError" in type(exc).__name__:
        return "工作流功能未启用：请安装可选依赖（requirements-langgraph.txt）"
    return f"工作流执行失败：{text}"


class BrainOps:
    def __init__(
        self,
        service: BrainService,
        *,
        provider_getter: Callable[[], Any] | None = None,
    ) -> None:
        self.service = service
        self._provider_getter = provider_getter

    def _sync_provider(self) -> None:
        if self._provider_getter is not None:
            provider = self._provider_getter()
            if provider is not None:
                self.service.provider = provider

    async def dispatch(self, op: str, data: dict[str, Any]) -> dict[str, Any]:
        handler = getattr(self, f"op_{op.replace('.', '_')}", None)
        if handler is None:
            raise BrainOpsError(f"不支持的操作：{op}")
        self._sync_provider()
        return await handler(data or {})

    # ------------------------------------------------------------------
    # 工作台
    # ------------------------------------------------------------------
    async def op_dashboard(self, _data: dict[str, Any]) -> dict[str, Any]:
        return self.service.dashboard()

    async def op_health(self, _data: dict[str, Any]) -> dict[str, Any]:
        return {
            "ok": True,
            "documents": self.service.store.stats()["documents"],
            "mail": mailer.load_config().public_view(),
        }

    # ------------------------------------------------------------------
    # 活动记录（工作台「最新活动」）
    # ------------------------------------------------------------------
    async def op_activities_list(self, data: dict[str, Any]) -> dict[str, Any]:
        limit = int(data.get("limit") or 50)
        return {"activities": self.service.store.list_activities(limit=max(1, min(limit, 200)))}

    async def op_activities_delete(self, data: dict[str, Any]) -> dict[str, Any]:
        activity_id = str(data.get("id") or "")
        if not activity_id:
            raise BrainOpsError("缺少活动记录 ID")
        if not self.service.delete_activity(activity_id):
            raise BrainOpsError("活动记录不存在或已被删除")
        return {"deleted": activity_id}

    async def op_activities_clear(self, _data: dict[str, Any]) -> dict[str, Any]:
        return {"deleted": self.service.clear_activities()}

    # ------------------------------------------------------------------
    # 知识库
    # ------------------------------------------------------------------
    async def op_documents_list(self, _data: dict[str, Any]) -> dict[str, Any]:
        return {"documents": self.service.store.list_documents()}

    async def op_documents_upload(self, data: dict[str, Any]) -> dict[str, Any]:
        name = str(data.get("name") or "").strip()
        raw = str(data.get("data") or "")
        if not name or not raw:
            raise BrainOpsError("缺少文件名或文件内容")
        try:
            payload = base64.b64decode(raw, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise BrainOpsError("文件内容传输失败，请重试") from exc
        if not payload:
            raise BrainOpsError("文件内容为空")
        if len(payload) > MAX_UPLOAD_BYTES:
            raise BrainOpsError("文件超过 15MB，请拆分后再上传")
        try:
            doc = self.service.upload(name, payload)
        except knowledge.DocumentParseError as exc:
            # 用户输入问题（格式不支持/文件损坏），不是服务端异常：不要打堆栈日志
            raise BrainOpsError(str(exc)) from exc
        if doc.get("status") == "failed":
            raise BrainOpsError(doc.get("error") or "文档解析失败")
        if doc.get("status") == "no_text":
            # 不算失败：文档已入库，但明确告知无法解析文字
            return {"document": _public_doc(doc), "warning": doc.get("error")}
        sync_errors = (doc.get("vector_sync") or {}).get("errors") or []
        if sync_errors:
            # 向量库不可用时上传仍然成功（业务优先），但必须告知用户
            # “这次能问，但暂时只能走关键词检索”，不能静默吞掉。
            return {
                "document": _public_doc(doc),
                "warning": "文档已入库，但向量索引写入失败，当前只能走关键词检索："
                           + str(sync_errors[0])[:200],
            }
        return {"document": _public_doc(doc)}

    async def op_documents_delete(self, data: dict[str, Any]) -> dict[str, Any]:
        doc_id = str(data.get("id") or "")
        if not doc_id:
            raise BrainOpsError("缺少文档 ID")
        if not self.service.delete_document(doc_id):
            raise BrainOpsError("文档不存在或已被删除")
        return {"deleted": doc_id}

    async def op_documents_chunks(self, data: dict[str, Any]) -> dict[str, Any]:
        doc_id = str(data.get("id") or "")
        chunks = self.service.store.chunks_of(doc_id)
        return {"chunks": chunks[:200], "total": len(chunks)}

    async def op_knowledge_query(self, data: dict[str, Any]) -> dict[str, Any]:
        question = str(data.get("question") or "").strip()
        if not question:
            raise BrainOpsError("请输入要查询的问题")
        doc_ids = {str(d) for d in (data.get("doc_ids") or []) if d}
        if doc_ids:
            hits = self.service.search(question, top_k=5, doc_ids=doc_ids)
            retrieval = getattr(self.service, "last_retrieval", None)
            if not hits:
                return {
                    "answer": "在这份文档中没有检索到相关内容。",
                    "citations": [],
                    "grounded": False,
                    "context_meta": {
                        "retrieved": 0,
                        "selected": 0,
                        "included": 0,
                        "estimated_tokens": 0,
                    },
                    "retrieval": retrieval,
                }
            from aieb.brain import knowledge as kb
            from aieb.brain import llm
            from aieb.brain.service import ANSWER_SYSTEM

            # 与全局问答共用 `service.build_context`：同一套 token 预算、同一份去重与
            # “不早停”策略。以前这里直接调 kb.build_context（字符预算 + 早停），
            # 导致单文档问答与全局问答的上下文口径不一致。
            context, context_meta = self.service.build_context(hits)
            usage: dict[str, int] = {}
            answer = await llm.complete(
                self.service.provider,
                ANSWER_SYSTEM,
                f"资料：\n{context}\n\n问题：{question}",
                usage_out=usage,
            )
            return {
                "answer": answer,
                "citations": kb.format_citations(hits),
                "grounded": True,
                "context_meta": context_meta,
                "usage": usage,
                # 必须带上检索轨迹：向量库/重排不可用时界面要能如实提示“已降级”
                "retrieval": retrieval,
            }
        return await self.service.answer(question)

    # ------------------------------------------------------------------
    # 会议
    # ------------------------------------------------------------------
    async def op_meetings_list(self, _data: dict[str, Any]) -> dict[str, Any]:
        return {"meetings": self.service.store.list_meetings()}

    async def op_meetings_get(self, data: dict[str, Any]) -> dict[str, Any]:
        meeting = self.service.store.get_meeting(str(data.get("id") or ""))
        if meeting is None:
            raise BrainOpsError("会议不存在或已被删除")
        return {"meeting": meeting}

    async def op_meetings_analyze(self, data: dict[str, Any]) -> dict[str, Any]:
        name = str(data.get("name") or "会议记录.txt").strip()
        text = data.get("text")
        if text is None and data.get("data"):
            try:
                raw = base64.b64decode(str(data["data"]), validate=True)
            except (binascii.Error, ValueError) as exc:
                raise BrainOpsError("文件内容传输失败，请重试") from exc
            if not raw:
                raise BrainOpsError("文件内容为空")
            if len(raw) > MAX_UPLOAD_BYTES:
                raise BrainOpsError("文件超过 15MB，请拆分后再上传")
            try:
                # 会议文件统一在这里转成纯文本：TXT / MD / DOCX / PDF
                text = knowledge.extract_document_text(name, raw)
            except knowledge.DocumentParseError as exc:
                raise BrainOpsError(str(exc)) from exc
        text = str(text or "")
        if len(text.strip()) < 30:
            raise BrainOpsError("会议记录内容过短（少于 30 字），无法分析")
        if len(text) > MAX_TEXT_CHARS:
            raise BrainOpsError("会议记录过长，请先拆分为多份")
        meeting = await self.service.analyze_meeting(name, text)
        # reused=True 表示同样的会议记录之前已经分析过，直接复用了旧结果，
        # 前端据此提示用户，而不是假装又分析了一遍。
        return {"meeting": meeting, "reused": bool(meeting.get("reused"))}

    async def op_meetings_delete(self, data: dict[str, Any]) -> dict[str, Any]:
        meeting_id = str(data.get("id") or "")
        if not self.service.store.delete_meeting(meeting_id):
            raise BrainOpsError("会议不存在或已被删除")
        return {"deleted": meeting_id}

    # ------------------------------------------------------------------
    # LangGraph 工作流（会议 → 风险/待办 → 邮件审批 → 发送）
    # ------------------------------------------------------------------
    async def op_workflow_start(self, data: dict[str, Any]) -> dict[str, Any]:
        """启动工作流；返回首个快照（通常停在“等待人工审批”）。"""
        name = str(data.get("name") or "会议记录.txt").strip()
        text = data.get("text")
        if text is None and data.get("data"):
            try:
                raw = base64.b64decode(str(data["data"]), validate=True)
            except (binascii.Error, ValueError) as exc:
                raise BrainOpsError("文件内容传输失败，请重试") from exc
            if not raw:
                raise BrainOpsError("文件内容为空")
            if len(raw) > MAX_UPLOAD_BYTES:
                raise BrainOpsError("文件超过 15MB，请拆分后再上传")
            try:
                text = knowledge.extract_document_text(name, raw)
            except knowledge.DocumentParseError as exc:
                raise BrainOpsError(str(exc)) from exc
        body = str(text or "")
        if len(body.strip()) < 30:
            raise BrainOpsError("会议记录内容过短（少于 30 字），无法分析")
        if len(body) > MAX_TEXT_CHARS:
            raise BrainOpsError("会议记录过长，请先拆分为多份")
        try:
            snapshot = await self.service.workflow.start(
                name=name,
                text=body,
                to_addr=str(data.get("to") or ""),
                extra_instruction=str(data.get("extra") or ""),
                session_id=str(data.get("session_id") or ""),
                allow_reuse=data.get("allow_reuse", True) is not False,
            )
        except Exception as exc:  # noqa: BLE001 - 工作流错误要给可读提示
            raise BrainOpsError(_workflow_message(exc)) from exc
        return {"workflow": snapshot.model_dump()}

    async def op_workflow_status(self, data: dict[str, Any]) -> dict[str, Any]:
        workflow_id = str(data.get("id") or "")
        if not workflow_id:
            raise BrainOpsError("缺少工作流 ID")
        try:
            snapshot = await self.service.workflow.status(workflow_id)
        except Exception as exc:  # noqa: BLE001
            raise BrainOpsError(_workflow_message(exc)) from exc
        return {"workflow": snapshot.model_dump()}

    async def op_workflow_resume(self, data: dict[str, Any]) -> dict[str, Any]:
        """人工审批：确认 / 修改 / 取消。版本与草稿指纹由前端回传。"""
        from aieb.workflow.state import ResumeCommand

        workflow_id = str(data.get("id") or "")
        if not workflow_id:
            raise BrainOpsError("缺少工作流 ID")
        decision = str(data.get("decision") or "").strip()
        if decision not in {"approve", "edit", "reject"}:
            raise BrainOpsError("审批动作只能是 approve / edit / reject")
        try:
            version = int(data.get("version") or 0)
        except (TypeError, ValueError) as exc:
            raise BrainOpsError("审批版本号非法") from exc
        draft_hash = str(data.get("draft_hash") or "")
        if not version or not draft_hash:
            raise BrainOpsError("审批需要带上草稿版本与指纹，请刷新后重试")
        command = ResumeCommand(
            decision=decision,  # type: ignore[arg-type]
            version=version,
            draft_hash=draft_hash,
            to_addr=str(data.get("to_addr") or ""),
            subject=str(data.get("subject") or ""),
            body=str(data.get("body") or ""),
            note=str(data.get("note") or ""),
        )
        edit: dict[str, Any] = {}
        if decision == "edit":
            edit = {
                "edit_to_addr": command.to_addr,
                "edit_subject": command.subject,
                "edit_body": command.body,
            }
        try:
            snapshot = await self.service.workflow.resume(workflow_id, command, edit=edit)
        except Exception as exc:  # noqa: BLE001
            raise BrainOpsError(_workflow_message(exc)) from exc
        return {"workflow": snapshot.model_dump()}

    async def op_workflow_cancel(self, data: dict[str, Any]) -> dict[str, Any]:
        workflow_id = str(data.get("id") or "")
        if not workflow_id:
            raise BrainOpsError("缺少工作流 ID")
        try:
            snapshot = await self.service.workflow.cancel(
                workflow_id, note=str(data.get("note") or "")
            )
        except Exception as exc:  # noqa: BLE001
            raise BrainOpsError(_workflow_message(exc)) from exc
        return {"workflow": snapshot.model_dump()}

    async def op_workflow_retry(self, data: dict[str, Any]) -> dict[str, Any]:
        """人工确认后重试发送（仅对“确定失败”放行）。"""
        workflow_id = str(data.get("id") or "")
        if not workflow_id:
            raise BrainOpsError("缺少工作流 ID")
        try:
            snapshot = await self.service.workflow.retry_send(workflow_id)
        except Exception as exc:  # noqa: BLE001
            raise BrainOpsError(_workflow_message(exc)) from exc
        return {"workflow": snapshot.model_dump()}

    async def op_workflow_list(self, data: dict[str, Any]) -> dict[str, Any]:
        limit = int(data.get("limit") or 20)
        return {"workflows": self.service.workflow.list(limit=limit)}

    async def op_workflow_history(self, data: dict[str, Any]) -> dict[str, Any]:
        workflow_id = str(data.get("id") or "")
        if not workflow_id:
            raise BrainOpsError("缺少工作流 ID")
        try:
            return self.service.workflow.history(workflow_id)
        except Exception as exc:  # noqa: BLE001
            raise BrainOpsError(_workflow_message(exc)) from exc

    # ------------------------------------------------------------------
    # 邮件
    # ------------------------------------------------------------------
    async def op_email_draft(self, data: dict[str, Any]) -> dict[str, Any]:
        meeting_id = str(data.get("meeting_id") or "")
        if not meeting_id:
            raise BrainOpsError("缺少会议 ID")
        draft = await self.service.draft_email(
            meeting_id,
            to_addr=str(data.get("to") or ""),
            extra=str(data.get("extra") or ""),
        )
        return {"email": draft}

    async def op_email_update(self, data: dict[str, Any]) -> dict[str, Any]:
        email_id = str(data.get("id") or "")
        stored = self.service.store.update_email(
            email_id,
            to_addr=data.get("to_addr"),
            subject=data.get("subject"),
            body=data.get("body"),
        )
        if stored is None:
            raise BrainOpsError("邮件草稿不存在")
        return {"email": stored}

    async def op_email_send(self, data: dict[str, Any]) -> dict[str, Any]:
        email_id = str(data.get("id") or "")
        if data.get("confirm") is not True:
            raise BrainOpsError("发送前需要用户确认")
        result = self.service.send_email(
            email_id,
            to_addr=data.get("to_addr"),
            subject=data.get("subject"),
            body=data.get("body"),
        )
        if not result.get("ok"):
            raise BrainOpsError(str(result.get("message") or "发送失败"))
        return result

    async def op_email_list(self, _data: dict[str, Any]) -> dict[str, Any]:
        return {"emails": self.service.store.list_emails()}

    async def op_email_config(self, _data: dict[str, Any]) -> dict[str, Any]:
        return {"mail": mailer.load_config().public_view()}

    # ------------------------------------------------------------------
    # 待办
    # ------------------------------------------------------------------
    async def op_todos_list(self, data: dict[str, Any]) -> dict[str, Any]:
        return {
            "todos": self.service.store.list_todos(
                status=data.get("status"), priority=data.get("priority")
            )
        }

    async def op_todos_update(self, data: dict[str, Any]) -> dict[str, Any]:
        todo_id = str(data.get("id") or "")
        status = data.get("status")
        if status is not None and status not in {"pending", "doing", "done"}:
            raise BrainOpsError("状态不合法")
        todo = self.service.store.update_todo(
            todo_id,
            status=status,
            owner=data.get("owner"),
            due_date=data.get("due_date"),
            priority=data.get("priority"),
        )
        if todo is None:
            raise BrainOpsError("待办不存在或已被删除")
        self.service.store.add_activity(
            "todo", f"更新待办状态：{todo['content'][:40]}", str(status or "")
        )
        return {"todo": todo}

    async def op_todos_create(self, data: dict[str, Any]) -> dict[str, Any]:
        content = str(data.get("content") or "").strip()
        if not content:
            raise BrainOpsError("待办内容不能为空")
        todo = self.service.store.add_todo(
            content=content,
            owner=str(data.get("owner") or ""),
            due_date=str(data.get("due_date") or ""),
            priority=str(data.get("priority") or "medium"),
            meeting_id=data.get("meeting_id"),
        )
        return {"todo": todo}

    async def op_todos_delete(self, data: dict[str, Any]) -> dict[str, Any]:
        """删除待办：支持单条（id）与批量（ids）。"""
        ids = [str(x) for x in (data.get("ids") or []) if x]
        single = str(data.get("id") or "")
        if single and single not in ids:
            ids.append(single)
        if not ids:
            raise BrainOpsError("缺少待办 ID")
        deleted = self.service.delete_todos(ids)
        if deleted == 0:
            raise BrainOpsError("待办不存在或已被删除")
        return {"deleted": deleted}

    # ------------------------------------------------------------------
    # 长期记忆
    # ------------------------------------------------------------------
    async def op_memory_list(self, _data: dict[str, Any]) -> dict[str, Any]:
        return {"memories": self.service.memory_items()}

    async def op_memory_delete(self, data: dict[str, Any]) -> dict[str, Any]:
        """删除记忆：支持单条（id）与批量（ids）。"""
        ids = [str(x) for x in (data.get("ids") or []) if x]
        single = str(data.get("id") or "")
        if single and single not in ids:
            ids.append(single)
        if not ids:
            raise BrainOpsError("缺少记忆 ID")
        deleted, failed = self.service.delete_memories(ids)
        if deleted == 0:
            raise BrainOpsError("找不到这条记忆" if len(ids) == 1 else "没有可删除的记忆")
        return {"ok": True, "deleted": deleted, "failed": failed}

    # ------------------------------------------------------------------
    # PPT
    # ------------------------------------------------------------------
    async def op_ppt_generate(self, data: dict[str, Any]) -> dict[str, Any]:
        topic = str(data.get("topic") or "").strip()
        if not topic:
            raise BrainOpsError("请描述 PPT 主题")
        try:
            slides = int(data.get("slides") or 8)
        except (TypeError, ValueError):
            slides = 8
        slides = max(3, min(20, slides))
        result = await self.service.make_ppt(
            topic=topic,
            slide_count=slides,
            focus=str(data.get("focus") or ""),
            audience=str(data.get("audience") or ""),
        )
        return result


def _public_doc(doc: dict[str, Any]) -> dict[str, Any]:
    """去掉本地路径等敏感字段。"""
    item = {k: v for k, v in doc.items() if k != "stored_path"}
    return item
