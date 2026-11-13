"""会议分析评分器：风险 / 待办 / 决策的确定性匹配与指标聚合。

为什么用规则匹配而不是 LLM-as-Judge
------------------------------------
会议分析是**抽取**任务，标准答案里已经写好了每条风险/待办/决策的判分短语。
用同一条确定性规则同时处理"标准答案"和"模型输出"，任何人重跑都能得到完全相同的分数。
LLM 打分在小样本上方差极大，反而不可复现。

匹配方式
--------
* 一对一贪心匹配：先枚举所有"标注项 × 预测项"的候选对，按匹配到的短语数降序指派，
  每个标注项与每个预测项最多只匹配一次——这样模型把一条风险拆成三条会被记为 FP，
  而不是靠重复召回刷高 Recall。
* 类型感知匹配：额外要求预测风险的类型（由固定关键词分类器判定）与标注类型一致，
  用来算 per-type P/R/F1；主指标用**类型无关**匹配，避免分类器的误差污染检出率。
"""

from __future__ import annotations

import re
from typing import Any, Iterable

from benchmarks.evaluators.metrics import mean, prf, ratio

RISK_TYPES = ("进度", "成本", "质量", "技术", "资源", "需求变更", "合规", "供应商")
SEVERITIES = ("high", "medium", "low")

_SQUASH_RE = re.compile(r"[\s\u3000，。、；：！？,.;:!?\"'“”‘’（）()\-—_~·…%]+")
_DIGITS_RE = re.compile(r"\d+")

# 风险类型分类器：只看预测结果的标题+描述，与不看标注。
# 这是评测工具链的一部分，同样作用于人工标注的复核（见 validate_dataset）。
RISK_TYPE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "进度": ("延期", "落后", "延误", "推迟", "超期", "交付节点", "工期", "进度", "窗口", "赶不上"),
    "成本": ("超支", "超出预算", "预算", "费用", "成本", "万元", "开支", "报价"),
    "质量": ("缺陷", "bug", "失败率", "合格率", "质量", "不达标", "验收标准", "返工", "重复支付", "代码质量"),
    "技术": ("技术债", "单点", "性能", "慢查询", "n+1", "兼容性", "架构", "数据库实例", "sql", "重构", "超时"),
    "资源": ("人力", "人员", "缺口", "招聘", "借调", "离职", "技能", "编制", "人手"),
    "需求变更": ("变更", "需求调整", "范围扩大", "范围过大", "改需求", "客户要求变更"),
    "合规": ("合规", "违规", "审计", "脱敏", "出境", "加密", "个人隐私", "个人信息", "处罚", "法律责任", "合同条款"),
    "供应商": ("供应商", "服务商", "对接人", "交付周期", "来料", "分拨", "外包方", "供货"),
}


def squash(text: str) -> str:
    return _SQUASH_RE.sub("", text or "").lower()


def classify_risk_type(*texts: str) -> str:
    """按命中关键词数量判定风险类型；一个都不命中返回空串。"""
    body = squash(" ".join(t or "" for t in texts))
    best = ""
    best_score = 0
    for name in RISK_TYPES:
        score = sum(1 for kw in RISK_TYPE_KEYWORDS[name] if squash(kw) in body)
        if score > best_score:
            best, best_score = name, score
    return best


def severity_of(level: str) -> str:
    text = str(level or "").strip().lower()
    if text in {"high", "高", "严重", "critical"}:
        return "high"
    if text in {"low", "低", "轻微", "minor"}:
        return "low"
    return "medium"


def pred_text(item: dict[str, Any], *fields: str) -> str:
    return " ".join(str(item.get(f) or "") for f in fields)


def phrase_hits(phrases: Iterable[str], body_squashed: str) -> int:
    return sum(1 for phrase in phrases if squash(phrase) and squash(phrase) in body_squashed)


def grounded(value: str, raw_squashed: str) -> bool:
    candidate = squash(value)
    return bool(candidate) and candidate in raw_squashed


def date_key(text: str) -> str:
    """把 ``2026年3月7日`` 与 ``2026-03-07`` 归一成同一串数字。"""
    return "".join(_DIGITS_RE.findall(str(text or "")))


