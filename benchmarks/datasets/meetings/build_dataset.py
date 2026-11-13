"""构建会议风险数据集并做**静态自检**。

运行::

    python -m benchmarks.datasets.meetings.build_dataset

产物::

    benchmarks/datasets/meetings/meetings/MEET-001.txt ... MEET-033.txt
    benchmarks/datasets/meetings/gold.jsonl

自检是硬约束，任何一条不通过都会 `exit 1`：

1. 会议 ID 唯一，总数 ≥ 30；
2. 每条 ``match`` 短语必须原样出现在会议正文（去掉空白标点后）——
   否则判分关键词就是凭空造的；
3. 每条 ``evidence`` 的每一段（用 ``……`` 分隔）都必须能在正文里找到；
4. ``risk_type`` 必须在固定分类法内；``severity`` 必须是 high/medium/low；
5. 标注的 ``owner`` / ``deadline`` 必须在正文中出现；
   标注为空则说明该待办**故意缺负责人/截止时间**，用来检验模型是否编造。
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from benchmarks.datasets.meetings.corpus_a import MEETINGS_A
from benchmarks.datasets.meetings.corpus_b import MEETINGS_B
from benchmarks.datasets.meetings.corpus_c import MEETINGS_C

OUT_DIR = Path(__file__).resolve().parent
TEXT_DIR = OUT_DIR / "meetings"
GOLD_PATH = OUT_DIR / "gold.jsonl"

RISK_TYPES = ("进度", "成本", "质量", "技术", "资源", "需求变更", "合规", "供应商")
SEVERITIES = ("high", "medium", "low")

_SQUASH_RE = re.compile(r"[\s\u3000，。、；：！？,.;:!?\"'“”‘’（）()\-—_~·…]+")


def squash(text: str) -> str:
    return _SQUASH_RE.sub("", text or "")


def all_meetings() -> list[dict]:
    return [*MEETINGS_A, *MEETINGS_B, *MEETINGS_C]


def validate(meetings: list[dict], min_count: int = 30) -> list[str]:
    problems: list[str] = []
    seen: set[str] = set()
    for item in meetings:
        mid = item["meeting_id"]
        if mid in seen:
            problems.append(f"{mid}: 会议 ID 重复")
        seen.add(mid)
        if len(item["text"].strip()) < 30:
            problems.append(f"{mid}: 正文过短")
        if not item.get("category"):
            problems.append(f"{mid}: 缺少 category")
        body = squash(item["text"])
        gold = item["gold"]

        for risk in gold["risks"]:
            rid = f"{mid}/{risk['risk_id']}"
            if risk["risk_type"] not in RISK_TYPES:
                problems.append(f"{rid}: 未知风险类型 {risk['risk_type']}")
            if risk["severity"] not in SEVERITIES:
                problems.append(f"{rid}: 非法等级 {risk['severity']}")
            if not risk.get("match"):
                problems.append(f"{rid}: 没有判分关键词")
            for phrase in risk.get("match") or []:
                if squash(phrase) not in body:
                    problems.append(f"{rid}: 关键词「{phrase}」不在正文中")
            for segment in str(risk.get("evidence") or "").split("……"):
                if segment.strip() and squash(segment) not in body:
                    problems.append(f"{rid}: 依据片段「{segment}」不在正文中")

        for decision in gold["decisions"]:
            did = f"{mid}/{decision['decision_id']}"
            if not decision.get("match"):
                problems.append(f"{did}: 没有判分关键词")
            for phrase in decision.get("match") or []:
                if squash(phrase) not in body:
                    problems.append(f"{did}: 关键词「{phrase}」不在正文中")

        for todo in gold["todos"]:
            tid = f"{mid}/{todo['todo_id']}"
            if not todo.get("match"):
                problems.append(f"{tid}: 没有判分关键词")
            for phrase in todo.get("match") or []:
                if squash(phrase) not in body:
                    problems.append(f"{tid}: 关键词「{phrase}」不在正文中")
            owner = todo.get("owner") or ""
            if owner and squash(owner) not in body:
                problems.append(f"{tid}: 负责人「{owner}」不在正文中")
            deadline = todo.get("deadline") or ""
            if deadline and squash(deadline) not in body:
                problems.append(f"{tid}: 截止时间「{deadline}」不在正文中")

    if len(meetings) < min_count:
        problems.append(f"会议总数 {len(meetings)} 少于 {min_count} 条")
    return problems


def build() -> dict:
    meetings = all_meetings()
    problems = validate(meetings)
    if problems:
        print("数据集自检未通过：")
        for item in problems:
            print(f"  - {item}")
        raise SystemExit(1)

    TEXT_DIR.mkdir(parents=True, exist_ok=True)
    for old in TEXT_DIR.glob("*.txt"):
        old.unlink()
    for item in meetings:
        path = TEXT_DIR / f"{item['meeting_id']}.txt"
        path.write_text(item["text"].strip() + "\n", encoding="utf-8")

    rows = [
        {
            "meeting_id": item["meeting_id"],
            "title": item["title"],
            "category": item["category"],
            "source_name": f"{item['meeting_id']}.txt",
            "char_count": len(item["text"]),
            **item["gold"],
        }
        for item in meetings
    ]
    GOLD_PATH.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
        encoding="utf-8",
    )

    risk_total = sum(len(row["risks"]) for row in rows)
    print(f"会议总数 {len(rows)}，风险标注 {risk_total} 条，自检通过")
    by_cat: dict[str, int] = {}
    for row in rows:
        by_cat[row["category"]] = by_cat.get(row["category"], 0) + 1
    print("  分类分布：" + "、".join(f"{k}={v}" for k, v in sorted(by_cat.items())))
    by_type: dict[str, int] = {}
    for row in rows:
        for risk in row["risks"]:
            by_type[risk["risk_type"]] = by_type.get(risk["risk_type"], 0) + 1
    print("  风险类型分布：" + "、".join(f"{k}={v}" for k, v in sorted(by_type.items())))
    print("  缺失负责人的待办：" + str(sum(1 for row in rows for t in row["todos"] if not t["owner"])))
    print("  缺失截止时间的待办：" + str(sum(1 for row in rows for t in row["todos"] if not t["deadline"])))
    print(f"  输出：{TEXT_DIR}")
    print(f"  输出：{GOLD_PATH}")
    return {"meetings": len(rows), "risks": risk_total}


def main() -> int:
    build()
    return 0


if __name__ == "__main__":
    sys.exit(main())
