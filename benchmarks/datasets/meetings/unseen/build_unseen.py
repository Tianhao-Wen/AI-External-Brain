"""构建"未暴露测试集"会议数据（14 场），复用开发集的静态自检规则。

运行::

    python -m benchmarks.datasets.meetings.build_unseen

产物::

    benchmarks/datasets/meetings/unseen/meetings/MEET-U01.txt ...
    benchmarks/datasets/meetings/unseen/gold.jsonl
"""

from __future__ import annotations

import json
import sys

from benchmarks.datasets.meetings.build_dataset import TEXT_DIR, validate
from benchmarks.datasets.meetings.unseen.corpus_unseen import UNSEEN_MEETINGS

OUT = TEXT_DIR.parent / "unseen"


def main() -> int:
    problems = validate(UNSEEN_MEETINGS, min_count=10)
    if problems:
        print("未暴露测试集自检未通过：")
        for item in problems:
            print("  -", item)
        return 1

    text_dir = OUT / "meetings"
    text_dir.mkdir(parents=True, exist_ok=True)
    for old in text_dir.glob("*.txt"):
        old.unlink()
    for item in UNSEEN_MEETINGS:
        (text_dir / f"{item['meeting_id']}.txt").write_text(
            item["text"].strip() + "\n", encoding="utf-8"
        )

    rows = [
        {
            "meeting_id": item["meeting_id"],
            "title": item["title"],
            "category": item["category"],
            "source_name": f"{item['meeting_id']}.txt",
            "char_count": len(item["text"]),
            **item["gold"],
        }
        for item in UNSEEN_MEETINGS
    ]
    (OUT / "gold.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8"
    )
    risks = sum(len(r["risks"]) for r in rows)
    no_risk = sum(1 for r in rows if not r["risks"])
    print(f"未暴露会议 {len(rows)} 场，风险 {risks} 条，其中无风险负例 {no_risk} 场，自检通过")
    print(f"  输出：{text_dir}")
    print(f"  输出：{OUT / 'gold.jsonl'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
