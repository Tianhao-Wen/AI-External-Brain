"""重复数据体检 / 清理工具（默认只体检，不改数据）。

背景：同一份会议记录被重复上传时，会生成多场会议 + 多套长期记忆，页面看起来
就是"同一条决策出现两遍"。新版本已在写入侧做了复用（同一份原文只分析一次），
但历史数据里可能已经存在重复，用这个脚本体检。

用法::

    python scripts/dedupe_memories.py            # 只体检，打印重复情况
    python scripts/dedupe_memories.py --apply    # 真的删除重复（保留最早的一条）

删除范围仅限 ``memory_facts`` 表的**完全重复行**（category + content 相同）
与重复的会议记录；知识库文档、待办、邮件一律不动。
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from aieb.brain.store import BrainStore  # noqa: E402


def _normalize(text: str) -> str:
    return " ".join((text or "").split())


def find_duplicate_facts(store: BrainStore) -> dict[tuple[str, str], list[dict]]:
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for fact in store.list_memory_facts():
        groups[(fact.get("category") or "", _normalize(fact.get("content") or ""))].append(fact)
    return {k: v for k, v in groups.items() if len(v) > 1}


def find_duplicate_meetings(store: BrainStore) -> dict[str, list[dict]]:
    by_id = {m["id"]: m for m in store.list_meetings(limit=500)}
    groups: dict[str, list[dict]] = defaultdict(list)
    for row in store.list_meeting_texts():
        meta = by_id.get(row["id"])
        if meta is None:
            continue
        groups[_normalize(row["raw_text"])].append(meta)
    return {k: v for k, v in groups.items() if len(v) > 1 and k}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--work-dir", default=str(ROOT))
    parser.add_argument("--apply", action="store_true", help="真的删除重复项（默认只体检）")
    args = parser.parse_args()

    store = BrainStore(Path(args.work_dir))
    try:
        dup_facts = find_duplicate_facts(store)
        dup_meetings = find_duplicate_meetings(store)

        facts_total = sum(len(v) for v in dup_facts.values())
        print(f"记忆事实：重复组 {len(dup_facts)} 个，涉及 {facts_total} 条")
        for (category, content), rows in list(dup_facts.items())[:10]:
            print(f"  [{category}] {content[:60]}… × {len(rows)}")
            for row in rows:
                print(f"      {row['id']}  {row.get('source_label','')}  {row.get('created_at','')}")

        meetings_total = sum(len(v) for v in dup_meetings.values())
        print(f"\n会议记录：重复组 {len(dup_meetings)} 个，涉及 {meetings_total} 场")
        for _, rows in list(dup_meetings.items())[:10]:
            for row in rows:
                print(f"  {row['id']}  {row.get('title','')}  {row.get('created_at','')}")

        if not args.apply:
            print("\n以上为体检结果。确认无误后加 --apply 才会真正删除（保留最早一条）。")
            return 0

        removed_facts = 0
        for _, rows in dup_facts.items():
            for row in sorted(rows, key=lambda r: r.get("created_at") or "")[1:]:
                if store.delete_memory_fact(row["id"]):
                    removed_facts += 1
        removed_meetings = 0
        for _, rows in dup_meetings.items():
            for row in sorted(rows, key=lambda r: r.get("created_at") or "")[1:]:
                if store.delete_meeting(row["id"]):
                    removed_meetings += 1
        print(f"\n已删除重复记忆 {removed_facts} 条、重复会议 {removed_meetings} 场。")
        return 0
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
