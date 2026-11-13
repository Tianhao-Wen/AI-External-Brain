"""从真实结果 JSON 生成图表（纯 Python 输出 SVG，不依赖 matplotlib）。

运行::

    python -m benchmarks.charts

产物（``benchmarks/reports/``）：

* ``chart_rag_metrics.svg``      RAG 检索层 / 回答层指标
* ``chart_meeting_prf.svg``      会议风险抽取 P/R/F1（总体 + 分类型）
* ``chart_agent_success.svg``    Agent 各任务类型成功率

图表数据全部从 ``benchmarks/results/*.json`` 读取，**不做任何手工填数**，
所以结果 JSON 变了图必然跟着变，不可能出现"图和数据对不上"。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Sequence

from benchmarks import common

OUT_DIR = common.REPORTS_DIR

FONT = "'Microsoft YaHei','PingFang SC','Noto Sans CJK SC',sans-serif"
PALETTE = ("#2f6feb", "#e8710a", "#1a9c62", "#8b5cf6", "#c2410c", "#0891b2")


def _text(x: float, y: float, content: str, *, size: int = 12, anchor: str = "middle",
          color: str = "#24292f", weight: str = "400", rotate: float | None = None) -> str:
    transform = f' transform="rotate({rotate} {x:.1f} {y:.1f})"' if rotate is not None else ""
    return (
        f'<text x="{x:.1f}" y="{y:.1f}" font-family="{FONT}" font-size="{size}" '
        f'fill="{color}" font-weight="{weight}" text-anchor="{anchor}"{transform}>'
        f"{_esc(content)}</text>"
    )


def _esc(value: str) -> str:
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _rect(x: float, y: float, w: float, h: float, fill: str, *, rx: float = 3) -> str:
    return f'<rect x="{x:.1f}" y="{y:.1f}" width="{max(w, 0):.1f}" height="{max(h, 0):.1f}" rx="{rx}" fill="{fill}"/>'


def grouped_bar_chart(
    *,
    title: str,
    subtitle: str,
    categories: Sequence[str],
    series: Sequence[tuple[str, Sequence[float | None]]],
    path: Path,
    max_value: float = 1.0,
) -> Path:
    """画一张分组柱状图；``None`` 表示"该项不适用"，画成灰色斜线占位而不是 0。"""
    width, height = 1000, 520
    left, right, top, bottom = 70, 30, 92, 128
    plot_w = width - left - right
    plot_h = height - top - bottom
    group_w = plot_w / max(1, len(categories))
    bar_gap = 6
    bar_w = max(8.0, (group_w * 0.72 - bar_gap * (len(series) - 1)) / max(1, len(series)))

    parts: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img">',
        f'<rect width="{width}" height="{height}" fill="#ffffff"/>',
        _text(width / 2, 34, title, size=20, weight="700"),
        _text(width / 2, 58, subtitle, size=12, color="#5b6470"),
    ]

    # 网格与 Y 轴刻度
    grid_steps = 4
    for step in range(grid_steps + 1):
        value = max_value * step / grid_steps
        y = top + plot_h - plot_h * step / grid_steps
        parts.append(
            f'<line x1="{left}" y1="{y:.1f}" x2="{width - right}" y2="{y:.1f}" '
            f'stroke="{"#d0d7de" if step else "#8b949e"}" stroke-width="1"/>'
        )
        parts.append(_text(left - 10, y + 4, f"{value:.2f}", size=11, anchor="end", color="#5b6470"))

    # 柱体
    for index, category in enumerate(categories):
        group_x = left + group_w * index
        cluster_w = bar_w * len(series) + bar_gap * (len(series) - 1)
        start_x = group_x + (group_w - cluster_w) / 2
        for s_index, (_name, values) in enumerate(series):
            value = values[index] if index < len(values) else None
            x = start_x + s_index * (bar_w + bar_gap)
            if value is None:
                parts.append(
                    f'<rect x="{x:.1f}" y="{top + plot_h - 6:.1f}" width="{bar_w:.1f}" height="6" '
                    f'fill="#c9d1d9"/>'
                )
                parts.append(_text(x + bar_w / 2, top + plot_h - 12, "n/a", size=9, color="#8b949e"))
                continue
            bar_h = plot_h * min(1.0, value / max_value) if max_value else 0
            y = top + plot_h - bar_h
            color = PALETTE[s_index % len(PALETTE)]
            parts.append(_rect(x, y, bar_w, bar_h, color))
            label = f"{value * 100:.1f}%" if max_value <= 1.0 else f"{value:.2f}"
            parts.append(
                _text(x + bar_w / 2, y - 6, label, size=10, color="#24292f", weight="600")
            )
        parts.append(
            _text(
                group_x + group_w / 2,
                top + plot_h + 20,
                category,
                size=11,
                color="#24292f",
            )
        )

    # 图例
    legend_y = height - 34
    total_w = 0.0
    for s_index, (name, _values) in enumerate(series):
        w = 20 + len(name) * 12
        total_w += w + 24
    cursor = (width - total_w) / 2
    for s_index, (name, _values) in enumerate(series):
        parts.append(_rect(cursor, legend_y - 10, 14, 14, PALETTE[s_index % len(PALETTE)], rx=3))
        parts.append(_text(cursor + 20, legend_y + 1, name, size=12, anchor="start"))
        cursor += 20 + len(name) * 12 + 24

    parts.append("</svg>")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(p for p in parts if p), encoding="utf-8")
    return path


def _load(name: str) -> dict[str, Any] | None:
    path = common.RESULTS_DIR / name
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def chart_rag() -> Path | None:
    data = _load("rag_results.json")
    if not data:
        return None
    agg = data["aggregates"]
    ret = agg["retrieval"]
    ans = agg.get("answer") or {}
    live = bool(ans.get("executed"))
    categories = ["Recall@1", "Recall@3", "Recall@5", "Hit@5", "MRR@5"]
    retrieval_values = [ret.get("recall@1"), ret.get("recall@3"), ret.get("recall@5"),
                        ret.get("hit@5"), ret.get("mrr@5")]
    series: list[tuple[str, Sequence[float | None]]] = [("检索层", retrieval_values)]
    if live:
        series.append((
            "回答层",
            [
                None,
                None,
                ans.get("citation_recall"),
                ans.get("end_to_end_success"),
                ans.get("answer_accuracy"),
            ],
        ))
    return grouped_bar_chart(
        title="RAG 评测：检索层与回答层指标",
        subtitle=(
            f"{data['meta']['model']['model']}｜mode={data['meta']['mode']}｜"
            f"run={data['meta']['run_id']}｜可回答问题 {ret['answerable_questions']} / "
            f"负例 {ret['negative_questions']}"
        ),
        categories=categories,
        series=series,
        path=OUT_DIR / "chart_rag_metrics.svg",
    )


def chart_meeting() -> Path | None:
    data = _load("meeting_results.json")
    if not data:
        return None
    agg = data["aggregates"]["overall"]
    risk = agg["risk"]
    categories = ["总体", *agg["risk_per_type"].keys()]
    precision = [risk.get("precision")]
    recall = [risk.get("recall")]
    f1 = [risk.get("f1")]
    for item in agg["risk_per_type"].values():
        precision.append(item.get("precision"))
        recall.append(item.get("recall"))
        f1.append(item.get("f1"))
    return grouped_bar_chart(
        title="会议风险抽取：Precision / Recall / F1",
        subtitle=(
            f"{data['meta']['model']['model']}｜会议 {agg['meetings']} 场"
            f"（无风险负例 {agg['no_risk_meetings']}）｜"
            f"标准风险 {risk['gold_total']} 条｜一对一一对匹配"
        ),
        categories=categories,
        series=[("Precision", precision), ("Recall", recall), ("F1", f1)],
        path=OUT_DIR / "chart_meeting_prf.svg",
    )


def chart_agent() -> Path | None:
    data = _load("agent_results.json")
    if not data:
        return None
    agg = data["aggregates"]
    by_type = agg["by_type"]
    categories = list(by_type.keys())
    success = [by_type[name]["success_rate"] for name in categories]
    return grouped_bar_chart(
        title="Agent 端到端任务成功率（按任务类型）",
        subtitle=(
            f"{data['meta']['model']['model']}｜执行 {agg['task_total']} 次｜"
            f"总任务成功率 {agg['task_success_rate'] * 100:.1f}%｜"
            f"工具调用成功率 {agg['tool_call_success_rate'] * 100:.1f}%"
        ),
        categories=categories,
        series=[("任务成功率", success)],
        path=OUT_DIR / "chart_agent_success.svg",
    )


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    made: list[Path] = []
    for builder in (chart_rag, chart_meeting, chart_agent):
        try:
            path = builder()
        except Exception as exc:  # noqa: BLE001 - 单张图失败不影响其他图
            print(f"  ! {builder.__name__} 失败：{type(exc).__name__}: {exc}")
            continue
        if path:
            made.append(path)
            print(f"  {builder.__name__} -> {path}")
        else:
            print(f"  {builder.__name__}: 缺少结果文件，跳过")
    if not made:
        print("没有任何图表生成：请先运行 python -m benchmarks.run")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
