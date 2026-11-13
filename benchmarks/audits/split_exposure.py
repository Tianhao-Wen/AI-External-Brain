"""数据切分暴露审计：确认 test 切分是否曾被用于调参/选择。

做两件事：
1. 扫描 `benchmarks/results/hybrid_rag/` 下所有逐样本 CSV，统计每个切分的题目出现在哪些运行里；
2. 结合 `docs/` 里的调参记录，给出"该切分是否已暴露"的判定。

输出：`benchmarks/results/hybrid_rag/split_exposure_audit.json`
"""

from __future__ import annotations

import csv
import json
from collections import defaultdict
from pathlib import Path

from benchmarks import common

DATASET_DIR = common.DATASETS_DIR / "hybrid_rag"
RESULTS = common.RESULTS_DIR / "hybrid_rag"
SPLITS_PATH = DATASET_DIR / "splits.json"


def main() -> int:
    splits = json.loads(SPLITS_PATH.read_text(encoding="utf-8"))["splits"]
    split_of = {qid: name for name, ids in splits.items() for qid in ids}

    coverage: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    per_file: dict[str, dict[str, int]] = {}
    for path in sorted(RESULTS.glob("*.csv")):
        with path.open(encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
        seen: dict[str, set[str]] = defaultdict(set)
        for row in rows:
            qid = row.get("question_id") or ""
            if qid in split_of:
                seen[split_of[qid]].add(qid)
        per_file[path.name] = {name: len(ids) for name, ids in sorted(seen.items())}
        for name, ids in seen.items():
            coverage[name][path.name] = ids

    report = {
        "splits": {name: len(ids) for name, ids in splits.items()},
        "files_scanned": len(per_file),
        "per_file_coverage": per_file,
        "exposure": {},
    }
    for name, ids in splits.items():
        total = len(ids)
        touched: set[str] = set()
        for file_ids in coverage[name].values():
            touched |= file_ids
        report["exposure"][name] = {
            "questions": total,
            "questions_seen_in_any_run": len(touched),
            "ratio": round(len(touched) / max(1, total), 4),
            "exposed": len(touched) > 0,
        }
    common.write_json(RESULTS / "split_exposure_audit.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2)[:4000])
    print(f"\n结果 → {RESULTS / 'split_exposure_audit.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
