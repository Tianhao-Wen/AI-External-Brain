"""把外脑业务能力注册成 Agent 工具，让自然语言对话可以直接驱动业务。

命名刻意贴近业务（知识库检索 / 会议分析 / 汇报材料 / 待办 / 邮件），
而不是暴露内部实现细节。
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from aieb.brain import knowledge
from aieb.brain.service import BrainService
from aieb.brain.tools_guard import describe_error
from aieb.tools.base import Tool, ToolResult


class SearchKnowledgeParams(BaseModel):
    query: str = Field(description="要在已上传知识文档中检索的问题或关键词")
    top_k: int = Field(default=5, description="返回的引用片段数量，1-8")


class SearchKnowledge(Tool):
    name = "SearchKnowledge"
    description = (
        "在企业知识库（用户上传的 PDF/TXT 文档）中检索并回答问题。"
        "回答会附带文档名与页码引用；知识库中没有的内容必须如实说明。"
    )
    params_model = SearchKnowledgeParams
    category = "read"
    is_concurrency_safe = True

    def __init__(self, service: BrainService) -> None:
        self._service = service

    async def execute(self, params: SearchKnowledgeParams) -> ToolResult:
        top_k = max(1, min(8, int(params.top_k or 5)))
        try:
            result = await self._service.answer(params.query, top_k=top_k)
        except Exception as exc:  # noqa: BLE001
            return ToolResult(output=describe_error(exc, "知识库检索失败"), is_error=True)
        lines = [result["answer"]]
        citations = result.get("citations") or []
        if citations:
            lines.append("\n参考来源：")
            for c in citations:
                lines.append(f"- {c['doc_name']} 第{c['page']}页")
        return ToolResult(output="\n".join(lines))


class AnalyzeMeetingParams(BaseModel):
    file_path: str = Field(
        default="", description="会议记录文件路径（相对工作目录），支持 .txt/.md/.docx/.pdf"
    )
    meeting_text: str = Field(default="", description="直接给出的会议记录正文（可选）")
    title: str = Field(default="", description="会议名称（可选，缺省用文件名）")


class AnalyzeMeeting(Tool):
    name = "AnalyzeMeeting"
    description = (
        "分析会议记录，抽取主题、时间、地点、参与人、决策、风险与待办，并保存到会议中心。"
        "负责人/截止时间原文没有写就标记为待确认，不要编造。"
    )
    params_model = AnalyzeMeetingParams
    category = "read"

    def __init__(self, service: BrainService, work_dir: str | None = None) -> None:
        self._service = service
        self.work_dir = work_dir

    async def execute(self, params: AnalyzeMeetingParams) -> ToolResult:
        text = params.meeting_text or ""
        name = params.title or "会议记录.txt"
        if not text.strip() and params.file_path:
            root = Path(self.work_dir or ".").resolve()
            candidate = Path(params.file_path)
            path = candidate if candidate.is_absolute() else root / candidate
            try:
                path = path.resolve()
                path.relative_to(root)
            except (OSError, ValueError):
                return ToolResult(output="只能分析工作目录内的会议文件", is_error=True)
            if not path.is_file():
                return ToolResult(output=f"找不到会议文件：{params.file_path}", is_error=True)
            try:
                text = knowledge.extract_document_text(path.name, path.read_bytes())
            except knowledge.DocumentParseError as exc:
                return ToolResult(output=str(exc), is_error=True)
            except OSError as exc:
                return ToolResult(output=f"读取会议文件失败：{exc}", is_error=True)
            name = params.title or path.name
        if len(text.strip()) < 30:
            return ToolResult(output="会议记录内容过短（少于 30 字），无法分析", is_error=True)
        try:
            meeting = await self._service.analyze_meeting(name, text)
        except Exception as exc:  # noqa: BLE001
            return ToolResult(output=describe_error(exc, "会议分析失败"), is_error=True)

        risks = meeting.get("risks") or []
        todos = meeting.get("todos") or []
        lines = [
            f"会议「{meeting['title']}」分析完成（编号 {meeting['id']}）。",
            f"- 时间：{meeting.get('meeting_time') or '待确认'}",
            f"- 地点：{meeting.get('location') or '待确认'}",
            f"- 参与人：{('、'.join(meeting.get('participants') or [])) or '待确认'}",
            f"- 结论：{meeting.get('summary') or '（无）'}",
            f"- 风险 {len(risks)} 条，待办 {len(todos)} 条，已存入会议中心。",
        ]
        for risk in risks[:5]:
            lines.append(f"  · [{risk['level']}] {risk['title']}")
        for todo in todos[:5]:
            lines.append(
                f"  · 待办：{todo['content']}（负责人 {todo.get('owner') or '待确认'}，"
                f"截止 {todo.get('due_date') or '待确认'}）"
            )
        return ToolResult(output="\n".join(lines))


def _meeting_blob(meeting: dict[str, Any]) -> str:
    """把一场会议的可检索字段拼成一个字符串，供关键词匹配。"""
    parts = [
        str(meeting.get("title") or ""),
        str(meeting.get("meeting_time") or ""),
        str(meeting.get("location") or ""),
        str(meeting.get("source_name") or ""),
        str(meeting.get("summary") or ""),
        " ".join(meeting.get("participants") or []),
    ]
    return " ".join(parts)


def _match_meetings(service: BrainService, query: str, limit: int = 10) -> list[dict[str, Any]]:
    """按关键字在会议中心里匹配会议（没有任何关键字就返回最近的会议）。

    中文没有空格分词，所以除了整体子串匹配，还把 query 按标点/空格切成若干词，
    只要命中任意一个词就计入候选，避免「2025-10-28 华瑞精密」这类复合查询匹配不上。
    """
    meetings = service.store.list_meetings(limit=50)
    text = (query or "").strip()
    if not text:
        return meetings[:limit]
    terms = [t for t in re.split(r"[\s，,。;；、/|()（）\[\]【】]+", text) if t]
    lowered = text.lower()
    scored: list[tuple[int, dict[str, Any]]] = []
    for meeting in meetings:
        blob = _meeting_blob(meeting).lower()
        score = 0
        if lowered in blob:
            score += 5
        for term in terms:
            if len(term) >= 2 and term.lower() in blob:
                score += 2
        if score:
            scored.append((score, meeting))
    scored.sort(key=lambda item: item[0], reverse=True)
    return [m for _, m in scored[:limit]]


class ListMeetingsParams(BaseModel):
    query: str = Field(
        default="",
        description="筛选关键字（会议名/公司名/日期/参与人），留空表示列出最近的会议",
    )
    limit: int = Field(default=10, description="最多返回条数（1-20）")


class ListMeetings(Tool):
    name = "ListMeetings"
    description = (
        "列出会议中心里已经保存的会议（用户在「会议中心」上传或之前分析过的会议记录）。"
        "用户提到某场会议、某个日期、某家公司却没说文件在哪时，先调用它确认是否已有记录；"
        "不要再回答「没有附上会议记录」。"
    )
    params_model = ListMeetingsParams
    category = "read"
    is_concurrency_safe = True

    def __init__(self, service: BrainService) -> None:
        self._service = service

    async def execute(self, params: ListMeetingsParams) -> ToolResult:
        limit = max(1, min(20, int(params.limit or 10)))
        try:
            meetings = _match_meetings(self._service, params.query or "", limit=limit)
        except Exception as exc:  # noqa: BLE001
            return ToolResult(output=describe_error(exc, "读取会议中心失败"), is_error=True)
        if not meetings:
            scope = f"符合「{params.query}」的" if (params.query or "").strip() else ""
            return ToolResult(
                output=f"会议中心里没有{scope}会议记录（共 0 场匹配）。"
            )
        lines = [f"会议中心共有 {len(meetings)} 场匹配的会议："]
        for m in meetings:
            lines.append(
                f"- [{m['id']}] {m.get('title') or '未命名会议'}"
                f"（时间 {m.get('meeting_time') or '待确认'}，"
                f"来源文件 {m.get('source_name') or '未知'}，"
                f"风险 {m.get('risk_count', 0)} 条 / 待办 {m.get('todo_count', 0)} 条）"
            )
        lines.append("需要细节时用 GetMeeting 传入会议编号。")
        return ToolResult(output="\n".join(lines))


class GetMeetingParams(BaseModel):
    meeting_id: str = Field(default="", description="会议编号（ListMeetings 返回的 id）")
    query: str = Field(
        default="",
        description="没有编号时用的关键字（会议名/公司名/日期/参与人），取匹配度最高的一场",
    )
    include_raw: bool = Field(default=True, description="是否附带会议记录原文片段")
    raw_limit: int = Field(default=4000, description="原文最多返回的字符数（500-8000）")


class GetMeeting(Tool):
    name = "GetMeeting"
    description = (
        "读取会议中心里某场已保存会议的完整详情：时间地点、参与人、结论、决策、"
        "风险（含原文依据）、待办（负责人/截止时间），并可附带原始会议记录片段。"
        "这是引用已上传会议记录的正规途径。"
    )
    params_model = GetMeetingParams
    category = "read"
    is_concurrency_safe = True

    def __init__(self, service: BrainService) -> None:
        self._service = service

    async def execute(self, params: GetMeetingParams) -> ToolResult:
        try:
            meeting = None
            if params.meeting_id.strip():
                meeting = self._service.store.get_meeting(params.meeting_id.strip())
            if meeting is None:
                candidates = _match_meetings(self._service, params.query or "", limit=1)
                if candidates:
                    meeting = self._service.store.get_meeting(candidates[0]["id"])
        except Exception as exc:  # noqa: BLE001
            return ToolResult(output=describe_error(exc, "读取会议详情失败"), is_error=True)
        if not meeting:
            return ToolResult(
                output="会议中心里没有找到这场会议（不要编造会议结论，请让用户确认会议名称或上传记录）。"
            )

        lines = [
            f"会议：{meeting.get('title') or '未命名会议'}（编号 {meeting['id']}）",
            f"- 时间：{meeting.get('meeting_time') or '待确认'}",
            f"- 地点：{meeting.get('location') or '待确认'}",
            f"- 参与人：{('、'.join(meeting.get('participants') or [])) or '待确认'}",
            f"- 来源文件：{meeting.get('source_name') or '未知'}",
            f"- 结论：{meeting.get('summary') or '（无）'}",
        ]
        decisions = meeting.get("decisions") or []
        if decisions:
            lines.append("- 已确认决策：")
            lines.extend(f"  · {d}" for d in decisions)
        discussion = meeting.get("discussion") or []
        if discussion:
            lines.append("- 讨论要点：")
            lines.extend(f"  · {d}" for d in discussion)

        risks = meeting.get("risks") or []
        lines.append(f"- 风险（{len(risks)} 条）：")
        if not risks:
            lines.append("  · 无")
        for risk in risks:
            lines.append(
                f"  · [{risk.get('level') or 'medium'}] {risk.get('title') or ''}"
                f"｜说明：{risk.get('description') or '（无）'}"
            )
            if risk.get("evidence"):
                lines.append(f"    原文依据：{risk['evidence']}")
            if risk.get("suggestion"):
                lines.append(f"    建议措施：{risk['suggestion']}")

        todos = meeting.get("todos") or []
        lines.append(f"- 待办（{len(todos)} 条）：")
        if not todos:
            lines.append("  · 无")
        for todo in todos:
            lines.append(
                f"  · {todo.get('content') or ''}（负责人 {todo.get('owner') or '待确认'}，"
                f"截止 {todo.get('due_date') or '待确认'}，优先级 {todo.get('priority') or 'medium'}，"
                f"状态 {todo.get('status') or 'pending'}）"
            )

        if params.include_raw:
            limit = max(500, min(8000, int(params.raw_limit or 4000)))
            raw = ""
            try:
                raw = self._service.store.get_meeting_raw_text(meeting["id"])
            except Exception:  # noqa: BLE001 - 原文只是补充材料，取不到不影响主结果
                raw = ""
            if raw:
                snippet = raw[:limit]
                suffix = "……（原文较长，已截断）" if len(raw) > limit else ""
                lines.append("\n【会议记录原文片段】\n" + snippet + suffix)
        return ToolResult(output="\n".join(lines))


class CreatePresentationParams(BaseModel):
    topic: str = Field(description="PPT 主题，例如「第三季度风险复盘汇报」")
    slides: int = Field(default=8, description="总页数（含封面），3-20")
    focus: str = Field(default="", description="内容重点（可选）")
    audience: str = Field(default="", description="汇报对象（可选）")


class CreatePresentation(Tool):
    name = "CreatePresentation"
    description = (
        "根据主题（可结合知识库与会议结论）生成真正的 .pptx 汇报文件，"
        "生成后会在回复中提供下载卡片。"
    )
    params_model = CreatePresentationParams
    category = "write"

    def __init__(self, service: BrainService, work_dir: str | None = None) -> None:
        self._service = service
        self.work_dir = work_dir

    async def execute(self, params: CreatePresentationParams) -> ToolResult:
        slides = max(3, min(20, int(params.slides or 8)))
        try:
            result = await self._service.make_ppt(
                topic=params.topic,
                slide_count=slides,
                focus=params.focus,
                audience=params.audience,
            )
        except Exception as exc:  # noqa: BLE001
            return ToolResult(output=describe_error(exc, "PPT 生成失败"), is_error=True)

        source = Path(str(result.get("_path") or ""))
        root = Path(self.work_dir or ".").resolve()
        out_dir = root / "汇报材料"
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
            target = out_dir / str(result["filename"])
            if source.is_file() and source != target:
                shutil.copy2(source, target)
        except OSError as exc:
            return ToolResult(output=f"PPT 已生成但导出到工作目录失败：{exc}", is_error=True)
        return ToolResult(
            output=(
                f"已生成 PPT《{params.topic}》，共 {result['slide_count']} 页，"
                f"文件名：{result['filename']}（存放于 汇报材料/ 目录）。"
            )
        )


class ListOpenTodosParams(BaseModel):
    status: str = Field(default="open", description="open=未完成，done=已完成，all=全部")
    limit: int = Field(default=20, description="最多返回条数")


class ListOpenTodos(Tool):
    name = "ListOpenTodos"
    description = "列出待办事项（含来源会议、负责人、截止时间、优先级、完成状态）。"
    params_model = ListOpenTodosParams
    category = "read"
    is_concurrency_safe = True

    def __init__(self, service: BrainService) -> None:
        self._service = service

    async def execute(self, params: ListOpenTodosParams) -> ToolResult:
        status = (params.status or "open").strip().lower()
        if status == "open":
            todos = [t for t in self._service.store.list_todos() if t["status"] != "done"]
        elif status == "done":
            todos = self._service.store.list_todos(status="done")
        else:
            todos = self._service.store.list_todos()
        todos = todos[: max(1, min(100, int(params.limit or 20)))]
        if not todos:
            return ToolResult(output="当前没有符合条件的待办事项。")
        lines = []
        for todo in todos:
            lines.append(
                f"- [{todo['status']}/{todo['priority']}] {todo['content']}"
                f"（负责人 {todo.get('owner') or '待确认'}，截止 {todo.get('due_date') or '待确认'}，"
                f"来源：{todo.get('meeting_title') or '手动创建'}）"
            )
        return ToolResult(output="\n".join(lines))


class DraftReminderEmailParams(BaseModel):
    meeting_id: str = Field(description="会议编号（AnalyzeMeeting 返回的 id）")
    to: str = Field(default="", description="收件人邮箱（可选，留空由用户填写）")
    extra: str = Field(default="", description="对邮件内容的额外要求（可选）")


class DraftReminderEmail(Tool):
    name = "DraftReminderEmail"
    description = (
        "为某次会议生成提醒邮件草稿（包含风险与待办）。"
        "只生成草稿，绝不自动发送——必须由用户在聊天里的草稿卡片上确认后发送。"
    )
    params_model = DraftReminderEmailParams
    category = "write"

    def __init__(self, service: BrainService) -> None:
        self._service = service

    async def execute(self, params: DraftReminderEmailParams) -> ToolResult:
        try:
            draft = await self._service.draft_email(
                params.meeting_id, to_addr=params.to, extra=params.extra
            )
        except KeyError:
            return ToolResult(output="找不到该会议，请先完成会议分析", is_error=True)
        except Exception as exc:  # noqa: BLE001
            return ToolResult(output=describe_error(exc, "邮件草稿生成失败"), is_error=True)
        # 机器可读标记：服务端据此在聊天里渲染真正的草稿卡片（模型看不到正文之外的细节）。
        marker = EMAIL_DRAFT_MARKER + json.dumps(
            {
                "id": draft.get("id", ""),
                "meeting_id": draft.get("meeting_id", params.meeting_id),
                "to_addr": draft.get("to_addr", ""),
                "subject": draft.get("subject", ""),
                "body": draft.get("body", ""),
                "status": draft.get("status", "draft"),
                "related_todos": self._related_todos(params.meeting_id),
            },
            ensure_ascii=False,
        )
        return ToolResult(
            output=(
                "已生成提醒邮件草稿（尚未发送，等待用户在卡片上确认）：\n"
                f"收件人：{draft.get('to_addr') or '待填写'}\n"
                f"主题：{draft['subject']}\n\n"
                f"{draft['body']}\n\n"
                "请在聊天里的邮件草稿卡片上核对并点击「确认发送」；"
                "不要声称已经发出，也不要让用户去别的页面找发送入口。\n"
                + marker
            )
        )

    def _related_todos(self, meeting_id: str) -> list[dict[str, str]]:
        """草稿关联的待办（让用户一眼看出这封邮件在催什么）。"""
        try:
            meeting = self._service.store.get_meeting(meeting_id) or {}
        except Exception:  # noqa: BLE001 - 只是展示用，失败不应影响草稿
            return []
        items = []
        for todo in (meeting.get("todos") or [])[:8]:
            items.append({
                "content": str(todo.get("content") or ""),
                "owner": str(todo.get("owner") or "待确认"),
                "due_date": str(todo.get("due_date") or "待确认"),
            })
        return items


#: 工具输出里的机器可读标记前缀（服务端解析后会在展示时去掉，不会给用户看）
EMAIL_DRAFT_MARKER = "[[AIEB_EMAIL_DRAFT]]"


class RecallMemoryParams(BaseModel):
    query: str = Field(
        default="",
        description="要回忆的主题/人物/会议名；留空表示返回最近的记忆",
    )
    limit: int = Field(default=8, description="最多返回条数（1-20）")


class RecallMemory(Tool):
    name = "RecallMemory"
    description = (
        "检索跨会话的长期记忆：历史会议的时间、地点、参与人、决策、待办，"
        "以及对话中沉淀的偏好与结论。用户问「上次会议」「之前讨论过什么」时先调用它，"
        "并在回答里注明来源会议；查不到就如实说没有记录。"
    )
    params_model = RecallMemoryParams
    category = "read"
    is_concurrency_safe = True

    def __init__(self, service: BrainService) -> None:
        self._service = service

    async def execute(self, params: RecallMemoryParams) -> ToolResult:
        limit = max(1, min(20, int(params.limit or 8)))
        try:
            items = self._service.recall(params.query or "", limit=limit)
        except Exception as exc:  # noqa: BLE001
            return ToolResult(output=describe_error(exc, "记忆检索失败"), is_error=True)
        if not items:
            return ToolResult(
                output="长期记忆里没有找到与该查询相关的记录（不要编造会议或结论）。"
            )
        lines = [f"长期记忆检索结果（共 {len(items)} 条，来自历史会话/会议）："]
        for item in items:
            lines.append(f"- [{item['category']}] {item['content']}（来源：{item['source']}）")
        return ToolResult(output="\n".join(lines))


def register_brain_tools(registry, service: BrainService, work_dir: str | None = None) -> list[str]:
    """把外脑工具注册进 ToolRegistry，返回实际注册的工具名。"""
    tools: list[Tool] = [
        SearchKnowledge(service),
        ListMeetings(service),
        GetMeeting(service),
        AnalyzeMeeting(service, work_dir),
        CreatePresentation(service, work_dir),
        ListOpenTodos(service),
        RecallMemory(service),
        DraftReminderEmail(service),
    ]
    for tool in tools:
        registry.register(tool)
    return [t.name for t in tools]
