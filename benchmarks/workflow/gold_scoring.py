"""金标准匹配与指标计算（确定性、可冻结、可复核）。

设计原则
--------
1. **只看证据与语义重合，不看条数**：风险是否命中，取决于"是否指到同一件事"，
   而不是"预测了 3 条、标准也是 3 条"。否则完全可以靠凑条数刷分。
2. **匹配规则固定且可解释**：字符 2-gram Jaccard + 依据包含关系。
3. **一对一匹配**：同一对 (预测, 标准) 只能命中一次，避免一条预测命中多条标准。
4. 全部函数无随机、无时间、无模型调用 —— 因此可以被 sha256 冻结。

匹配规则（v1，冻结）
--------------------
* 归一化：去空白与中英文标点，小写化。
* `sim(a, b)` = 归一化后字符 2-gram 的 Jaccard 系数（空串返回 0）。
* 风险命中条件（满足其一）：
  - `sim(title_pred, title_gold) >= 0.45`；或
  - `title` 相似度 >= 0.25 且 gold.evidence 被预测 evidence 包含（或反向包含）。
* 待办命中条件（满足其一）：
  - `sim(content_pred, content_gold) >= 0.45`；或
  - `sim >= 0.25` 且负责人相同（都非空）。
* 决策命中：`sim >= 0.35`。
* 采用**贪心最大相似度**做一对一分配（按相似度降序配对）。

这些阈值是在**开发集**（A/B/D 类各 1 例）上确定的，之后不再针对测试集调整。
"""

from __future__ import annotations

import re
from typing import Any, Iterable

SQUASH_RE = re.compile(r"[\s\u3000，。、；：！？,.;:!?\"'“”‘’（）()\-—_~·…]+")

RISK_TITLE_THRESHOLD = 0.45
RISK_TITLE_SOFT = 0.25
TODO_CONTENT_THRESHOLD = 0.45
TODO_CONTENT_SOFT = 0.25
DECISION_THRESHOLD = 0.35

#: 宽松阈值：**只用于灵敏度分析**，不进主指标。
#: 目的：区分「模型真的漏了」与「模型说对了但措辞不同，严格匹配器不接受」。
LOOSE_RISK_TITLE_THRESHOLD = 0.25
LOOSE_TODO_CONTENT_THRESHOLD = 0.25
LOOSE_DECISION_THRESHOLD = 0.18

SCORING_VERSION = "gold_scoring_v1"


def normalize(text: str) -> str:
    return SQUASH_RE.sub("", (text or "").lower())


def bigrams(text: str) -> set[str]:
    norm = normalize(text)
    if len(norm) < 2:
        return {norm} if norm else set()
    return {norm[i : i + 2] for i in range(len(norm) - 1)}


def similarity(a: str, b: str) -> float:
    left, right = bigrams(a), bigrams(b)
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def _contains(a: str, b: str) -> bool:
    """归一化后的包含关系（空串视为不包含）。"""
    na, nb = normalize(a), normalize(b)
    if not na or not nb:
        return False
    return na in nb or nb in na


def _greedy_match(
    preds: list[dict[str, Any]],
    golds: list[dict[str, Any]],
    pair_score,
) -> tuple[list[tuple[int, int, float]], list[int], list[int]]:
    """一对一贪心匹配，返回 (命中三元组, 未命中的预测下标, 漏掉的标准下标)。"""
    candidates: list[tuple[float, int, int]] = []
    for i, pred in enumerate(preds):
        for j, gold in enumerate(golds):
            score = pair_score(pred, gold)
            if score > 0:
                candidates.append((score, i, j))
    candidates.sort(reverse=True)
    used_pred: set[int] = set()
    used_gold: set[int] = set()
    hits: list[tuple[int, int, float]] = []
    for score, i, j in candidates:
        if i in used_pred or j in used_gold:
            continue
        used_pred.add(i)
        used_gold.add(j)
        hits.append((i, j, round(score, 3)))
    fp = [i for i in range(len(preds)) if i not in used_pred]
    fn = [j for j in range(len(golds)) if j not in used_gold]
    return hits, fp, fn


