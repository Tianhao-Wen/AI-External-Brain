"""列出某次会议评测中被确定性规则拦截的具体条目（供错误分析文档引用）。

用法::

    python -m benchmarks.diag_drops benchmarks/results/baseline_v1/meeting_dev
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from benchmarks import common


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    from aieb.brain import risk_rules

    payload = json.loads((Path(argv[0]) / "meeting_results.json").read_text(encoding="utf-8"))
    meed_dir = common.DATASETS_DIR / "meetings"
    total = 0
    for case in payload["cases"]:
        pred = case["prediction"].get("risks") or []
        path = meed_dir / "meetings" / f"{case['meeting_id']}.txt"
        if not path.is_file():
            path = meed_dir / "unseen" / "meetings" / f"{case['meeting_id']}.txt"
        text = path.read_text(encoding="utf-8")
        _kept, dropped = risk_rules.filter_risks(pred, text)
        for item in dropped:
            total += 1
            print(f"{case['meeting_id']} | {item['title'][:36]} | {item['dropped_reason']}")
    print(f"\n共拦截 {total} 条")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
