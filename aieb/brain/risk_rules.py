"""风险条目的确定性校验规则（不依赖模型，可单测、可复现）。

为什么要有这一层
----------------
Benchmark 显示会议风险抽取是"高召回、低精度"：97.5% 的标准风险都能被找到，
但模型会把**已经解决的问题、别人提出的规范提醒、背景说明、总结句**也当成风险。
这类错误有很强的语言学特征，用确定性规则拦掉比让模型"自己小心一点"更可靠。

设计原则
--------
1. **只删有明确反证的东西**：每条规则都必须能指着原文说"这里已经说明解决了/这只是要求"。
   宁可漏删，也不误删——高风险漏报的代价远大于多条冗余。
2. **规则可解释**：每条丢弃都返回原因字符串，落库/日志里能看到"为什么没报"。
3. **不改变"有没有风险"的判断上限**：规则只在 LLM 已经抽出来的候选项里做减法，
   不会凭空补出风险，也不会把被过滤的条目算成"已解决"。

与数据库的关系
--------------
``risk_type`` / ``status`` / ``dropped_reason`` 都是写进既有 risks JSON 里的**附加键**，
不新增表、不改列定义；旧数据没有这些键时读取方按默认值处理，完全向后兼容。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable

# 已解决的语义标记：出现在依据里说明这件事已经闭合
RESOLVED_MARKERS: tuple[str, ...] = (
    "已解决",
    "已关闭",
    "已恢复",
    "已消除",
    "已修复",
    "已通过",
    "未复现",
    "已清零",
    "已整改",
    "恢复正常",
    "暂无异常",
    "均在",
    "已全部关闭",
    "已处理",
)

# 未解决的语义标记：与已解决标记同时出现时不做过滤（说明只是部分缓解）
UNRESOLVED_MARKERS: tuple[str, ...] = (
    "尚未",
    "仍未",
    "未解决",
    "未完成",
    "未达标",
    "待处理",
    "待确认",
    "待定",
    "仍需",
    "尚待",
    "尚未到位",
    "未闭合",
)

# 明确的问题语义：出现这些词说明依据本身在描述一个"不正常"的事实
PROBLEM_MARKERS: tuple[str, ...] = (
    "未",
    "缺",
    "超",
    "延误",
    "延迟",
    "落后",
    "失败",
    "下降",
    "不达标",
    "争议",
    "违规",
    "隐患",
    "冲突",
    "紧张",
    "不足",
    "偏差",
    "异常",
    "投诉",
    "漏报",
    "误报",
    "冲突",
    "超出",
    "中断",
    "停线",
    "超时",
    "返工",
    # 补充：这些词同样在描述"已经不正常的事实"，缺了它们会把真实风险误判成"只是规范要求"
    "用满",
    "无法",
    "受阻",
    "断连",
    "停摆",
    "积压",
    "逾期",
    "吃紧",
    "口径不一",
    "对不上",
    "不一致",
)

# 指令 / 规范 / 提醒：这类句子是"要求怎么做"，不是"已经出了什么问题"
INSTRUCTION_MARKERS: tuple[str, ...] = (
    "提醒",
    "要求",
    "必须",
    "不要",
    "禁止",
    "不能",
    "不得",
    "应当",
    "需要走",
    "一律",
)

# 工作安排 / 关注项
ARRANGEMENT_MARKERS: tuple[str, ...] = ("重点关注", "关注", "安排", "待办", "下一步")

# 假设 / 测算：只在"没有已发生的事实"时才算无效依据
HYPOTHETICAL_MARKERS: tuple[str, ...] = ("如果", "假如", "假设", "若 ", "倘若", "若接受")
CALCULATION_MARKERS: tuple[str, ...] = ("测算", "预估", "估算", "潜在")

# 待评估 / 尚未决定
PENDING_DECISION_MARKERS: tuple[str, ...] = ("需要评估", "待评估", "考虑是否", "是否替换", "需评估")

# 总结性表述：把前面几条风险再概括一遍，属于衍生重复
SUMMARY_MARKERS: tuple[str, ...] = ("以上问题", "综上", "上述问题", "总体来看", "整体来看", "叠加")

_SQUASH_RE = re.compile(r"[\s\u3000，。、；：！？,.;:!?\"'“”‘’（）()\-—_~·…]+")
_SENTENCE_SPLIT = re.compile(r"[。！？；\n]")


def squash(text: str) -> str:
    return _SQUASH_RE.sub("", text or "")


def _has_any(text: str, markers: Iterable[str]) -> str:
    for marker in markers:
        if marker and marker in text:
            return marker
    return ""


@dataclass
class Decision:
    """单条风险的处置结论。"""

    keep: bool
    reason: str = ""


def _next_sentence_hint(raw_text: str, evidence: str, window: int = 80) -> str:
    """看依据在原文里的**下一句**是否说明了"已解决"。

    很多误报的依据本身只是"发生了什么"，真正表示闭合的信息在后面一句
    （例："均为磁盘使用率超过 80% 的阈值提醒，已通过清理日志恢复"）。
    """
    if not evidence or not raw_text:
        return ""
    needle = evidence.strip()
    probe = needle[: min(len(needle), 40)]
    index = raw_text.find(probe)
    if index < 0:
        return ""
    tail = raw_text[index + len(probe) : index + len(probe) + window]
    return tail


def classify_drop(
    risk: dict[str, Any],
    raw_text: str,
) -> Decision:
    """判断一条候选风险是否应当丢弃，并给出可复核的理由。"""
    status = str(risk.get("status") or "").strip().lower()
    title = str(risk.get("title") or "")
    description = str(risk.get("description") or "")
    evidence = str(risk.get("evidence") or "")
    body = f"{title}{description}{evidence}"

    if status in {"resolved", "closed"}:
        return Decision(False, "模型自评状态为已解决")

    if not squash(evidence):
        return Decision(False, "缺少可核查的原文依据")

    squashed_evidence = squash(evidence)
    has_unresolved = _has_any(squashed_evidence, UNRESOLVED_MARKERS)

    resolved = _has_any(squashed_evidence, RESOLVED_MARKERS)
    if resolved and not has_unresolved:
        return Decision(False, f"依据内含已解决标记「{resolved}」")

    # 依据本身没有闭合信息，但紧随其后的一句说明了已恢复/已关闭
    tail = _next_sentence_hint(raw_text, evidence)
    if tail and not has_unresolved:
        tail_resolved = _has_any(squash(tail), RESOLVED_MARKERS)
        if tail_resolved:
            return Decision(False, f"依据后一句已说明解决（「{tail_resolved}」）")

    instruction = _has_any(squashed_evidence, INSTRUCTION_MARKERS)
    # 判断"是否在描述问题"时把标题与描述也算进来：
    # 依据常常只引到"客户要求支持双语"这种要求句，而真正的问题写在标题里
    # （"国际化基础设施与翻译资源缺失"）。只看依据会把真实风险误删。
    problem_hint = _has_any(squash(body), PROBLEM_MARKERS)
    if instruction and not problem_hint:
        return Decision(False, f"依据是规范/提醒（「{instruction}」），未描述已发生的问题")

    arrangement = _has_any(squashed_evidence, ARRANGEMENT_MARKERS)
    if arrangement and not problem_hint:
        return Decision(False, f"依据是工作安排（「{arrangement}」）")

    hypothetical = _has_any(evidence, HYPOTHETICAL_MARKERS)
    calculation = _has_any(squashed_evidence, CALCULATION_MARKERS)
    if hypothetical and calculation and not has_unresolved:
        return Decision(False, "依据是假设条件下的测算，不是已发生的事实")

    pending = _has_any(squashed_evidence, PENDING_DECISION_MARKERS)
    if pending and not _has_any(squashed_evidence, ("已出现", "已导致", "目前存在")):
        return Decision(False, f"依据是待评估事项（「{pending}」），尚未形成风险事实")

    summary = _has_any(squashed_evidence, SUMMARY_MARKERS)
    if summary:
        return Decision(False, f"依据是总结性表述（「{summary}」），属于衍生重复")

    if not squash(body):
        return Decision(False, "风险标题与说明均为空")

    return Decision(True)


def _bigrams(text: str) -> set[str]:
    squashed = squash(text)
    return {squashed[i : i + 2] for i in range(len(squashed) - 1)} or {squashed}


def _similarity(a: str, b: str) -> float:
    sa, sb = _bigrams(a), _bigrams(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def dedupe(risks: list[dict[str, Any]], threshold: float = 0.62) -> list[dict[str, Any]]:
    """合并同一风险的重复条目（保留等级更高、依据更具体的一条）。"""
    kept: list[dict[str, Any]] = []
    for risk in risks:
        merged = False
        for existing in kept:
            if squash(risk.get("title", "")) and (
                squash(risk.get("title", "")) == squash(existing.get("title", ""))
                or squash(risk.get("evidence", "")) == squash(existing.get("evidence", ""))
            ):
                _absorb(existing, risk)
                merged = True
                break
            if _similarity(
                f"{risk.get('title','')}{risk.get('description','')}",
                f"{existing.get('title','')}{existing.get('description','')}",
            ) >= threshold:
                _absorb(existing, risk)
                merged = True
                break
        if not merged:
            kept.append(dict(risk))
    return kept


_LEVEL_ORDER = {"high": 3, "medium": 2, "low": 1}


def _absorb(target: dict[str, Any], extra: dict[str, Any]) -> None:
    if _LEVEL_ORDER.get(str(extra.get("level")), 0) > _LEVEL_ORDER.get(str(target.get("level")), 0):
        target["level"] = extra.get("level", target.get("level"))
    if len(str(extra.get("description") or "")) > len(str(target.get("description") or "")):
        target["description"] = extra.get("description", target.get("description"))
    if not squash(target.get("evidence", "")) and squash(extra.get("evidence", "")):
        target["evidence"] = extra.get("evidence")


def filter_risks(
    risks: list[dict[str, Any]], raw_text: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """返回 ``(保留的风险, 被丢弃的风险及原因)``。"""
    kept: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    for risk in risks:
        decision = classify_drop(risk, raw_text)
        if decision.keep:
            kept.append(risk)
        else:
            dropped.append({**risk, "dropped_reason": decision.reason})
    kept = dedupe(kept)
    return kept, dropped
