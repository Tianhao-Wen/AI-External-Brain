"""会议分析套件运行器：真实调用 ``BrainService.analyze_meeting``，规则化评分。

fixture 模式下不调用模型：直接用"空预测"跑一遍链路，确保
评分器、输出格式、失败样本处理都可以在没有 API Key 的环境里验证；
**fixture 的分数绝不能当作模型能力写进报告。**
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from benchmarks import common
from benchmarks.evaluators import meeting as meeting_eval
from benchmarks.evaluators.metrics import percent

MEET_DIR = common.DATASETS_DIR / "meetings"
TEXT_DIR = MEET_DIR / "meetings"
GOLD_PATH = MEET_DIR / "gold.jsonl"
UNSEEN_DIR = MEET_DIR / "unseen"


def load_gold(limit: int = 0, only: list[str] | None = None, split: str = "all") -> list[dict[str, Any]]:
    if split == "test_unseen":
        gold_path, text_dir = UNSEEN_DIR / "gold.jsonl", UNSEEN_DIR / "meetings"
    else:
        gold_path, text_dir = GOLD_PATH, TEXT_DIR
    if not gold_path.is_file():
        raise FileNotFoundError(
            f"缺少 {gold_path}，请先运行对应的 build 脚本生成会议数据集"
        )
    rows = common.read_jsonl(gold_path)
    if split not in ("", "all", "test_unseen"):
        split_path = MEET_DIR / "splits.json"
        if not split_path.is_file():
            raise FileNotFoundError("缺少 meetings/splits.json，请先运行 python -m benchmarks.splits")
        ids = set(json.loads(split_path.read_text(encoding="utf-8"))["splits"].get(split) or [])
        rows = [r for r in rows if r["meeting_id"] in ids]
    if only:
        wanted = {item.strip() for item in only}
        rows = [r for r in rows if r["meeting_id"] in wanted]
    if limit:
        rows = rows[:limit]
    return rows


def text_dir_for(split: str) -> Path:
    return UNSEEN_DIR / "meetings" if split == "test_unseen" else TEXT_DIR


async def run(
    ctx,
    *,
    limit: int = 0,
    only: list[str] | None = None,
    split: str = "all",
) -> dict[str, Any]:
    from aieb.brain.service import BrainService

    gold_rows = load_gold(limit=limit, only=only, split=split)
    if not gold_rows:
        raise RuntimeError(f"split={split} 没有匹配会议")
    text_dir = text_dir_for(split)
    workspace = ctx.workspace("meeting")
    service = BrainService(work_dir=workspace, provider=ctx.provider)
    live = ctx.mode == "live"
    cases: list[dict[str, Any]] = []
    try:
        for index, gold in enumerate(gold_rows, start=1):
            meeting_id = gold["meeting_id"]
            text_path = text_dir / f"{meeting_id}.txt"
            raw_text = text_path.read_text(encoding="utf-8")

            started = time.perf_counter()
            prediction: dict[str, Any] = {"risks": [], "todos": [], "decisions": [], "error": ""}
            reused = False
            if live:
                try:
                    result = await service.analyze_meeting(text_path.name, raw_text)
                    reused = bool(result.get("reused"))
                    prediction = {
                        "title": result.get("title", ""),
                        "risks": result.get("risks") or [],
                        "todos": result.get("todos") or [],
                        "decisions": result.get("decisions") or [],
                        "summary": result.get("summary", ""),
                        "error": "",
                    }
                except common.BudgetExceeded as exc:
                    prediction["error"] = f"budget: {exc}"
                except Exception as exc:  # noqa: BLE001 - 失败样本必须保留
                    prediction["error"] = f"{type(exc).__name__}: {exc}"
            else:
                prediction["error"] = "fixture 模式未调用模型"

            latency_ms = (time.perf_counter() - started) * 1000

            risks = meeting_eval.evaluate_risks(gold, prediction, raw_text)
            todos = meeting_eval.evaluate_todos(gold, prediction, raw_text)
            decisions = meeting_eval.evaluate_decisions(gold, prediction)

            cases.append(
                {
                    "meeting_id": meeting_id,
                    "title": gold["title"],
                    "category": gold["category"],
                    "text_chars": len(raw_text),
                    "gold": {
                        "risks": gold["risks"],
                        "decisions": gold["decisions"],
                        "todos": gold["todos"],
                    },
                    "prediction": prediction,
                    "reused": reused,
                    "risks": risks,
                    "todos": todos,
                    "decisions": decisions,
                    "latency_ms": round(latency_ms, 1),
                    "index": index,
                }
            )
            print(
                f"  [{index}/{len(gold_rows)}] {meeting_id} {gold['category']:<18} "
                f"risk R={risks['recall']:.2f} P={risks['precision']:.2f} "
                f"gold={risks['gold_count']} pred={risks['pred_count']}"
                f"{'  ERROR: ' + prediction['error'][:60] if prediction['error'] and live else ''}",
                flush=True,
            )

        aggregates = {
            "overall": meeting_eval.aggregate(cases),
            "by_category": meeting_eval.aggregate_by_category(cases),
            "latency": {
                "p50_ms": common.percentile([c["latency_ms"] for c in cases], 50),
                "p95_ms": common.percentile([c["latency_ms"] for c in cases], 95),
                "total_s": round(sum(c["latency_ms"] for c in cases) / 1000.0, 1),
            },
        }
        return {"cases": cases, "aggregates": aggregates, "mode": ctx.mode}
    finally:
        service.store.close()


CSV_COLUMNS = [
    "meeting_id",
    "category",
    "title",
    "gold_risks",
    "pred_risks",
    "risk_tp",
    "risk_fp",
    "risk_fn",
    "risk_precision",
    "risk_recall",
    "risk_f1",
    "high_risk_missed",
    "missed_ids",
    "spurious_titles",
    "severity_accuracy",
    "evidence_grounded_rate",
    "type_accuracy",
    "todo_tp",
    "todo_fp",
    "todo_fn",
    "todo_precision",
    "todo_recall",
    "owner_accuracy",
    "deadline_accuracy",
    "decision_tp",
    "decision_fn",
    "all_decisions_found",
    "latency_ms",
    "error",
]


def to_csv_rows(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for case in cases:
        r, t, d = case["risks"], case["todos"], case["decisions"]
        rows.append(
            {
                "meeting_id": case["meeting_id"],
                "category": case["category"],
                "title": case["title"],
                "gold_risks": r["gold_count"],
                "pred_risks": r["pred_count"],
                "risk_tp": r["tp"],
                "risk_fp": r["fp"],
                "risk_fn": r["fn"],
                "risk_precision": r["precision"],
                "risk_recall": r["recall"],
                "risk_f1": r["f1"],
                "high_risk_missed": r["high_risk_missed"],
                "missed_ids": r["missed"],
                "spurious_titles": [s["title"] for s in r["spurious"]],
                "severity_accuracy": r["severity_accuracy"],
                "evidence_grounded_rate": r["evidence_grounded_rate"],
                "type_accuracy": r["type_accuracy"],
                "todo_tp": t["tp"],
                "todo_fp": t["fp"],
                "todo_fn": t["fn"],
                "todo_precision": t["precision"],
                "todo_recall": t["recall"],
                "owner_accuracy": t["owner_accuracy"],
                "deadline_accuracy": t["deadline_accuracy"],
                "decision_tp": d["tp"],
                "decision_fn": d["fn"],
                "all_decisions_found": d["all_found"],
                "latency_ms": case["latency_ms"],
                "error": common.redact(case["prediction"].get("error") or ""),
            }
        )
    return rows


def summary_markdown(result: dict[str, Any], ctx) -> str:
    agg = result["aggregates"]
    o = agg["overall"]
    risk = o["risk"]
    todo = o["todos"]
    dec = o["decisions"]

    def p(value) -> str:
        return "—" if value is None else percent(value)

    lines = [
        "# 会议分析评测结果摘要",
        "",
        f"- 运行 ID：`{ctx.run_id}`",
        f"- 模式：`{ctx.mode}`（live = 真实模型抽取；fixture = 不调用模型，仅验证链路）",
        f"- 模型：`{ctx.model.model}`",
        f"- Git commit：`{ctx.git.get('commit_short')}`",
        f"- 会议数：{o['meetings']}（无风险负例 {o['no_risk_meetings']}，含风险 {o['risky_meetings']}）",
        f"- 分析失败：{o['failed_meetings']} 场（失败样本保留在分母中）",
        f"- 平均耗时：P50 {agg['latency']['p50_ms']:.0f} ms / P95 {agg['latency']['p95_ms']:.0f} ms",
        "",
        "## 风险抽取",
        "",
        f"- 标准风险 {risk['gold_total']} 条，模型输出 {risk['pred_total']} 条",
        f"- Precision：{p(risk['precision'])}　Recall：{p(risk['recall'])}　F1：{p(risk['f1'])}",
        f"- 高风险漏报：{risk['high_risk_missed']} / {risk['high_risk_total']}"
        f"（漏报率 {p(risk['high_risk_miss_rate'])}）",
        f"- 无风险会议误报：{risk['no_risk_false_positive_meetings']} / {o['no_risk_meetings']}"
        f"（误报率 {p(risk['no_risk_false_positive_rate'])}）",
        f"- 风险等级正确率：{p(risk['severity_accuracy'])}",
        f"- 依据（evidence）可在原文找到：{p(risk['evidence_grounded_rate'])}",
        f"- 依据指向正确事实：{p(risk['evidence_relevant_rate'])}",
        f"- 风险类型判定正确率：{p(risk['type_accuracy'])}",
        f"- 会议级完全正确（无漏报且无误报）：{p(risk['exact_meeting_rate'])}",
        "",
        "### 按风险类型",
        "",
        "TP/FN 来自类型无关的一对一匹配（只看这条风险有没有被检出）；"
        "Precision 的分母需要把模型输出归类，依赖评测侧的规则分类器。",
        "",
        "| 类型 | 标准条数 | 模型归入条数 | Precision | Recall | F1 | TP | FP | FN |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for name, item in o["risk_per_type"].items():
        lines.append(
            f"| {name} | {item['gold']} | {item['pred']} | {p(item['precision'])} | {p(item['recall'])} | "
            f"{p(item['f1'])} | {item['tp']} | {item['fp']} | {item['fn']} |"
        )

    lines += [
        "",
        "### 风险等级混淆矩阵（行=标准，列=模型）",
        "",
        "| 标准 \\ 模型 | high | medium | low |",
        "| --- | --- | --- | --- |",
    ]
    for gold_level in ("high", "medium", "low"):
        row = risk["severity_confusion"][gold_level]
        lines.append(f"| {gold_level} | {row['high']} | {row['medium']} | {row['low']} |")

    lines += [
        "",
        "## 待办抽取",
        "",
        f"- 标准待办 {todo['gold_total']} 条，模型输出 {todo['pred_total']} 条",
        f"- Precision：{p(todo['precision'])}　Recall：{p(todo['recall'])}　F1：{p(todo['f1'])}",
        f"- 负责人正确率：{p(todo['owner_accuracy'])}",
        f"- 截止时间正确率：{p(todo['deadline_accuracy'])}",
        f"- 编造负责人（原文无此人）次数：{todo['owner_fabricated_total']}",
        f"- 原文未指定却填了负责人的次数：{todo['owner_false_fill_total']}",
        f"- 编造截止时间次数：{todo['deadline_fabricated_total']}",
        "",
        "## 决策抽取",
        "",
        f"- 标准决策 {dec['gold_total']} 条，模型输出 {dec['pred_total']} 条",
        f"- Precision：{p(dec['precision'])}　Recall：{p(dec['recall'])}　F1：{p(dec['f1'])}",
        f"- 单场会议全部决策都抽到的比例：{p(dec['meeting_all_found_rate'])}",
        f"- 撤回/否定类冲突（把已撤销的旧决议当最终结论）：{dec['retracted_conflicts']} 次",
        "",
        "## 按会议类别",
        "",
        "| 类别 | 场次 | 标准风险 | 模型输出 | Precision | Recall | F1 |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for name, item in agg["by_category"].items():
        lines.append(
            f"| {name} | {item['meetings']} | {item['gold_risks']} | {item['pred_risks']} | "
            f"{p(item['precision'])} | {p(item['recall'])} | {p(item['f1'])} |"
        )

    failures = [
        c for c in result["cases"]
        if c["risks"]["fn"] or c["risks"]["fp"] or c["prediction"].get("error")
    ]
    lines += ["", "## 失败案例（有漏报 / 误报 / 调用失败）", ""]
    if not failures:
        lines.append("无。")
    for case in failures[:25]:
        detail = []
        if case["risks"]["missed"]:
            detail.append("漏报：" + "、".join(case["risks"]["missed"]))
        if case["risks"]["spurious"]:
            detail.append("误报：" + "、".join(s["title"] for s in case["risks"]["spurious"]))
        if case["prediction"].get("error"):
            detail.append("失败：" + common.redact(case["prediction"]["error"])[:80])
        lines.append(f"- `{case['meeting_id']}`（{case['category']}）：" + "；".join(detail))
    lines.append("")
    return "\n".join(lines)


def write_outputs(result: dict[str, Any], ctx) -> dict[str, Path]:
    payload = {
        "meta": ctx.meta(),
        "aggregates": result["aggregates"],
        "cases": result["cases"],
    }
    return {
        "json": common.write_json(common.RESULTS_DIR / "meeting_results.json", payload),
        "csv": common.write_csv(
            common.RESULTS_DIR / "meeting_cases.csv", to_csv_rows(result["cases"]), CSV_COLUMNS
        ),
        "summary": common.write_markdown(
            common.RESULTS_DIR / "meeting_summary.md", summary_markdown(result, ctx)
        ),
    }