def greedy_match(
    gold_items: list[dict[str, Any]],
    pred_items: list[dict[str, Any]],
    pred_bodies: list[str],
    *,
    same_type: bool = False,
    type_of_gold=None,
    type_of_pred=None,
) -> list[tuple[int, int, int]]:
    """返回 ``[(gold_index, pred_index, 命中短语数)]``，一一对应。"""
    candidates: list[tuple[int, int, int]] = []
    for gi, gold in enumerate(gold_items):
        phrases = gold.get("match") or []
        for pi, body in enumerate(pred_bodies):
            if same_type and type_of_gold is not None and type_of_pred is not None:
                if type_of_gold(gi) != type_of_pred(pi):
                    continue
            score = phrase_hits(phrases, body)
            if score > 0:
                candidates.append((score, gi, pi))
    candidates.sort(key=lambda item: (-item[0], item[1], item[2]))
    used_gold: set[int] = set()
    used_pred: set[int] = set()
    pairs: list[tuple[int, int, int]] = []
    for score, gi, pi in candidates:
        if gi in used_gold or pi in used_pred:
            continue
        used_gold.add(gi)
        used_pred.add(pi)
        pairs.append((gi, pi, score))
    return pairs


# ---------------------------------------------------------------------------
# 风险
# ---------------------------------------------------------------------------


def evaluate_risks(gold: dict[str, Any], prediction: dict[str, Any], raw_text: str) -> dict[str, Any]:
    gold_risks: list[dict[str, Any]] = gold.get("risks") or []
    pred_risks: list[dict[str, Any]] = prediction.get("risks") or []
    raw_squashed = squash(raw_text)

    bodies = [squash(pred_text(r, "title", "description")) for r in pred_risks]
    pred_types = [classify_risk_type(pred_text(r, "title", "description")) for r in pred_risks]

    pairs = greedy_match(gold_risks, pred_risks, bodies)
    matched_gold = {gi for gi, _, _ in pairs}
    matched_pred = {pi for _, pi, _ in pairs}

    severity_confusion: dict[str, dict[str, int]] = {g: {p: 0 for p in SEVERITIES} for g in SEVERITIES}
    severity_ok = 0
    evidence_grounded = 0
    evidence_relevant = 0
    type_ok = 0
    matched_rows: list[dict[str, Any]] = []
    for gi, pi, score in pairs:
        g = gold_risks[gi]
        p = pred_risks[pi]
        g_sev = severity_of(g.get("severity"))
        p_sev = severity_of(p.get("level"))
        severity_confusion[g_sev][p_sev] += 1
        if g_sev == p_sev:
            severity_ok += 1
        evidence = str(p.get("evidence") or "")
        is_grounded = grounded(evidence, raw_squashed)
        is_relevant = is_grounded and phrase_hits(g.get("match") or [], squash(evidence)) > 0
        evidence_grounded += 1 if is_grounded else 0
        evidence_relevant += 1 if is_relevant else 0
        same_type = pred_types[pi] == g["risk_type"]
        type_ok += 1 if same_type else 0
        matched_rows.append(
            {
                "risk_id": g["risk_id"],
                "gold_type": g["risk_type"],
                "pred_type": pred_types[pi],
                "type_ok": same_type,
                "gold_severity": g_sev,
                "pred_severity": p_sev,
                "severity_ok": g_sev == p_sev,
                "match_score": score,
                "pred_title": str(p.get("title") or "")[:60],
                "evidence_grounded": is_grounded,
                "evidence_relevant": is_relevant,
            }
        )

    tp = len(pairs)
    fp = len(pred_risks) - tp
    fn = len(gold_risks) - tp
    overall = prf(tp, fp, fn)

    # per-type：TP/FN 走**类型无关**的一对一匹配（只看是否检出了这条风险），
    # 分母里的预测条数才需要把预测项归类。这样分类器判错不会凭空抹掉召回，
    # 只在 Precision 上体现，指标含义在报告里有明确说明。
    per_type: dict[str, dict[str, Any]] = {}
    for name in RISK_TYPES:
        g_idx = [i for i, r in enumerate(gold_risks) if r["risk_type"] == name]
        p_idx = [i for i, t in enumerate(pred_types) if t == name]
        t_tp = sum(1 for gi, _, _ in pairs if gi in g_idx)
        t_fn = len(g_idx) - t_tp
        t_fp = max(0, len(p_idx) - t_tp)
        if not g_idx and not p_idx:
            continue
        per_type[name] = {
            "gold": len(g_idx),
            "pred": len(p_idx),
            **prf(t_tp, t_fp, t_fn),
        }

    high_total = sum(1 for r in gold_risks if severity_of(r.get("severity")) == "high")
    high_missed = sum(
        1 for i, r in enumerate(gold_risks)
        if severity_of(r.get("severity")) == "high" and i not in matched_gold
    )

    return {
        "gold_count": len(gold_risks),
        "pred_count": len(pred_risks),
        **overall,
        "matched": matched_rows,
        "missed": [gold_risks[gi]["risk_id"] for gi in range(len(gold_risks)) if gi not in matched_gold],
        "spurious": [
            {
                "index": pi,
                "title": str(pred_risks[pi].get("title") or "")[:60],
                "pred_type": pred_types[pi],
            }
            for pi in range(len(pred_risks))
            if pi not in matched_pred
        ],
        "truly_no_risk": len(gold_risks) == 0,
        "no_risk_false_positive": len(gold_risks) == 0 and len(pred_risks) > 0,
        "severity_accuracy": ratio(severity_ok, tp),
        "severity_confusion": severity_confusion,
        "evidence_grounded_rate": ratio(evidence_grounded, tp),
        "evidence_relevant_rate": ratio(evidence_relevant, tp),
        "type_accuracy": ratio(type_ok, tp),
        "high_risk_total": high_total,
        "high_risk_missed": high_missed,
        "per_type": per_type,
    }


