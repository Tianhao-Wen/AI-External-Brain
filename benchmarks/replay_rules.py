"""离线复放：对**已有的预测结果**重新施加确定性规则，零额外模型调用地量化规则贡献。

为什么要这样做：模型每次采样不同，直接跑两遍无法区分"规则的效果"和"采样噪声"。
把同一批预测分别按"有规则/无规则"评分，才是干净的对照。

用法::

    python -m benchmarks.replay_rules benchmarks/results/baseline_v1/meeting_dev
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from benchmarks import common
from benchmarks.evaluators import meeting as meeting_eval

MEET_DIR = common.DATASETS_DIR / "meetings"


def _text(meeting_id: str) -> str:
    for sub in ("meetings", "unseen/meetings"):
        path = MEET_DIR / sub / f"{meeting_id}.txt"
        if path.is_file():
            return path.read_text(encoding="utf-8")
    return ""


def replay(directory: Path) -> dict[str, Any]:
    from aieb.brain import risk_rules

    payload = json.loads((directory / "meeting_results.json").read_text(encoding="utf-8"))
    cases_raw = []
    cases_filtered = []
    for case in payload["cases"]:
        gold = case["gold"]
        pred = case["prediction"]
        raw_text = _text(case["meeting_id"])
        candidates = list(pred.get("risks") or [])
        kept, dropped = risk_rules.filter_risks(candidates, raw_text)

        base = {
            "meeting_id": case["meeting_id"],
            "title": case["title"],
            "category": case["category"],
            "gold": gold,
        }
        cases_raw.append({**base, "prediction": {**pred, "risks": candidates},
                          "risks": meeting_eval.evaluate_risks(gold, {**pred, "risks": candidates}, raw_text)})
        bj = None
        for d in dropped:
            bj = (bj or "") + f"{d.get('title')}（{d.get('dropped_reason')}）；"
        cases_filtered.append({**base, "prediction": {**pred, "risks": kept},
                               "risks": meeting_eval.evaluate_risks(gold, {**pred, "risks": kept}, raw_text)})

    def summarize(cases):
        tp = sum(c["risks"]["tp"] for c in cases)
        fp = sum(c["risks"]["fp"] for c in cases)
        fn = sum(c["risks"]["fn"] for c in cases)
        no_risk = [c for c in cases if c["risks"]["truly_no_risk"]]
        return {
            "tp": tp,
            "fp": fp,
            "fn": fn,
            "precision": round(tp / (tp + fp), 4) if tp + fp else 0.0,
            "recall": round(tp / (tp + fn), 4) if tp + fn else 0.0,
            "f1": round(2 * tp / (2 * tp + fp + fn), 4) if tp else 0.0,
            "no_risk_fp": sum(1 for c in no_risk if c["risks"]["no_risk_false_positive"]),
            "no_risk_total": len(no_risk),
            "high_missed": sum(c["risks"]["high_risk_missed"] for c in cases),
            "high_total": sum(c["risks"]["high_risk_total"] for c in cases),
        }

    # 逐条看规则到底删了什么：删掉的是 FP 还是 TP
    dropped_details = []
    for raw_case, filt_case in zip(cases_raw, cases_filtered):
        if raw_case["risks"]["tp"] != filt_case["risks"]["tp"]:
            dropped_details.append(
                {
                    "meeting_id": raw_case["meeting_id"],
                    "tp_before": raw_case["risks"]["tp"],
                    "tp_after": filt_case["risks"]["tp"],
                    "missed_after": filt_case["risks"]["missed"],
                }
            )

    return {
        "source": str(directory),
        "meetings": len(cases_raw),
        "without_rules": summarize(cases_raw),
        "with_rules": summarize(cases_filtered),
        "tp_lost_cases": dropped_details,
    }


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    out = {}
    for item in argv:
        directory = Path(item)
        result = replay(directory)
        out[str(directory)] = result
        a, b = result["without_rules"], result["with_rules"]
        print(f"\n=== {directory}（{result['meetings']} 场）")
        print(f"  无规则：P={a['precision']:.3f} R={a['recall']:.3f} F1={a['f1']:.3f} "
              f"FP={a['fp']} 无风险误报={a['no_risk_fp']}/{a['no_risk_total']}")
        print(f"  有规则：P={b['precision']:.3f} R={b['recall']:.3f} F1={b['f1']:.3f} "
              f"FP={b['fp']} 无风险误报={b['no_risk_fp']}/{b['no_risk_total']}")
        print(f"  高风险漏检：{a['high_missed']}/{a['high_total']} -> {b['high_missed']}/{b['high_total']}")
        if result["tp_lost_cases"]:
            print(f"  ！规则导致 TP 下降的场次：{result['tp_lost_cases']}")
        else:
            print("  规则未删除任何命中项（TP 不变）")
    common.RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    (common.RESULTS_DIR / "replay_rules.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\n明细：{common.RESULTS_DIR / 'replay_rules.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
