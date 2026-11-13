"""把多个结果目录汇总成一张表（基线 / 优化后都用它，保证口径一致）。

用法::

    python -m benchmarks.summarize benchmarks/results/baseline_v1/meeting_dev ...
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


def _load(directory: Path, suite: str) -> dict[str, Any] | None:
    path = directory / f"{suite}_results.json"
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def rag_row(directory: Path) -> list[str]:
    data = _load(directory, "rag")
    if not data:
        return []
    agg = data["aggregates"]
    ret, ans = agg["retrieval"], agg.get("answer") or {}
    if not ans.get("executed"):
        return []
    return [
        str(directory),
        f"{ret['answerable_questions']}",
        f"{ret['recall@5']:.3f}",
        f"{ans['answer_accuracy']:.3f}",
        f"{ans['citation_precision']:.3f}",
        f"{ans['citation_recall']:.3f}",
        f"{ans['end_to_end_success']:.3f}",
        f"{ans.get('abstention_accuracy') if ans.get('abstention_accuracy') is not None else float('nan'):.3f}",
        f"{ans['groundedness']:.3f}",
    ]


def meeting_row(directory: Path) -> list[str]:
    data = _load(directory, "meeting")
    if not data:
        return []
    o = data["aggregates"]["overall"]
    r = o["risk"]
    return [
        str(directory),
        f"{o['meetings']}",
        f"{r['gold_total']}",
        f"{r['precision']:.3f}",
        f"{r['recall']:.3f}",
        f"{r['f1']:.3f}",
        f"{r['no_risk_false_positive_rate']:.3f}",
        f"{r['high_risk_missed']}/{r['high_risk_total']}",
        f"{r['severity_accuracy']:.3f}",
        f"{o['todos']['f1']:.3f}",
        f"{data['aggregates']['latency']['p50_ms']:.0f}",
    ]


def agent_row(directory: Path) -> list[str]:
    data = _load(directory, "agent")
    if not data:
        return []
    a = data["aggregates"]
    cases = data["cases"]
    per_task: dict[str, list[bool]] = {}
    for c in cases:
        per_task.setdefault(c["task_id"], []).append(bool(c["success"]))
    pptx = [c for c in cases if c["type"] == "pptx"]
    return [
        str(directory),
        f"{len(cases)}",
        f"{a['task_success_rate']:.3f}",
        f"{sum(1 for c in cases if c['success'])}/{len(cases)}",
        f"{a['tool_call_success_rate']:.3f}",
        f"{a['latency_p50_ms']:.0f}",
        f"{a['latency_p95_ms']:.0f}",
        f"{a['avg_llm_calls']:.2f}",
        f"{(sum(1 for c in pptx if c['success']) / len(pptx)) if pptx else float('nan'):.3f}",
        f"{sum(1 for v in per_task.values() if all(v))}/{len(per_task)}",
    ]


HEADERS = {
    "rag": ["目录", "题数", "Recall@5", "答案正确", "引用准确", "引用覆盖", "端到端", "拒答", "Grounded"],
    "meeting": ["目录", "场次", "标准风险", "Precision", "Recall", "F1", "无风险误报", "高风险漏检", "等级准确", "待办F1", "P50(ms)"],
    "agent": ["目录", "执行次数", "任务成功率", "通过/总数", "工具成功率", "P50(ms)", "P95(ms)", "平均LLM", "PPT成功率", "任务级全过"],
}


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    for suite, row_fn in (("rag", rag_row), ("meeting", meeting_row), ("agent", agent_row)):
        rows = [row_fn(Path(item)) for item in argv]
        rows = [r for r in rows if r]
        if not rows:
            continue
        print(f"\n### {suite}")
        print(" | ".join(HEADERS[suite]))
        for row in rows:
            print(" | ".join(row))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