# ---------------------------------------------------------------------------
# 待办
# ---------------------------------------------------------------------------


def evaluate_todos(gold: dict[str, Any], prediction: dict[str, Any], raw_text: str) -> dict[str, Any]:
    gold_todos: list[dict[str, Any]] = gold.get("todos") or []
    pred_todos: list[dict[str, Any]] = prediction.get("todos") or []
    raw_squashed = squash(raw_text)
    bodies = [squash(pred_text(t, "content")) for t in pred_todos]

    pairs = greedy_match(gold_todos, pred_todos, bodies)
    matched_gold = {gi for gi, _, _ in pairs}
    matched_pred = {pi for _, pi, _ in pairs}

    owner_ok = 0
    deadline_ok = 0
    owner_fabricated = 0
    owner_false_fill = 0
    deadline_fabricated = 0
    rows: list[dict[str, Any]] = []
    for gi, pi, score in pairs:
        g = gold_todos[gi]
        p = pred_todos[pi]
        g_owner = str(g.get("owner") or "")
        p_owner = str(p.get("owner") or "").strip()
        g_due = str(g.get("deadline") or "")
        p_due = str(p.get("due_date") or "").strip()

        if not g_owner:
            # 原文没写负责人：模型必须留空，填了就是编造
            o_ok = not p_owner
            if p_owner:
                owner_false_fill += 1
        else:
            o_ok = squash(p_owner) == squash(g_owner) or (
                bool(p_owner) and (squash(g_owner) in squash(p_owner) or squash(p_owner) in squash(g_owner))
            )
        owner_ok += 1 if o_ok else 0

        if not g_due:
            d_ok = not p_due
            if p_due and not grounded(p_due, raw_squashed):
                deadline_fabricated += 1
        else:
            d_ok = bool(p_due) and date_key(p_due) == date_key(g_due)

        if p_owner and not grounded(p_owner, raw_squashed):
            owner_fabricated += 1
        deadline_ok += 1 if d_ok else 0

        rows.append(
            {
                "todo_id": g["todo_id"],
                "match_score": score,
                "gold_owner": g_owner,
                "pred_owner": p_owner,
                "owner_ok": o_ok,
                "gold_deadline": g_due,
                "pred_deadline": p_due,
                "deadline_ok": d_ok,
                "pred_content": str(p.get("content") or "")[:60],
            }
        )

    tp = len(pairs)
    fp = len(pred_todos) - tp
    fn = len(gold_todos) - tp
    return {
        "gold_count": len(gold_todos),
        "pred_count": len(pred_todos),
        **prf(tp, fp, fn),
        "matched": rows,
        "missed": [gold_todos[gi]["todo_id"] for gi in range(len(gold_todos)) if gi not in matched_gold],
        "owner_accuracy": ratio(owner_ok, tp),
        "deadline_accuracy": ratio(deadline_ok, tp),
        "owner_fabricated": owner_fabricated,
        "owner_false_fill": owner_false_fill,
        "deadline_fabricated": deadline_fabricated,
        "unmatched_pred": len(pred_todos) - len(matched_pred),
    }


