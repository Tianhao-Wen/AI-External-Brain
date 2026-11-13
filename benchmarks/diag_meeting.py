"""单场会议诊断：把 LLM 原始输出、接地校验后、确定性过滤后的三层结果都打印出来。

用于回答"这条风险到底是模型没抽到，还是被规则删掉了"。

用法::

    python -m benchmarks.diag_meeting MEET-003 [MEET-018 ...]
"""

from __future__ import annotations

import asyncio
import sys

from benchmarks import common


async def diagnose(meeting_id: str) -> None:
    from aieb.brain import meetings as m

    path = common.DATASETS_DIR / "meetings" / "meetings" / f"{meeting_id}.txt"
    if not path.is_file():
        path = common.DATASETS_DIR / "meetings" / "unseen" / "meetings" / f"{meeting_id}.txt"
    text = path.read_text(encoding="utf-8")

    provider, _model = common.resolve_provider(offline=False)
    raw = await m.analyze_async(provider, text, source_name=path.name)
    print(f"\n===== {meeting_id} LLM 原始输出 =====")
    for risk in raw.risks:
        print(f"  [{risk.level}/{risk.status}/{risk.risk_type}] {risk.title}")
        print(f"      evidence: {risk.evidence[:100]}")

    candidates = []
    raw_squashed = m._squash(text)
    for risk in raw.risks:
        if not risk.title.strip() and not risk.description.strip():
            continue
        candidates.append(
            {
                "title": risk.title.strip() or "未命名风险",
                "level": risk.level,
                "risk_type": (risk.risk_type or "").strip(),
                "status": risk.status,
                "description": risk.description.strip(),
                "evidence": risk.evidence if m._grounded(risk.evidence, raw_squashed) else "",
                "suggestion": risk.suggestion.strip(),
            }
        )
    kept, dropped = m.risk_rules.filter_risks(candidates, text)
    print(f"----- 确定性过滤：保留 {len(kept)} / 丢弃 {len(dropped)} -----")
    for item in dropped:
        print(f"  DROP {item['title']} <- {item['dropped_reason']}")
    cleaned = m.sanitize(raw, text)
    print(f"----- 最终入库 {len(cleaned.risks)} 条 -----")
    for risk in cleaned.risks:
        print(f"  [{risk.level}] {risk.title}")


async def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    common.neutralise_env()
    for meeting_id in argv:
        await diagnose(meeting_id)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
