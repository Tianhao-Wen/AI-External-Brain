"""生成 A/B 对比表（机器计算，避免手抄错数字）。

用法::

    python -m benchmarks.ab_report

读取 ``benchmarks/results/baseline_v1/<suite>_<split>`` 与 ``benchmarks/results/opt_v2/<suite>_<split>``，
输出 ``docs/_ab_table.md``，并在终端打印。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from benchmarks import common

BASE = common.RESULTS_DIR / "baseline_v1"
OPT = common.RESULTS_DIR / "opt_v2"
OPT_FINAL = common.RESULTS_DIR / "opt_final"
OUT = common.PROJECT_ROOT / "docs" / "_ab_table.md"

# split -> (baseline 子目录, optimized 子目录)
PAIRS: dict[str, dict[str, tuple[str, str]]] = {
    "meeting": {
        "dev": ("meeting_dev", "meeting_dev"),
        "test_exposed": ("meeting_test_exposed", "meeting_test_exposed"),
        "test_unseen": ("meeting_test_unseen", "meeting_test_unseen"),
    },
    "rag": {
        "dev": ("rag_dev", "rag_dev"),
        "test_exposed": ("rag_test_exposed", "rag_test_exposed"),
        "test_unseen": ("rag_test_unseen", "rag_test_unseen"),
    },
    "agent": {
        "dev": ("agent_dev", "agent_dev_v3"),
        "test_exposed": ("agent_test_exposed", "agent_test_exposed_v3"),
        "test_unseen": ("agent_test_unseen", "agent_test_unseen_v3"),
    },}


def _load(root: Path, sub: str, suite: str) -> dict[str, Any] | None:
    path = root / sub / f"{suite}_results.json"
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def meeting_metrics(data: dict[str, Any]) -> dict[str, float]:
    o = data["aggregates"]["overall"]
    r = o["risk"]
    return {
        "meetings": o["meetings"],
        "gold_risks": r["gold_total"],
        "precision": r["precision"],
        "recall": r["recall"],
        "f1": r["f1"],
        "no_risk_fp_rate": r["no_risk_false_positive_rate"],
        "no_risk_fp": r["no_risk_false_positive_meetings"],
        "no_risk_total": o["no_risk_meetings"],
        "high_missed": r["high_risk_missed"],
        "high_total": r["high_risk_total"],
        "severity_accuracy": r["severity_accuracy"],
        "todo_f1": o["todos"]["f1"],
        "p50_ms": data["aggregates"]["latency"]["p50_ms"],
    }


def rag_metrics(data: dict[str, Any]) -> dict[str, float]:
    ret = data["aggregates"]["retrieval"]
    ans = data["aggregates"]["answer"]
    return {
        "answerable": ret["answerable_questions"],
        "negative": ret["negative_questions"],
        "recall_at_5": ret["recall@5"],
        "answer_accuracy": ans["answer_accuracy"],
        "citation_precision": ans["citation_precision"],
        "citation_recall": ans["citation_recall"],
        "end_to_end": ans["end_to_end_success"],
        "abstention": ans["abstention_accuracy"],
        "groundedness": ans["groundedness"],
        "answer_p95_ms": data["aggregates"]["latency"]["answer_p95_ms"],
    }


def agent_metrics(data: dict[str, Any]) -> dict[str, float]:
    a = data["aggregates"]
    cases = data["cases"]
    pptx = [c for c in cases if c["type"] == "pptx"]
    per_task: dict[str, list[bool]] = {}
    for c in cases:
        per_task.setdefault(c["task_id"], []).append(bool(c["success"]))
    dup = a.get("duplicate_tool_calls") or {}
    return {
        "executions": len(cases),
        "task_success": a["task_success_rate"],
        "passed": a["task_passed"],
        "tool_success": a["tool_call_success_rate"],
        "p50_ms": a["latency_p50_ms"],
        "p95_ms": a["latency_p95_ms"],
        "avg_llm": a["avg_llm_calls"],
        "pptx_success": (sum(1 for c in pptx if c["success"]) / len(pptx)) if pptx else float("nan"),
        "pptx_runs": len(pptx),
        "task_level_all_rounds": sum(1 for v in per_task.values() if all(v)) / len(per_task),
        "dup_rate": dup.get("rate", 0.0),
    }


EXTRACTORS = {"meeting": meeting_metrics, "rag": rag_metrics, "agent": agent_metrics}

FMT = {
    "meetings": "{:.0f}",
    "gold_risks": "{:.0f}",
    "precision": "{:.3f}",
    "recall": "{:.3f}",
    "f1": "{:.3f}",
    "no_risk_fp_rate": "{:.3f}",
    "severity_accuracy": "{:.3f}",
    "todo_f1": "{:.3f}",
    "p50_ms": "{:.0f}",
    "answerable": "{:.0f}",
    "negative": "{:.0f}",
    "recall_at_5": "{:.3f}",
    "answer_accuracy": "{:.3f}",
    "citation_precision": "{:.3f}",
    "citation_recall": "{:.3f}",
    "end_to_end": "{:.3f}",
    "abstention": "{:.3f}",
    "groundedness": "{:.3f}",
    "answer_p95_ms": "{:.0f}",
    "executions": "{:.0f}",
    "task_success": "{:.3f}",
    "tool_success": "{:.3f}",
    "p95_ms": "{:.0f}",
    "avg_llm": "{:.2f}",
    "pptx_success": "{:.3f}",
    "pptx_runs": "{:.0f}",
    "task_level_all_rounds": "{:.3f}",
    "dup_rate": "{:.3f}",
}

LABELS = {
    "precision": "风险 Precision",
    "recall": "风险 Recall",
    "f1": "风险 F1",
    "no_risk_fp_rate": "无风险会议误报率",
    "severity_accuracy": "风险等级准确率",
    "todo_f1": "待办 F1",
    "recall_at_5": "RAG Recall@5",
    "answer_accuracy": "RAG 答案正确率",
    "citation_precision": "RAG 引用准确率",
    "citation_recall": "RAG 引用覆盖率",
    "end_to_end": "RAG 端到端正确率",
    "groundedness": "Groundedness",
    "task_success": "Agent 任务成功率",
    "tool_success": "工具调用成功率",
    "pptx_success": "PPT 任务成功率",
    "p95_ms": "Agent P95(ms)",
    "p50_ms": "P50(ms)",
    "avg_llm": "平均 LLM 调用",
}


def main() -> int:
    lines: list[str] = ["# A/B 对比表（机器生成）", ""]
    for suite, pairs in PAIRS.items():
        for split, (b_sub, o_sub) in pairs.items():
            before = _load(BASE, b_sub, suite)
            root = OPT if suite == "agent" else OPT_FINAL
            after = _load(root, o_sub, suite)
            if not before or not after:
                lines.append(f"## {suite} / {split}\n\n（缺少结果目录）\n")
                continue
            mb = EXTRACTORS[suite](before)
            ma = EXTRACTORS[suite](after)
            lines.append(f"## {suite} / {split}")
            lines.append("")
            lines.append("| 指标 | 优化前 | 优化后 | 变化 |")
            lines.append("| --- | --- | --- | --- |")
            for key in mb:
                if key not in LABELS:
                    continue
                vb, va = mb[key], ma[key]
                delta = va - vb
                fmt = FMT.get(key, "{:.3f}")
                lines.append(
                    f"| {LABELS[key]} | {fmt.format(vb)} | {fmt.format(va)} | "
                    f"{delta:+.3f} |"
                )
            extra = []
            if suite == "meeting":
                extra.append(
                    f"- 分母：{ma['meetings']:.0f} 场会议 / {ma['gold_risks']:.0f} 条标准风险；"
                    f"无风险误报 {ma['no_risk_fp']}/{ma['no_risk_total']}；"
                    f"高风险漏检 {ma['high_missed']}/{ma['high_total']}"
                )
            if suite == "rag":
                extra.append(
                    f"- 分母：可回答 {ma['answerable']:.0f} 题 / 负例 {ma['negative']:.0f} 题"
                )
            if suite == "agent":
                extra.append(
                    f"- 分母：{ma['executions']:.0f} 次执行，{ma['passed']:.0f} 次通过；"
                    f"任务级全轮次通过率 {ma['task_level_all_rounds']:.3f}"
                )
            lines.extend(extra)
            lines.append("")
    OUT.write_text("\n".join(lines), encoding="utf-8")
    print(f"已生成 {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