def _risk_pair(pred: dict[str, Any], gold: dict[str, Any], loose: bool = False) -> float:
    title_threshold = LOOSE_RISK_TITLE_THRESHOLD if loose else RISK_TITLE_THRESHOLD
    title_sim = similarity(str(pred.get("title") or ""), str(gold.get("title") or ""))
    if title_sim >= title_threshold:
        return title_sim
    if title_sim >= RISK_TITLE_SOFT and _contains(
        str(pred.get("evidence") or ""), str(gold.get("evidence") or "")
    ):
        return title_sim + 0.2
    # 说明文字高度重合也算命中（模型可能把标题写得更笼统）
    desc_sim = similarity(
        f"{pred.get('title') or ''}{pred.get('description') or ''}",
        f"{gold.get('title') or ''}{gold.get('description') or ''}",
    )
    if desc_sim >= (0.35 if loose else 0.55):
        return desc_sim
    return 0.0


def _todo_pair(pred: dict[str, Any], gold: dict[str, Any], loose: bool = False) -> float:
    threshold = LOOSE_TODO_CONTENT_THRESHOLD if loose else TODO_CONTENT_THRESHOLD
    sim = similarity(str(pred.get("content") or ""), str(gold.get("content") or ""))
    if sim >= threshold:
        return sim
    owner_pred = normalize(str(pred.get("owner") or ""))
    owner_gold = normalize(str(gold.get("owner") or ""))
    if sim >= TODO_CONTENT_SOFT and owner_pred and owner_pred == owner_gold:
        return sim + 0.15
    return 0.0


def _decision_pair(pred: str, gold: str, loose: bool = False) -> float:
    sim = similarity(pred, gold)
    threshold = LOOSE_DECISION_THRESHOLD if loose else DECISION_THRESHOLD
    return sim if sim >= threshold else 0.0


def _prf(tp: int, fp: int, fn: int) -> dict[str, float]:
    precision = tp / (tp + fp) if (tp + fp) else 1.0 if tp == 0 and fp == 0 else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 1.0
    if precision + recall == 0:
        f1 = 0.0
    else:
        f1 = 2 * precision * recall / (precision + recall)
    return {
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
        "tp": tp,
        "fp": fp,
        "fn": fn,
    }


