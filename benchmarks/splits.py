"""数据集切分：开发集 / 已暴露测试集 / 未暴露测试集。

为什么要切
----------
现有数据集是在搭建评测体系的过程中反复看过的：我调整过 RAG-014 的答案关键词、
补齐过 4 条会议标注、修正过 PPT 的页数断言。这些数据已经"被看过"，再用它证明
"优化有效"就是自欺欺人。因此把数据切成三块：

* ``dev``           —— 允许反复调 Prompt / 规则的地方；
* ``test_exposed``  —— 原数据集里留出的部分，只用于最终 A/B，且在报告里明确标注"已暴露"；
* ``test_unseen``   —— 全新编写、从未参与任何调参的数据，用于最终验证。

切分原则
--------
* 会议按 **整场会议** 切分（不会出现同一场会议的一半在 dev、一半在 test）；
* RAG 按 **整份文档** 切分（不会出现同一份文档既用于调参又用于测试）；
* Agent 按 **整个任务** 切分，并按任务类型分层。

运行::

    python -m benchmarks.splits
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Iterable

from benchmarks import common

MEET_DIR = common.DATASETS_DIR / "meetings"
RAG_DIR = common.DATASETS_DIR / "rag"
AGENT_DIR = common.DATASETS_DIR / "agent"

# RAG：整份文档分配，保证测试文档从未被调参时看过
RAG_DOC_SPLIT = {
    "DOC-A": "dev",
    "DOC-B": "dev",
    "DOC-C": "test_exposed",
    "DOC-D": "test_exposed",
}


def _stratified_assign(items: list[dict[str, Any]], key: str, ratio: float) -> dict[str, str]:
    """按某字段分层做确定性轮转分配，保证每层在两侧都有样本。

    做法：同层内按 ID 排序后交替取样（第 1 个进 dev、第 2 个进 test、第 3 个进 dev……），
    层内样本数 ≥2 时保证至少 1 个进 test。完全是确定性的，重跑结果不变。
    """
    buckets: dict[str, list[Any]] = {}
    for item in items:
        buckets.setdefault(str(item.get(key) or ""), []).append(item)
    assign: dict[str, str] = {}
    for _bucket_key, group in sorted(buckets.items()):
        def ident(item: dict[str, Any]) -> str:
            return item.get("meeting_id") or item.get("task_id") or ""

        ordered = sorted(group, key=ident)
        if len(ordered) == 1:
            assign[ident(ordered[0])] = "dev"
            continue
        test_count = 0
        for index, item in enumerate(ordered):
            take_test = index % 2 == 1
            if not take_test and index == len(ordered) - 1 and test_count == 0:
                take_test = True  # 兜底：这一层至少要有一个测试样本
            assign[ident(item)] = "test_exposed" if take_test else "dev"
            test_count += 1 if take_test else 0
    return assign


def build_meeting_splits() -> dict[str, Any]:
    gold = common.read_jsonl(MEET_DIR / "gold.jsonl")
    assign = _stratified_assign(gold, "category", ratio=0.36)
    splits: dict[str, list[str]] = {"dev": [], "test_exposed": [], "test_unseen": []}
    for row in gold:
        splits[assign[row["meeting_id"]]].append(row["meeting_id"])
    unseen_path = MEET_DIR / "unseen" / "gold.jsonl"
    if unseen_path.is_file():
        splits["test_unseen"] = [r["meeting_id"] for r in common.read_jsonl(unseen_path)]
    for key in splits:
        splits[key] = sorted(splits[key])
    payload = {
        "rule": "按整场会议切分；按 category 分层轮转；unseen 为独立编写的新会议",
        "splits": splits,
        "sizes": {k: len(v) for k, v in splits.items()},
    }
    common.write_json(MEET_DIR / "splits.json", payload)
    return payload


def build_rag_splits() -> dict[str, Any]:
    questions = common.read_jsonl(RAG_DIR / "questions.jsonl")
    dev: list[str] = []
    exposed: list[str] = []
    for q in questions:
        docs = {ref["document_id"] for ref in q.get("gold_relevant_pages") or []}
        if not docs:  # 负例题：按题干所属主题归属，默认放 dev
            dev.append(q["question_id"])
            continue
        targets = {RAG_DOC_SPLIT.get(d, "dev") for d in docs}
        if targets == {"test_exposed"}:
            exposed.append(q["question_id"])
        else:
            # 跨 dev/test 文档的问题一律放 dev，避免用测试文档调参
            dev.append(q["question_id"])
    unseen_path = RAG_DIR / "unseen" / "questions.jsonl"
    unseen: list[str] = []
    if unseen_path.is_file():
        unseen = [q["question_id"] for q in common.read_jsonl(unseen_path)]
    payload = {
        "rule": "按整份文档切分；跨文档问题归 dev；unseen 为独立编写的新文档与新题",
        "documents": RAG_DOC_SPLIT,
        "splits": {
            "dev": sorted(dev),
            "test_exposed": sorted(exposed),
            "test_unseen": sorted(unseen),
        },
        "sizes": {"dev": len(dev), "test_exposed": len(exposed), "test_unseen": len(unseen)},
    }
    common.write_json(RAG_DIR / "splits.json", payload)
    return payload


def build_agent_splits() -> dict[str, Any]:
    from benchmarks.datasets.agent.tasks import TASKS

    assign = _stratified_assign(TASKS, "type", ratio=0.4)
    splits: dict[str, list[str]] = {"dev": [], "test_exposed": [], "test_unseen": []}
    for task in TASKS:
        splits[assign[task["task_id"]]].append(task["task_id"])
    unseen_path = AGENT_DIR / "unseen_tasks.py"
    if unseen_path.is_file():
        from benchmarks.datasets.agent.unseen_tasks import TASKS as UNSEEN

        splits["test_unseen"] = [t["task_id"] for t in UNSEEN]
    payload = {
        "rule": "按任务切分；按 type 分层轮转；unseen 为独立编写的新任务",
        "splits": {k: sorted(v) for k, v in splits.items()},
        "sizes": {k: len(v) for k, v in splits.items()},
    }
    common.write_json(AGENT_DIR / "splits.json", payload)
    return payload


def main() -> int:
    for builder in (build_meeting_splits, build_rag_splits, build_agent_splits):
        payload = builder()
        print(f"[{builder.__name__}] 规模 {payload['sizes']}")
    print("\n已写入 splits.json（meeting / rag / agent）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
