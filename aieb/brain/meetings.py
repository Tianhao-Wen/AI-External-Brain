"""会议分析：LLM 结构化抽取 + 校验 + 持久化 + 记忆事实沉淀。

三点硬要求
----------
1. **不编造**：原文没有写负责人/截止时间/依据时，字段留空，界面显示"待确认"。
   即使模型写了，只要该字符串在原文中找不到，也会被清空。
2. **可追溯**：每个风险带 `evidence`（原文依据），每条记忆事实带来源会议。
3. **结构化**：输出经 Pydantic 校验，字段类型不对会整体重试一次，仍失败则报错，
   绝不把半结构化文本硬塞进卡片。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError, field_validator

from aieb.brain import llm, risk_rules
from aieb.brain.store import BrainStore
from aieb.config import ProviderConfig

PENDING = "待确认"
log = logging.getLogger(__name__)

ANALYSIS_SYSTEM = """你是企业会议纪要分析助手。你只根据用户提供的会议原文抽取信息。
绝对规则：
1. 不得编造原文中不存在的人物、时间、地点、金额、结论。
2. 负责人（owner）与截止时间（due_date）必须原文明确写出才能填写；否则一律填空字符串 ""。
3. 每条风险必须给出原文依据（evidence），必须是原文中的**连续片段**，逐字复制，不要改写、不要拼接。
4. 只输出一个 JSON 对象，不要输出解释文字、不要加 markdown 代码块以外的内容。
5. 全部使用简体中文。

【什么才算风险】必须同时满足三条，缺一不可：
   (a) 原文明确写出了一件**已经存在或明确将要发生**的不利事实（落后、超支、不达标、缺陷、缺口、违规、争议…）；
   (b) 这件事**尚未解决**；
   (c) 依据里能读到这件不利事实本身，而不是只读到"要小心""需要评审""张伟提醒"这类**要求或提醒**。
以下情况**不要**记为风险：
   - 已经解决、已经关闭、已经恢复、已经通过、未再复现的问题（哪怕本周刚解决）；
   - 会议中的规范提醒、流程要求、行为约束（例如"必须走变更评审""不要承诺交付日期"）；
   - 仅仅是背景说明、原因解释、进度更新、工作安排或"重点关注"事项；
   - 仅用于举例的假设（"如果当初…"）以及假设条件下的损失测算；
   - 尚在讨论、尚未决定的待评估事项（用 status="uncertain" 表示，不要写成具体风险事实）；
   - 已被否定、撤回、顺延的旧决议（那属于决策变更，不是风险）；
   - 同一件事的多个侧面：只保留一条，写在最具体的那条里，不要拆成多条。

【severity 判定标准】必须按下列锚点选择，不要凭感觉：
   high   ：已经造成或必然造成交付延误、合规处罚、安全/质量事故，或有明确金额/比例突破制度阈值；
   medium ：问题明确存在、影响可控，但如果不处理会在本周期内造成明显后果；
   low    ：影响有限，属于需要跟进观察的隐患。
   "已解决""已关闭"的事项一律不得标为 high。

