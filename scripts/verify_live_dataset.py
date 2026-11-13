"""校验真实 LLM 数据集的金标准是否"可核查"，并生成冻结清单。

校验内容（全部是确定性检查，不调用模型）：
1. 用例数量、分类分布、ID 唯一；
2. 每条 `gold_risks[].evidence` **必须能在原文中原样找到**（允许空白差异）；
3. 每条 `gold_todos[]` 的 owner / due_date 若非空，必须能在原文中找到；
4. `expected_side_effects` 与 `controller_decision` 自洽（reject → sends=0）；
5. 文本长度分档（short/medium/long）与实际字符数一致。

输出 `manifest.json`：数据文件与评分脚本的 sha256，用于"正式测试前冻结数据集与评分脚本"。

    python scripts/verify_live_dataset.py
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / "benchmarks" / "datasets" / "live_workflow" / "meetings.json"
MANIFEST = ROOT / "benchmarks" / "datasets" / "live_workflow" / "manifest.json"
SCORING = ROOT / "benchmarks" / "workflow" / "gold_scoring.py"

_SQUASH = re.compile(r"[\s\u3000，。、；：！？,.;:!?\"'“”‘’（）()\-—_~·…]+")

#: 字符数分档（不含空白）。实测分布：short 109–142 / medium 159–316 / long 770–1260
LENGTH_BOUNDS = {"short": (0, 150), "medium": (150, 400), "long": (400, 10**6)}


def squash(text: str) -> str:
    return _SQUASH.sub("", text or "")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    payload = json.loads(DATASET.read_text(encoding="utf-8"))
    cases = payload["cases"]
    problems: list[str] = []

    ids = [c["id"] for c in cases]
    if len(ids) != len(set(ids)):
        problems.append("存在重复用例 ID")
    if len(cases) != 25:
        problems.append(f"用例数为 {len(cases)}，期望 25")

    by_category: dict[str, int] = {}
    for case in cases:
        by_category[case["category"]] = by_category.get(case["category"], 0) + 1
        body = squash(case["text"])
        lo, hi = LENGTH_BOUNDS[case["length"]]
        if not (lo <= len(body) < hi):
            problems.append(f"{case['id']} 长度分档不符：{case['length']} 实测 {len(body)} 字")
        for risk in case.get("gold_risks") or []:
            evidence = squash(risk.get("evidence") or "")
            if not evidence:
                problems.append(f"{case['id']} 风险「{risk.get('title')}」缺少 evidence")
            elif evidence not in body:
                problems.append(f"{case['id']} 风险「{risk.get('title')}」evidence 未在原文出现")
        for todo in case.get("gold_todos") or []:
            for field in ("owner", "due"):
                value = squash(todo.get(field) or "")
                if value and value not in body:
                    problems.append(
                        f"{case['id']} 待办「{todo.get('content')}」的 {field}={value} 未在原文出现"
                    )
        effects = case["expected_side_effects"]
        if case["controller_decision"] == "reject" and effects["sends"] != 0:
            problems.append(f"{case['id']} 决策为 reject 但期望发送次数不为 0")
        if case["controller_decision"] in {"approve", "edit"} and effects["sends"] != 1:
            problems.append(f"{case['id']} 决策为 {case['controller_decision']} 但期望发送次数不为 1")

    print(f"用例数：{len(cases)}")
    print("分类分布：" + json.dumps(by_category, ensure_ascii=False))
    total_risks = sum(len(c.get("gold_risks") or []) for c in cases)
    total_todos = sum(len(c.get("gold_todos") or []) for c in cases)
    total_chars = sum(len(squash(c["text"])) for c in cases)
    print(f"金标准风险 {total_risks} 条 / 待办 {total_todos} 条 / 原文合计 {total_chars} 字")

    if problems:
        print("\n校验失败：")
        for item in problems:
            print(" -", item)
        return 1

    manifest = {
        "dataset": payload["dataset"],
        "version": payload["version"],
        "cases": len(cases),
        "by_category": by_category,
        "gold_risks": total_risks,
        "gold_todos": total_todos,
        "text_chars": total_chars,
        "sha256_dataset": sha256(DATASET),
        "sha256_scoring": sha256(SCORING) if SCORING.exists() else "",
        "frozen_on": payload["frozen_on"],
    }
    MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n校验通过（证据可核查、长度分档一致、副作用自洽）")
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