# ---------------------------------------------------------------------------
# 决策
# ---------------------------------------------------------------------------

_RETRACT_WORDS = ("撤回", "撤销", "取消", "推翻", "暂缓", "顺延", "更正")


def evaluate_decisions(gold: dict[str, Any], prediction: dict[str, Any]) -> dict[str, Any]:
    gold_decisions: list[dict[str, Any]] = gold.get("decisions") or []
    pred_decisions = [str(d) for d in (prediction.get("decisions") or [])]
    bodies = [squash(d) for d in pred_decisions]

    pairs = greedy_match(gold_decisions, pred_decisions, bodies)
    found = {gi for gi, _, _ in pairs}
    used_pred = {pi for _, pi, _ in pairs}

    conflicts: list[str] = []
    for g in gold_decisions:
        superseded = g.get("superseded") or []
        if not superseded:
            continue
        claims_old = any(phrase_hits(superseded, body) > 0 for body in bodies)
        says_retract = any(any(w in d for w in _RETRACT_WORDS) for d in pred_decisions)
        if claims_old and not says_retract:
            conflicts.append(g["decision_id"])

    tp = len(pairs)
    fp = len(pred_decisions) - tp
    fn = len(gold_decisions) - tp
    return {
        "gold_count": len(gold_decisions),
        "pred_count": len(pred_decisions),
        **prf(tp, fp, fn),
        "found": [gold_decisions[gi]["decision_id"] for gi in range(len(gold_decisions)) if gi in found],
        "missed": [gold_decisions[gi]["decision_id"] for gi in range(len(gold_decisions)) if gi not in found],
        "spurious_count": len(pred_decisions) - len(used_pred),
        "retracted_conflicts": conflicts,
        "all_found": len(found) == len(gold_decisions),
    }


# ---------------------------------------------------------------------------
# 聚合
# ---------------------------------------------------------------------------


