"""消融实验：区分"Prompt 改进"与"确定性校验"各自的贡献。

做法：把 ``risk_rules.filter_risks`` 替换成恒等函数（只保留模型输出，不做规则过滤），
其余链路完全不变，在同一个切分上再跑一遍。这样两个数字的差就是规则层的净贡献。

用法::

    python -m benchmarks.ablation_meeting --split dev --tag prompt_only
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from benchmarks import common


async def main_async(args) -> int:
    from benchmarks.runners import meeting_runner

    from aieb.brain import meetings as meetings_mod, risk_rules

    original = risk_rules.filter_risks
    if args.mode == "prompt_only":
        risk_rules.filter_risks = lambda risks, raw: (risks, [])  # type: ignore[assignment]
        meetings_mod.risk_rules = risk_rules  # 保证模块引用一致
    try:
        ctx = common.prepare_run(suite="meeting", mode="live", max_calls=args.max_calls,
                                 run_id=args.run_id)
        with common.patch_llm_counter(ctx.budget):
            result = await meeting_runner.run(ctx, split=args.split)
        meeting_runner.write_outputs(result, ctx)
        agg = result["aggregates"]["overall"]["risk"]
        print(f"\n[{args.mode}] split={args.split} "
              f"P={agg['precision']:.3f} R={agg['recall']:.3f} F1={agg['f1']:.3f} "
              f"noRiskFP={agg['no_risk_false_positive_rate']:.3f} "
              f"highMiss={agg['high_risk_missed']}/{agg['high_risk_total']}")
        return 0
    finally:
        risk_rules.filter_risks = original  # type: ignore[assignment]
        meetings_mod.risk_rules = risk_rules


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--split", default="dev")
    parser.add_argument("--mode", default="prompt_only", choices=("prompt_only", "full"))
    parser.add_argument("--output", default="")
    parser.add_argument("--max-calls", type=int, default=40)
    parser.add_argument("--run-id", default="")
    args = parser.parse_args()

    if args.output:
        common.RESULTS_DIR = common.PROJECT_ROOT / args.output
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())
