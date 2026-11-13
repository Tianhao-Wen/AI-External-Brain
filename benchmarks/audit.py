"""基线审计：从结果文件独立重算指标，核对报告口径，并冻结快照。

运行::

    python -m benchmarks.audit

做的事：

1. **独立重算**：不复用评测器里存好的 aggregates，而是从 ``cases`` 逐条重算
   Precision / Recall / F1 / 各类准确率，与结果文件里记录的 aggregates 比对，
   不一致就报出来（说明评测器的聚合逻辑有问题）。
2. **口径核对**：把"无风险会议""Agent 成功率"等容易含糊的分母逐一列出，
   同时给出 run-level / task-level 两种口径，避免用重复任务加权刷分。
3. **冻结快照**：把三份结果复制到 ``benchmarks/results/baseline_v1/``，
   不覆盖任何原始结果，后续 A/B 对比一律以这份快照为"优化前"。
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from typing import Any

from benchmarks import common

RESULTS = common.RESULTS_DIR
SNAPSHOT = RESULTS / "baseline_v1"


# ---------------------------------------------------------------------------
# 独立重算
# ---------------------------------------------------------------------------


def _ratio(num: float, den: float) -> float:
    return round(num / den, 4) if den else 0.0


def audit_rag(data: dict[str, Any]) -> dict[str, Any]:
    cases = data["cases"]
    positives = [c for c in cases if c["answerable"]]
    negatives = [c for c in cases if not c["answerable"]]
    executed = [c for c in cases if (c.get("answer") or {}).get("executed")]

    recomputed = {
        "answer_accuracy": _ratio(
            sum(1 for c in positives if c["answer"].get("answer_correct")), len(positives)
        ),
        "abstention_accuracy": _ratio(
            sum(1 for c in negatives if c["answer"].get("answer_correct")), len(negatives)
        ),
        "citation_precision": round(
            sum(c["answer"].get("citation_precision") or 0.0 for c in positives) / max(1, len(positives)), 4
        ),
        "recall@5": round(
            sum(c["retrieval"].get("recall@5") or 0.0 for c in positives) / max(1, len(positives)), 4
        ),
        "hit@5": round(
            sum(c["retrieval"].get("hit@5") or 0.0 for c in positives) / max(1, len(positives)), 4
        ),
        "end_to_end": _ratio(sum(1 for c in executed if c["answer"].get("correct")), len(executed)),
    }
    stored_answer = data["aggregates"]["answer"]
    stored_retrieval = data["aggregates"]["retrieval"]
    mismatches = []
    for key, value in recomputed.items():
        if key in ("recall@5", "hit@5"):
            target = stored_retrieval.get(key)
        elif key == "end_to_end":
            target = stored_answer.get("end_to_end_success")
        else:
            target = stored_answer.get(key)
        if target is None:
            mismatches.append(f"{key}: 结果文件缺少该字段")
        elif abs(float(target) - value) > 0.001:
            mismatches.append(f"{key}: 重算 {value} vs 记录 {target}")
    return {
        "denominators": {
            "questions": len(cases),
            "answerable": len(positives),
            "negative": len(negatives),
            "answered_executed": len(executed),
        },
        "recomputed": recomputed,
        "stored": {
            **{k: stored_retrieval.get(k) for k in ("recall@5", "hit@5")},
            **{k: stored_answer.get(k) for k in
               ("answer_accuracy", "abstention_accuracy", "citation_precision", "end_to_end_success")},
        },
        "mismatches": mismatches,
    }


def audit_meeting(data: dict[str, Any]) -> dict[str, Any]:
    cases = data["cases"]
    tp = sum(c["risks"]["tp"] for c in cases)
    fp = sum(c["risks"]["fp"] for c in cases)
    fn = sum(c["risks"]["fn"] for c in cases)
    no_risk_cases = [c for c in cases if c["risks"]["truly_no_risk"]]
    no_risk_fp = [c for c in no_risk_cases if c["risks"]["no_risk_false_positive"]]
    by_category_no_risk = [c for c in cases if c["category"] == "no_risk"]

    recomputed = {
        "precision": _ratio(tp, tp + fp),
        "recall": _ratio(tp, tp + fn),
        "f1": _ratio(2 * tp, 2 * tp + fp + fn),
        "gold_total": sum(c["risks"]["gold_count"] for c in cases),
        "pred_total": sum(c["risks"]["pred_count"] for c in cases),
    }
    stored = data["aggregates"]["overall"]["risk"]
    mismatches = []
    for key in ("precision", "recall", "f1", "gold_total", "pred_total"):
        if key in ("gold_total", "pred_total"):
            if int(stored.get(key, -1)) != recomputed[key]:
                mismatches.append(f"{key}: 重算 {recomputed[key]} vs 记录 {stored.get(key)}")
        elif abs(float(stored.get(key, -1)) - recomputed[key]) > 0.001:
            mismatches.append(f"{key}: 重算 {recomputed[key]} vs 记录 {stored.get(key)}")

    # 一对一匹配自检：TP 不能超过任何一侧的总数，且 gold/pred 计数必须与逐条累加一致
    assert tp <= recomputed["gold_total"] and tp <= recomputed["pred_total"], "匹配数超过单侧总量"
    per_case_ok = all(
        c["risks"]["tp"] + c["risks"]["fn"] == c["risks"]["gold_count"]
        and c["risks"]["tp"] + c["risks"]["fp"] == c["risks"]["pred_count"]
        for c in cases
    )
    return {
        "denominators": {
            "meetings": len(cases),
            "meetings_with_zero_gold_risk": len(no_risk_cases),
            "meetings_in_no_risk_category": len(by_category_no_risk),
            "no_risk_false_positive_meetings": len(no_risk_fp),
            "gold_risks": recomputed["gold_total"],
            "pred_risks": recomputed["pred_total"],
        },
        "recomputed": recomputed,
        "no_risk_false_positive_rate_on_zero_gold": _ratio(len(no_risk_fp), len(no_risk_cases)),
        "no_risk_false_positive_rate_on_category": _ratio(
            sum(1 for c in by_category_no_risk if c["risks"]["no_risk_false_positive"]),
            len(by_category_no_risk),
        ),
        "one_to_one_invariant_holds": per_case_ok,
        "stored": stored,
        "mismatches": mismatches,
    }


def audit_agent(data: dict[str, Any]) -> dict[str, Any]:
    cases = data["cases"]
    per_task: dict[str, list[bool]] = {}
    for case in cases:
        per_task.setdefault(case["task_id"], []).append(bool(case["success"]))

    tool_calls = [t for c in cases for t in c["tool_calls"]]
    tool_ok = [t for t in tool_calls if t["ok"]]

    pptx = [c for c in cases if c["type"] == "pptx"]
    recomputed = {
        "run_level_success": _ratio(sum(1 for c in cases if c["success"]), len(cases)),
        "task_level_first_round": _ratio(
            sum(1 for v in per_task.values() if v[0]), len(per_task)
        ),
        "task_level_all_rounds": _ratio(
            sum(1 for v in per_task.values() if all(v)), len(per_task)
        ),
        "tool_call_success": _ratio(len(tool_ok), len(tool_calls)),
    }
    return {
        "denominators": {
            "executions": len(cases),
            "unique_tasks": len(per_task),
            "repeated_tasks": sum(1 for v in per_task.values() if len(v) > 1),
            "tool_calls": len(tool_calls),
            "pptx_executions": len(pptx),
        },
        "recomputed": recomputed,
        "pptx_run_level_success": _ratio(sum(1 for c in pptx if c["success"]), len(pptx)),
        "failed_labels": [c["label"] for c in cases if not c["success"]],
        "stored": {
            "task_success_rate": data["aggregates"].get("task_success_rate"),
            "tool_call_success_rate": data["aggregates"].get("tool_call_success_rate"),
        },
    }


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def main() -> int:
    report: dict[str, Any] = {"snapshot_dir": str(SNAPSHOT)}
    problems: list[str] = []

    rag = json.loads((RESULTS / "rag_results.json").read_text(encoding="utf-8"))
    meeting = json.loads((RESULTS / "meeting_results.json").read_text(encoding="utf-8"))
    agent = json.loads((RESULTS / "agent_results.json").read_text(encoding="utf-8"))

    report["rag"] = audit_rag(rag)
    report["meeting"] = audit_meeting(meeting)
    report["agent"] = audit_agent(agent)

    for name, block in (("rag", report["rag"]), ("meeting", report["meeting"])):
        problems.extend(f"{name}: {item}" for item in block.get("mismatches") or [])
    if not report["meeting"]["one_to_one_invariant_holds"]:
        problems.append("meeting: 一对一匹配的 tp/fp/fn 与 gold/pred 计数不自洽")

    SNAPSHOT.mkdir(parents=True, exist_ok=True)
    for name in ("rag_results.json", "rag_cases.csv", "rag_summary.md",
                 "meeting_results.json", "meeting_cases.csv", "meeting_summary.md",
                 "agent_results.json", "agent_cases.csv", "agent_summary.md"):
        src = RESULTS / name
        if src.is_file():
            shutil.copy2(src, SNAPSHOT / name)

    report["problems"] = problems
    common.write_json(SNAPSHOT / "AUDIT.json", report)

    print("=" * 70)
    print("RAG")
    for k, v in report["rag"]["denominators"].items():
        print(f"  分母 {k}: {v}")
    for k, v in report["rag"]["recomputed"].items():
        print(f"  {k}: {v}")
    print("会议")
    for k, v in report["meeting"]["denominators"].items():
        print(f"  分母 {k}: {v}")
    print(f"  零风险会议误报率（分母=零标准风险会议）: "
          f"{report['meeting']['no_risk_false_positive_rate_on_zero_gold']:.4f}")
    print(f"  零风险会议误报率（分母=no_risk 类别）: "
          f"{report['meeting']['no_risk_false_positive_rate_on_category']:.4f}")
    print(f"  一对一匹配自洽: {report['meeting']['one_to_one_invariant_holds']}")
    print("Agent")
    for k, v in report["agent"]["denominators"].items():
        print(f"  分母 {k}: {v}")
    for k, v in report["agent"]["recomputed"].items():
        print(f"  {k}: {v}")
    print(f"  PPT run-level: {report['agent']['pptx_run_level_success']:.4f}")
    print("=" * 70)
    if problems:
        print("发现口径不一致：")
        for item in problems:
            print("  -", item)
    else:
        print("所有重算指标与结果文件记录一致。")
    print(f"基线快照：{SNAPSHOT}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