JSON 结构：
{
  "title": "会议主题",
  "meeting_time": "原文中的时间，没有则空字符串",
  "location": "原文中的地点/会议形式，没有则空字符串",
  "participants": ["姓名1", "姓名2"],
  "discussion": ["核心讨论点1", "核心讨论点2"],
  "decisions": ["已确认的决策1"],
  "summary": "3-5 句话的会议结论概述",
  "risks": [
    {"title": "风险标题", "risk_type": "进度|成本|质量|技术|资源|需求变更|合规|供应商",
     "level": "high|medium|low", "status": "open|resolved|uncertain",
     "description": "风险说明", "evidence": "原文连续片段", "suggestion": "建议措施"}
  ],
  "todos": [
    {"content": "待办内容", "owner": "负责人或空字符串", "due_date": "截止时间或空字符串",
     "priority": "high|medium|low"}
  ]
}"""


class RiskIn(BaseModel):
    title: str = ""
    level: Literal["high", "medium", "low"] = "medium"
    # 以下三个字段是"判定依据"，用于确定性校验与可解释性；
    # 旧数据没有这些键时按默认值处理，不需要数据库迁移。
    risk_type: str = ""
    status: Literal["open", "resolved", "uncertain"] = "open"
    description: str = ""
    evidence: str = ""
    suggestion: str = ""

    @field_validator("level", mode="before")
    @classmethod
    def _normalize_level(cls, value: Any) -> str:
        text = str(value or "medium").strip().lower()
        if text in {"high", "高", "高风险", "严重", "critical"}:
            return "high"
        if text in {"low", "低", "低风险", "轻微"}:
            return "low"
        return "medium"

    @field_validator("status", mode="before")
    @classmethod
    def _normalize_status(cls, value: Any) -> str:
        text = str(value or "open").strip().lower()
        if text in {"resolved", "closed", "已解决", "已关闭", "已修复"}:
            return "resolved"
        if text in {"uncertain", "pending", "待确认", "待定", "讨论中"}:
            return "uncertain"
        return "open"


class TodoIn(BaseModel):
    content: str = ""
    owner: str = ""
    due_date: str = ""
    priority: Literal["high", "medium", "low"] = "medium"

    @field_validator("priority", mode="before")
    @classmethod
    def _normalize_priority(cls, value: Any) -> str:
        text = str(value or "medium").strip().lower()
        if text in {"high", "高", "紧急", "p0", "p1"}:
            return "high"
        if text in {"low", "低", "p3"}:
            return "low"
        return "medium"


class MeetingAnalysis(BaseModel):
    title: str = ""
    meeting_time: str = ""
    location: str = ""
    participants: list[str] = Field(default_factory=list)
    discussion: list[str] = Field(default_factory=list)
    decisions: list[str] = Field(default_factory=list)
    summary: str = ""
    risks: list[RiskIn] = Field(default_factory=list)
    todos: list[TodoIn] = Field(default_factory=list)

    @field_validator("participants", "discussion", "decisions", mode="before")
    @classmethod
    def _coerce_list(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return [value] if value.strip() else []
        if isinstance(value, list):
            return [str(v).strip() for v in value if str(v).strip()]
        return []


class MeetingAnalysisError(RuntimeError):
    """会议分析失败（原文过短、模型输出不合规等）。"""


def _squash(text: str) -> str:
    """去掉空白与标点，用于判断某段文字是否真的出现在原文里。"""
    return re.sub(r"[\s\u3000，。、；：！？,.;:!?\"'“”‘’（）()\-—_~·]+", "", text or "")


def _grounded(value: str, raw_squashed: str) -> bool:
    candidate = _squash(value)
    if not candidate:
        return False
    return candidate in raw_squashed


def analyze(
    provider: ProviderConfig,
    raw_text: str,
    *,
    source_name: str = "",
) -> MeetingAnalysis:
    """同步入口：内部用 asyncio.run，供工具线程或后台任务调用。"""
    import asyncio

    return asyncio.run(analyze_async(provider, raw_text, source_name=source_name))


def build_analysis_prompt(raw_text: str, source_name: str = "") -> str:
    """构造会议分析 prompt（原路径与 LangChain 适配层共用，避免两套 prompt 漂移）。"""
    text = (raw_text or "").strip()
    # 控制上下文长度：保留开头与结尾，避免超长会议记录一次性打爆上下文
    if len(text) > 24000:
        text = text[:18000] + "\n\n……（中间内容略）……\n\n" + text[-6000:]
    return f"会议记录文件名：{source_name or '未命名'}\n\n会议原文如下：\n\n{text}"


async def analyze_async(
    provider: ProviderConfig,
    raw_text: str,
    *,
    source_name: str = "",
    usage_out: dict[str, int] | None = None,
) -> MeetingAnalysis:
    text = (raw_text or "").strip()
    if len(text) < 30:
        raise MeetingAnalysisError("会议记录内容过短（少于 30 字），无法分析")

    prompt = build_analysis_prompt(text, source_name)
    last_error: Exception | None = None
    for _ in range(2):
        try:
            data = await llm.complete_json(
                provider, ANALYSIS_SYSTEM, prompt, usage_out=usage_out
            )
            return MeetingAnalysis.model_validate(data)
        except (llm.LLMError, ValidationError) as exc:
            last_error = exc
            continue
    raise MeetingAnalysisError(f"会议分析失败：{last_error}")


def sanitize(analysis: MeetingAnalysis, raw_text: str) -> MeetingAnalysis:
    """清掉任何在原文中找不到的"事实"，再用确定性规则过滤无效风险。

    两步：
    1. **接地校验**（原有逻辑）：负责人/截止时间/依据/参与人只要在原文找不到就清空，
       保证不编造。
    2. **风险有效性校验**（新增）：把"已解决的事项、规范提醒、工作安排、假设测算、
       待评估事项、总结句"从候选风险里剔除，并把重复条目合并。
       规则见 ``aieb.brain.risk_rules``，每条丢弃都带原因，便于复核。
    """
    raw_squashed = _squash(raw_text)

    participants = [p for p in analysis.participants if _grounded(p, raw_squashed)]
    candidate_risks: list[dict[str, Any]] = []
    for risk in analysis.risks:
        if not risk.title.strip() and not risk.description.strip():
            continue
        evidence = risk.evidence if _grounded(risk.evidence, raw_squashed) else ""
        candidate_risks.append(
            {
                "title": risk.title.strip() or "未命名风险",
                "level": risk.level,
                "risk_type": (risk.risk_type or "").strip(),
                "status": risk.status,
                "description": risk.description.strip(),
                "evidence": evidence,
                "suggestion": risk.suggestion.strip(),
            }
        )

    kept, dropped = risk_rules.filter_risks(candidate_risks, raw_text)
    risks = [RiskIn(**item) for item in kept]
    if dropped:
        log.info(
            "[meeting] 风险候选 %d 条，采用 %d 条，丢弃 %d 条：%s",
            len(candidate_risks),
            len(kept),
            len(dropped),
            [(d.get("title"), d.get("dropped_reason")) for d in dropped],
        )

    todos: list[TodoIn] = []
    for todo in analysis.todos:
        content = todo.content.strip()
        if not content:
            continue
        owner = todo.owner.strip()
        if owner and not _grounded(owner, raw_squashed):
            owner = ""  # 原文没这个人出现 → 判定为模型编造，退回待确认
        due = todo.due_date.strip()
        if due and not (_grounded(due, raw_squashed) or _grounded(due.rstrip("日号"), raw_squashed)):
            due = ""
        todos.append(TodoIn(content=content, owner=owner, due_date=due, priority=todo.priority))

    return MeetingAnalysis(
        title=analysis.title.strip() or (source_name or "未命名会议"),
        meeting_time=analysis.meeting_time.strip() if _grounded(analysis.meeting_time, raw_squashed) else "",
        location=analysis.location.strip() if _grounded(analysis.location, raw_squashed) else "",
        participants=participants,
        discussion=[d for d in analysis.discussion if d.strip()],
        decisions=[d for d in analysis.decisions if d.strip()],
        summary=analysis.summary.strip(),
        risks=risks,
        todos=todos,
    )


def save(store: BrainStore, analysis: MeetingAnalysis, raw_text: str, source_name: str) -> dict[str, Any]:
    """落库：会议 + 风险 + 待办 + 结构化记忆事实。"""
    meeting = store.add_meeting(
        title=analysis.title,
        meeting_time=analysis.meeting_time,
        location=analysis.location,
        participants=analysis.participants,
        summary=analysis.summary,
        decisions=analysis.decisions,
        discussion=analysis.discussion,
        source_name=source_name,
        raw_text=raw_text,
        risks=[r.model_dump() for r in analysis.risks],
        todos=[t.model_dump() for t in analysis.todos],
    )
    _persist_memory_facts(store, meeting, analysis)
    store.add_activity(
        "meeting",
        f"完成会议分析：{analysis.title}",
        f"{len(analysis.risks)} 条风险 / {len(analysis.todos)} 条待办",
    )
    return meeting


def _persist_memory_facts(store: BrainStore, meeting: dict[str, Any], analysis: MeetingAnalysis) -> None:
    """把会议关键要素写成结构化记忆，供跨会话长期记忆页面与对话检索使用。"""
    facts: list[dict[str, Any]] = []
    base = {
        "source_kind": "meeting",
        "source_id": meeting["id"],
        "source_label": f"会议「{meeting['title']}」",
    }
    for person in analysis.participants:
        facts.append({"category": "person", "content": f"{person} 参加了「{meeting['title']}」", **base})
    if analysis.meeting_time:
        facts.append({"category": "time", "content": f"「{meeting['title']}」时间：{analysis.meeting_time}", **base})
    if analysis.location:
        facts.append({"category": "place", "content": f"「{meeting['title']}」地点：{analysis.location}", **base})
    facts.append({"category": "topic", "content": f"会议主题：{meeting['title']}", **base})
    for decision in analysis.decisions:
        facts.append({"category": "decision", "content": f"决策：{decision}", **base})
    for todo in analysis.todos:
        owner = todo.owner or PENDING
        due = todo.due_date or PENDING
        facts.append({
            "category": "todo",
            "content": f"待办：{todo.content}（负责人 {owner}，截止 {due}）",
            **base,
        })
    store.add_memory_facts(facts)


# ---------------------------------------------------------------------------
# 邮件草稿
# ---------------------------------------------------------------------------

EMAIL_SYSTEM = """你是企业行政助理，负责写简洁专业的会议提醒邮件。
要求：
- 中文，正式但不啰嗦；
- 明确列出关键风险与待办（负责人/截止时间未知时写"待确认"）；
- 不要编造人名、时间、链接；
- 只输出 JSON：{"subject": "...", "body": "..."}。"""


async def draft_email(
    provider: ProviderConfig,
    meeting: dict[str, Any],
    *,
    to_addr: str = "",
    extra_instruction: str = "",
    usage_out: dict[str, int] | None = None,
) -> dict[str, Any]:
    risks = meeting.get("risks") or []
    todos = meeting.get("todos") or []
    payload = {
        "会议": meeting.get("title"),
        "时间": meeting.get("meeting_time") or PENDING,
        "地点": meeting.get("location") or PENDING,
        "参与人": meeting.get("participants") or [],
        "结论": meeting.get("summary") or "",
        "风险": [
            {"标题": r["title"], "等级": r["level"], "说明": r["description"]} for r in risks
        ],
        "待办": [
            {
                "内容": t["content"],
                "负责人": t.get("owner") or PENDING,
                "截止": t.get("due_date") or PENDING,
                "优先级": t.get("priority"),
            }
            for t in todos
        ],
    }
    prompt = (
        "请根据以下会议信息生成一封提醒邮件。\n"
        f"{json.dumps(payload, ensure_ascii=False, indent=2)}\n"
        + (f"\n额外要求：{extra_instruction}" if extra_instruction else "")
    )
    try:
        data = await llm.complete_json(
            provider, EMAIL_SYSTEM, prompt, usage_out=usage_out
        )
        subject = str(data.get("subject") or "").strip()
        body = str(data.get("body") or "").strip()
    except llm.LLMError:
        # 模型不可用时退化为模板草稿：仍然是真实可编辑的邮件，只是不经过模型润色
        subject = f"【会议提醒】{meeting.get('title') or '会议'} 风险与待办"
        body = _fallback_email_body(meeting)
    if not subject:
        subject = f"【会议提醒】{meeting.get('title') or '会议'} 风险与待办"
    if not body:
        body = _fallback_email_body(meeting)
    return {"subject": subject[:200], "body": body, "to_addr": to_addr}


def _fallback_email_body(meeting: dict[str, Any]) -> str:
    lines = [f"各位好：", "", f"关于「{meeting.get('title') or '会议'}」，请关注以下事项。", ""]
    risks = meeting.get("risks") or []
    if risks:
        lines.append("一、风险提醒")
        for r in risks:
            lines.append(f"- [{r['level']}] {r['title']}：{r['description'] or '（无补充说明）'}")
        lines.append("")
    todos = meeting.get("todos") or []
    if todos:
        lines.append("二、待办事项")
        for t in todos:
            owner = t.get("owner") or PENDING
            due = t.get("due_date") or PENDING
            lines.append(f"- {t['content']}（负责人：{owner}，截止：{due}）")
        lines.append("")
    lines.append("如有疑问请回复本邮件，谢谢。")
    return "\n".join(lines)