def score_case(
    gold: dict[str, Any],
    predicted: dict[str, Any],
    *,
    status: str,
    sent_count: int,
    duplicate_side_effects: int,
    loose: bool = False,
) -> dict[str, Any]:
    """对单个用例评分。`predicted` 需包含 risks/todos/decisions /meetings/emails。

    `loose=True` 走**宽松阈值**，只用于灵敏度分析（区分"真的漏了"与"措辞不同"），
    结果不能当作主指标对外报。
    """
    risk_hits, risk_fp, risk_fn = _greedy_match(
        list(predicted.get("risks") or []),
        list(gold.get("gold_risks") or []),
        lambda p, g: _risk_pair(p, g, loose),
    )
    todo_hits, todo_fp, todo_fn = _greedy_match(
        list(predicted.get("todos") or []),
        list(gold.get("gold_todos") or []),
        lambda p, g: _todo_pair(p, g, loose),
    )
    gold_decisions = [str(d) for d in gold.get("gold_decisions") or []]
    pred_decisions = [str(d) for d in predicted.get("decisions") or []]
    decision_hits, decision_fp, decision_fn = _greedy_match(
        [{"content": d} for d in pred_decisions],
        [{"content": d} for d in gold_decisions],
        lambda p, g: _decision_pair(str(p["content"]), str(g["content"]), loose),
    )

    risk_metrics = _prf(len(risk_hits), len(risk_fp), len(risk_fn))
    todo_metrics = _prf(len(todo_hits), len(todo_fp), len(todo_fn))
    decision_recall = (
        len(decision_hits) / len(gold_decisions) if gold_decisions else 1.0
    )

    effects = gold.get("expected_side_effects") or {}
    side_effect_ok = (
        predicted.get("meetings", 0) == effects.get("meetings", 1)
        and predicted.get("emails", 0) == effects.get("emails", 1)
        and sent_count == effects.get("sends", 0)
        and duplicate_side_effects == 0
    )
    workflow_ok = status == gold.get("expected_workflow_status")
    # 任务成功 = 流程走对 + 业务副作用正确 + 没有重复副作用
    task_ok = bool(workflow_ok and side_effect_ok)

    return {
        "id": gold["id"],
        "category": gold["category"],
        "length": gold["length"],
        "controller_decision": gold["controller_decision"],
        "status": status,
        "expected_status": gold.get("expected_workflow_status"),
        "workflow_ok": workflow_ok,
        "side_effect_ok": side_effect_ok,
        "task_ok": task_ok,
        "risks": risk_metrics,
        "todos": todo_metrics,
        "decisions": {
            "recall": round(decision_recall, 4),
            "matched": len(decision_hits),
            "gold": len(gold_decisions),
            "extra": len(decision_fp),
        },
        "risk_false_positives": [predicted["risks"][i].get("title") for i in risk_fp],
        "risk_false_negatives": [gold["gold_risks"][j].get("title") for j in risk_fn],
        "todo_false_positives": [predicted["todos"][i].get("content") for i in todo_fp],
        "todo_false_negatives": [gold["gold_todos"][j].get("content") for j in todo_fn],
        "predicted_counts": {
            "risks": len(predicted.get("risks") or []),
            "todos": len(predicted.get("todos") or []),
            "decisions": len(pred_decisions),
        },
        "gold_counts": {
            "risks": len(gold.get("gold_risks") or []),
            "todos": len(gold.get("gold_todos") or []),
            "decisions": len(gold_decisions),
        },
        "duplicate_side_effects": duplicate_side_effects,
        "sent_count": sent_count,
    }


def aggregate(cases: Iterable[dict[str, Any]]) -> dict[str, Any]:
    cases = list(cases)
    if not cases:
        return {}

    def _mean(values: list[float]) -> float:
        return round(sum(values) / len(values), 4) if values else 0.0

    tp_r = sum(c["risks"]["tp"] for c in cases)
    fp_r = sum(c["risks"]["fp"] for c in cases)
    fn_r = sum(c["risks"]["fn"] for c in cases)
    tp_t = sum(c["todos"]["tp"] for c in cases)
    fp_t = sum(c["todos"]["fp"] for c in cases)
    fn_t = sum(c["todos"]["fn"] for c in cases)
    return {
        "scoring_version": SCORING_VERSION,
        "cases": len(cases),
        # 分母说明见 docs/LANGGRAPH_LIVE_BENCHMARK.md
        "workflow_completion_rate": _mean([1.0 if c["workflow_ok"] else 0.0 for c in cases]),
        "task_success_rate": _mean([1.0 if c["task_ok"] else 0.0 for c in cases]),
        "side_effect_correct_rate": _mean([1.0 if c["side_effect_ok"] else 0.0 for c in cases]),
        "risk_micro": _prf(tp_r, fp_r, fn_r),
        "risk_macro": {
            "precision": _mean([c["risks"]["precision"] for c in cases]),
            "recall": _mean([c["risks"]["recall"] for c in cases]),
            "f1": _mean([c["risks"]["f1"] for c in cases]),
        },
        "todo_micro": _prf(tp_t, fp_t, fn_t),
        "todo_macro": {
            "precision": _mean([c["todos"]["precision"] for c in cases]),
            "recall": _mean([c["todos"]["recall"] for c in cases]),
            "f1": _mean([c["todos"]["f1"] for c in cases]),
        },
        "decision_accuracy": _mean([c["decisions"]["recall"] for c in cases]),
        "false_positive_rate": (
            round(fp_r / (tp_r + fp_r), 4) if (tp_r + fp_r) else 0.0
        ),
        "duplicate_side_effects": sum(c["duplicate_side_effects"] for c in cases),
        "failed_cases": [c["id"] for c in cases if not c["task_ok"]],
    }
