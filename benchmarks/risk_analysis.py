"""会议风险错误分析：把每个误报/漏报及其原文、依据、模型输出导出成可读清单。

用法::

    python -m benchmarks.risk_analysis benchmarks/results/baseline_v1/meeting_dev

输出 ``docs/_risk_cases.json``（供人工逐条归类）与终端摘要。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from benchmarks import common

MEET_DIR = common.DATASETS_DIR / "meetings"
OUT_PATH = common.PROJECT_ROOT / "docs" / "_risk_cases.json"


def _text_for(meeting_id: str) -> str:
    for sub in ("meetings", "unseen/meetings"):
        path = MEET_DIR / sub / f"{meeting_id}.txt"
        if path.is_file():
            return path.read_text(encoding="utf-8")
    return ""


def collect(directory: Path) -> dict[str, Any]:
    payload = json.loads((directory / "meeting_results.json").read_text(encoding="utf-8"))
    rows = []
    for case in payload["cases"]:
        risks = case["risks"]
        pred = case["prediction"].get("risks") or []
        gold = case["gold"]["risks"]
        if not (risks["spurious"] or risks["missed"]):
            continue
        rows.append(
            {
                "meeting_id": case["meeting_id"],
                "category": case["category"],
                "gold_risks": [
                    {
                        "risk_id": g["risk_id"],
                        "type": g["risk_type"],
                        "severity": g["severity"],
                        "evidence": g["evidence"],
                    }
                    for g in gold
                ],
                "spurious": [
                    {
                        "title": s["title"],
                        "pred_type": s["pred_type"],
                        "level": pred[s["index"]].get("level"),
                        "description": pred[s["index"]].get("description"),
                        "evidence": pred[s["index"]].get("evidence"),
                    }
                    for s in risks["spurious"]
                ],
                "missed": risks["missed"],
                "severity_errors": [
                    {
                        "risk_id": m["risk_id"],
                        "gold": m["gold_severity"],
                        "pred": m["pred_severity"],
                        "title": m["pred_title"],
                    }
                    for m in risks["matched"]
                    if not m["severity_ok"]
                ],
                "text": _text_for(case["meeting_id"]),
            }
        )
    return {"source": str(directory), "meetings": len(payload["cases"]), "rows": rows}


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    data = collect(Path(argv[0]))
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    total_spurious = sum(len(r["spurious"]) for r in data["rows"])
    total_missed = sum(len(r["missed"]) for r in data["rows"])
    total_sev = sum(len(r["severity_errors"]) for r in data["rows"])
    print(f"会议 {data['meetings']} 场，问题场次 {len(data['rows'])}")
    print(f"  误报 {total_spurious} 条 / 漏报 {total_missed} 条 / 等级错误 {total_sev} 条")
    print(f"  明细：{OUT_PATH}")
    for row in data["rows"]:
        print(f"\n[{row['meeting_id']}] {row['category']}")
        for item in row["spurious"]:
            print(f"  误报 {item['title']} | {item['pred_type']} | {item['level']}")
            print(f"       依据：{(item['evidence'] or '（空）')[:90]}")
        for missed in row["missed"]:
            print(f"  漏报 {missed}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