def aggregate(cases: list[dict[str, Any]]) -> dict[str, Any]:
    from benchmarks.evaluators.metrics import prf as _prf

    risk_tp = sum(c["risks"]["tp"] for c in cases)
    risk_fp = sum(c["risks"]["fp"] for c in cases)
    risk_fn = sum(c["risks"]["fn"] for c in cases)

    no_risk_cases = [c for c in cases if c["risks"]["truly_no_risk"]]
    risky_cases = [c for c in cases if not c["risks"]["truly_no_risk"]]

    per_type: dict[str, dict[str, Any]] = {}
    for name in RISK_TYPES:
        tp = sum(c["risks"]["per_type"].get(name, {}).get("tp", 0) for c in cases)
        fp = sum(c["risks"]["per_type"].get(name, {}).get("fp", 0) for c in cases)
        fn = sum(c["risks"]["per_type"].get(name, {}).get("fn", 0) for c in cases)
        gold = sum(c["risks"]["per_type"].get(name, {}).get("gold", 0) for c in cases)
        pred = sum(c["risks"]["per_type"].get(name, {}).get("pred", 0) for c in cases)
        if gold == 0 and tp == 0 and fp == 0:
            continue
        per_type[name] = {"gold": gold, "pred": pred, **_prf(tp, fp, fn)}

    confusion: dict[str, dict[str, int]] = {g: {p: 0 for p in SEVERITIES} for g in SEVERITIES}
    for case in cases:
        for g in SEVERITIES:
            for p in SEVERITIES:
                confusion[g][p] += case["risks"]["severity_confusion"][g][p]

    todo_tp = sum(c["todos"]["tp"] for c in cases)
    todo_fp = sum(c["todos"]["fp"] for c in cases)
    todo_fn = sum(c["todos"]["fn"] for c in cases)

    decision_tp = sum(c["decisions"]["tp"] for c in cases)
    decision_fp = sum(c["decisions"]["fp"] for c in cases)
    decision_fn = sum(c["decisions"]["fn"] for c in cases)

    high_total = sum(c["risks"]["high_risk_total"] for c in cases)
    high_missed = sum(c["risks"]["high_risk_missed"] for c in cases)

    matched_total = sum(len(c["risks"]["matched"]) for c in cases)
    return {
        "meetings": len(cases),
        "no_risk_meetings": len(no_risk_cases),
        "risky_meetings": len(risky_cases),
        "failed_meetings": sum(1 for c in cases if c["prediction"].get("error")),
        "risk": {
            "gold_total": sum(c["risks"]["gold_count"] for c in cases),
            "pred_total": sum(c["risks"]["pred_count"] for c in cases),
            **_prf(risk_tp, risk_fp, risk_fn),
            "high_risk_total": high_total,
            "high_risk_missed": high_missed,
            "high_risk_miss_rate": ratio(high_missed, high_total),
            "no_risk_false_positive_meetings": sum(
                1 for c in no_risk_cases if c["risks"]["no_risk_false_positive"]
            ),
            "no_risk_false_positive_rate": ratio(
                sum(1 for c in no_risk_cases if c["risks"]["no_risk_false_positive"]),
                len(no_risk_cases),
            ),
            "severity_accuracy": mean(
                [c["risks"]["severity_accuracy"] for c in cases if c["risks"]["tp"]]
            ),
            "severity_confusion": confusion,
            "evidence_grounded_rate": mean(
                [c["risks"]["evidence_grounded_rate"] for c in cases if c["risks"]["tp"]]
            ),
            "evidence_relevant_rate": mean(
                [c["risks"]["evidence_relevant_rate"] for c in cases if c["risks"]["tp"]]
            ),
            "type_accuracy": mean([c["risks"]["type_accuracy"] for c in cases if c["risks"]["tp"]]),
            "exact_meeting_rate": ratio(
                sum(
                    1
                    for c in cases
                    if c["risks"]["fp"] == 0 and c["risks"]["fn"] == 0
                ),
                len(cases),
            ),
        },
        "risk_per_type": per_type,
        "todos": {
            "gold_total": sum(c["todos"]["gold_count"] for c in cases),
            "pred_total": sum(c["todos"]["pred_count"] for c in cases),
            **_prf(todo_tp, todo_fp, todo_fn),
            "owner_accuracy": mean([c["todos"]["owner_accuracy"] for c in cases if c["todos"]["tp"]]),
            "deadline_accuracy": mean(
                [c["todos"]["deadline_accuracy"] for c in cases if c["todos"]["tp"]]
            ),
            "owner_fabricated_total": sum(c["todos"]["owner_fabricated"] for c in cases),
            "owner_false_fill_total": sum(c["todos"]["owner_false_fill"] for c in cases),
            "deadline_fabricated_total": sum(c["todos"]["deadline_fabricated"] for c in cases),
        },
        "decisions": {
            "gold_total": sum(c["decisions"]["gold_count"] for c in cases),
            "pred_total": sum(c["decisions"]["pred_count"] for c in cases),
            **_prf(decision_tp, decision_fp, decision_fn),
            "meeting_all_found_rate": mean(
                [1.0 if c["decisions"]["all_found"] else 0.0 for c in cases if c["decisions"]["gold_count"]]
            ),
            "retracted_conflicts": sum(len(c["decisions"]["retracted_conflicts"]) for c in cases),
        },
        "matched_risks": matched_total,
    }


def aggregate_by_category(cases: list[dict[str, Any]]) -> dict[str, Any]:
    buckets: dict[str, list[dict[str, Any]]] = {}
    for case in cases:
        buckets.setdefault(case["category"], []).append(case)
    out: dict[str, Any] = {}
    for name, items in sorted(buckets.items()):
        tp = sum(c["risks"]["tp"] for c in items)
        fp = sum(c["risks"]["fp"] for c in items)
        fn = sum(c["risks"]["fn"] for c in items)
        out[name] = {
            "meetings": len(items),
            "gold_risks": sum(c["risks"]["gold_count"] for c in items),
            "pred_risks": sum(c["risks"]["pred_count"] for c in items),
            **prf(tp, fp, fn),
        }
    return out
