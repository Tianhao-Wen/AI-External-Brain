"""从 Top-K 消融结果 JSON 生成图表与机器可读表格。

运行::

    python -m benchmarks.ablations.report_topk

产物：

* ``benchmarks/reports/chart_topk_*.svg`` —— 5 张图，数据全部来自结果 JSON
* ``docs/_topk_tables.md`` —— 报告里引用的表格，机器生成，不手工填数

刻意不引入 matplotlib：与 ``benchmarks/charts.py`` 保持同一套纯 SVG 方案，
保证在干净环境里也能一键复现。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from benchmarks import common
from benchmarks.charts import grouped_bar_chart

RESULTS = common.RESULTS_DIR / "topk"
REPORTS = common.REPORTS_DIR
DOCS = common.PROJECT_ROOT / "docs"

RUNS = {
    "full": "topk_full",
    "stability": "topk_full_stability",
    "unseen": "topk_unseen",
}


def load(name: str, kind: str = "qa") -> dict[str, Any] | None:
    path = RESULTS / f"{name}_{kind}_summary.json"
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _pct(value: Any, digits: int = 1) -> str:
    return f"{value * 100:.{digits}f}%" if isinstance(value, (int, float)) else "—"


def _num(value: Any, digits: int = 0) -> str:
    return f"{value:.{digits}f}" if isinstance(value, (int, float)) else "—"


def chart_retrieval(full: dict[str, Any]) -> Path:
    agg = full["retrieval_aggregates"]
    ks = [str(k) for k in full["ks"]]
    return grouped_bar_chart(
        title="RAG Top-K 消融：检索层召回（50 题，DOC-A~DOC-D）",
        subtitle="Recall@K=Top-K 覆盖标准证据页的比例；全证据率=Top-K 覆盖全部证据页的题目比例",
        categories=[f"K={k}" for k in ks],
        series=[
            ("Recall@K", [agg[k]["recall"] for k in ks]),
            ("Hit@K", [agg[k]["hit"] for k in ks]),
            ("全证据率", [agg[k]["full_evidence_rate"] for k in ks]),
            ("MRR@10", [agg[k]["mrr@10"] for k in ks]),
        ],
        path=REPORTS / "chart_topk_retrieval.svg",
    )


def chart_quality(full: dict[str, Any]) -> Path:
    agg = full["aggregates"]
    ks = [str(k) for k in full["ks"]]
    return grouped_bar_chart(
        title="RAG Top-K 消融：回答质量与引用准确性（真实 LLM）",
        subtitle="答对率/引用指标只统计可答题；拒答率只统计负例题；端到端=全部题目",
        categories=[f"K={k}" for k in ks],
        series=[
            ("答对率", [agg[k]["answer_accuracy"] for k in ks]),
            ("端到端", [agg[k]["end_to_end"] for k in ks]),
            ("拒答准确率", [agg[k]["abstention_accuracy"] for k in ks]),
            ("引用准确率", [agg[k]["citation_precision"] for k in ks]),
            ("引用召回率", [agg[k]["citation_recall"] for k in ks]),
        ],
        path=REPORTS / "chart_topk_quality.svg",
    )


def chart_tokens(full: dict[str, Any]) -> Path:
    agg = full["aggregates"]
    ks = [str(k) for k in full["ks"]]
    peak = max(agg[k]["avg_prompt_tokens"] or 0 for k in ks) or 1
    return grouped_bar_chart(
        title="RAG Top-K 消融：每次问答的 Token 成本",
        subtitle="prompt tokens = 未缓存输入 + provider 缓存命中；输出 tokens 为模型生成的回答长度",
        categories=[f"K={k}" for k in ks],
        series=[
            ("prompt tokens（全部）", [agg[k]["avg_prompt_tokens"] for k in ks]),
            ("其中未缓存输入", [agg[k]["avg_input_tokens"] for k in ks]),
            ("输出 tokens", [agg[k]["avg_output_tokens"] for k in ks]),
        ],
        path=REPORTS / "chart_topk_tokens.svg",
        max_value=peak,
    )


def chart_latency(full: dict[str, Any]) -> Path:
    agg = full["aggregates"]
    ks = [str(k) for k in full["ks"]]
    peak = max(agg[k]["answer_p95_ms"] or 0 for k in ks) or 1
    return grouped_bar_chart(
        title="RAG Top-K 消融：端到端回答延迟",
        subtitle="BM25 检索耗时与 K 无关（评分需遍历全部分块）；延迟差异来自 prompt 长度",
        categories=[f"K={k}" for k in ks],
        series=[
            ("P50 毫秒", [agg[k]["answer_p50_ms"] for k in ks]),
            ("P95 毫秒", [agg[k]["answer_p95_ms"] for k in ks]),
        ],
        path=REPORTS / "chart_topk_latency.svg",
        max_value=peak,
    )


def chart_by_type(full: dict[str, Any]) -> Path:
    ks = [str(k) for k in full["ks"]]
    by_type = full["by_type"]
    types = sorted(by_type[ks[0]].keys())
    series = []
    for k in ks:
        values = []
        for name in types:
            row = by_type[k].get(name) or {}
            value = row.get("answer_accuracy")
            # 纯负例题型（negative）没有"答对率"口径，用端到端代替
            values.append(value if value is not None else row.get("end_to_end"))
        series.append((f"K={k}", values))
    return grouped_bar_chart(
        title="RAG Top-K 消融：分题型答对率",
        subtitle="negative 为纯负例题型，取端到端（正确拒答）口径",
        categories=types,
        series=series,
        path=REPORTS / "chart_topk_by_type.svg",
    )


def chart_stability(stability: dict[str, Any]) -> Path:
    ks = [str(k) for k in stability["ks"]]
    rounds = max((v["rounds"] for v in stability["stability"].values()), default=0)
    series = []
    for index in range(rounds):
        values = []
        for k in ks:
            per_round = stability["stability"][k]["per_round"]
            values.append(per_round[index]["answer_accuracy"] if index < len(per_round) else None)
        series.append((f"第 {index + 1} 轮", values))
    return grouped_bar_chart(
        title="RAG Top-K 消融：重复实验的稳定性（分层 16 题）",
        subtitle="同一份代码、同一批题、同样的 K，只换采样轮次 —— 用于量化模型噪声",
        categories=[f"K={k}" for k in ks],
        series=series,
        path=REPORTS / "chart_topk_stability.svg",
    )


def chart_unseen(unseen: dict[str, Any]) -> Path:
    agg = unseen["aggregates"]
    ret = unseen["retrieval_aggregates"]
    ks = [str(k) for k in unseen["ks"]]
    return grouped_bar_chart(
        title="RAG Top-K 消融：未暴露文档集（23 题，DOC-E/DOC-F）",
        subtitle="文档全新、未参与任何调参；该库只有 8 页，K=10 实际只能取到 8 块",
        categories=[f"K={k}" for k in ks],
        series=[
            ("Recall@K", [ret[k]["recall"] for k in ks]),
            ("答对率", [agg[k]["answer_accuracy"] for k in ks]),
            ("引用准确率", [agg[k]["citation_precision"] for k in ks]),
        ],
        path=REPORTS / "chart_topk_unseen.svg",
    )


def markdown_tables(full: dict[str, Any], stability: dict[str, Any], unseen: dict[str, Any]) -> str:
    lines: list[str] = ["# Top-K 消融：机器生成表格", ""]
    lines.append("> 本文件由 `python -m benchmarks.ablations.report_topk` 从结果 JSON 直接生成，")
    lines.append("> 不得手工修改；数据变化后重跑即可。")
    lines.append("")

    ks = [str(k) for k in full["ks"]]
    ret = full["retrieval_aggregates"]
    lines += ["## 表 1 · 检索层（full，50 题，其中 6 条负例）", ""]
    lines.append("| K | Recall@K | Hit@K | 全证据率 | MRR@10 | 平均进 prompt 块数 | 平均上下文字符 | 被预算截断题数 |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- |")
    for k in ks:
        r = ret[k]
        lines.append(
            f"| {k} | {_pct(r['recall'])} | {_pct(r['hit'])} | {_pct(r['full_evidence_rate'])} | "
            f"{_num(r['mrr@10'], 3)} | {_num(r['avg_delivered_chunks'], 1)} | "
            f"{_num(r['avg_context_chars'])} | {r['truncated_questions']} |"
        )
    lines.append("")

    agg = full["aggregates"]
    lines += ["## 表 2 · 回答层（full，真实 LLM）", ""]
    lines.append("| K | 答对率 | 拒答准确率 | 端到端 | 引用准确率 | 引用召回率 | 严格引用 | 依据度 | prompt tokens | 未缓存输入 | 输出 tokens | P50 ms | P95 ms |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for k in ks:
        a = agg[k]
        lines.append(
            f"| {k} | {_pct(a['answer_accuracy'])} | {_pct(a['abstention_accuracy'])} | "
            f"{_pct(a['end_to_end'])} | {_pct(a['citation_precision'])} | {_pct(a['citation_recall'])} | "
            f"{_pct(a['strict_citation_accuracy'])} | {_pct(a['groundedness'])} | "
            f"{_num(a['avg_prompt_tokens'])} | {_num(a['avg_input_tokens'])} | "
            f"{_num(a['avg_output_tokens'])} | {_num(a['answer_p50_ms'])} | {_num(a['answer_p95_ms'])} |"
        )
    lines.append("")

    lines += ["## 表 3 · 分题型答对率（full）", ""]
    types = sorted(full["by_type"][ks[0]].keys())
    lines.append("| 题型 | " + " | ".join(f"K={k}" for k in ks) + " |")
    lines.append("| --- | " + " | ".join("---" for _ in ks) + " |")
    for name in types:
        cells = []
        for k in ks:
            row = full["by_type"][k].get(name) or {}
            value = row.get("answer_accuracy")
            cells.append(_pct(value) if value is not None else _pct(row.get("end_to_end")) + "*")
        lines.append(f"| {name} | " + " | ".join(cells) + " |")
    lines.append("")
    lines.append("`*` 纯负例题型（negative），取端到端=正确拒答率。")
    lines.append("")

    lines += ["## 表 4 · 重复实验稳定性（分层 16 题 × 3 轮）", ""]
    sks = [str(k) for k in stability["ks"]]
    lines.append("| K | 第 1 轮 | 第 2 轮 | 第 3 轮 | 三轮均值 | 极差 |")
    lines.append("| --- | --- | --- | --- | --- | --- |")
    for k in sks:
        s = stability["stability"][k]
        rounds = [_pct(r["answer_accuracy"]) for r in s["per_round"]]
        while len(rounds) < 3:
            rounds.append("—")
        lines.append(
            f"| {k} | {rounds[0]} | {rounds[1]} | {rounds[2]} | "
            f"{_pct(s['answer_accuracy_mean'])} | {_pct(s['answer_accuracy_range'])} |"
        )
    lines.append("")

    uks = [str(k) for k in unseen["ks"]]
    uret = unseen["retrieval_aggregates"]
    uagg = unseen["aggregates"]
    lines += ["## 表 5 · 未暴露文档集（23 题，DOC-E/DOC-F）", ""]
    lines.append("| K | Recall@K | 全证据率 | 答对率 | 拒答准确率 | 引用准确率 | prompt tokens | P95 ms |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- |")
    for k in uks:
        lines.append(
            f"| {k} | {_pct(uret[k]['recall'])} | {_pct(uret[k]['full_evidence_rate'])} | "
            f"{_pct(uagg[k]['answer_accuracy'])} | {_pct(uagg[k]['abstention_accuracy'])} | "
            f"{_pct(uagg[k]['citation_precision'])} | {_num(uagg[k]['avg_prompt_tokens'])} | "
            f"{_num(uagg[k]['answer_p95_ms'])} |"
        )
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    full = load(RUNS["full"])
    full_ret = load(RUNS["full"], "retrieval")
    stability = load(RUNS["stability"])
    unseen = load(RUNS["unseen"])
    unseen_ret = load(RUNS["unseen"], "retrieval")
    if not (full and full_ret and stability and unseen and unseen_ret):
        print("缺少结果文件，请先跑完 live 与 fixture 消融实验")
        return 1

    full_payload = dict(full)
    full_payload["retrieval_aggregates"] = full_ret["aggregates"]
    unseen_payload = dict(unseen)
    unseen_payload["retrieval_aggregates"] = unseen_ret["aggregates"]

    paths = [
        chart_retrieval(full_payload),
        chart_quality(full_payload),
        chart_tokens(full_payload),
        chart_latency(full_payload),
        chart_by_type(full_payload),
        chart_stability(stability),
        chart_unseen(unseen_payload),
    ]
    tables = DOCS / "_topk_tables.md"
    tables.parent.mkdir(parents=True, exist_ok=True)
    tables.write_text(markdown_tables(full_payload, stability, unseen_payload), encoding="utf-8")
    for path in paths:
        print(f"  chart: {path}")
    print(f"  tables: {tables}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
