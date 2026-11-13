"""Benchmark 统一入口。

用法示例::

    python -m benchmarks.run --suite rag --mode fixture
    python -m benchmarks.run --suite rag --mode live --limit 5 --max-calls 10
    python -m benchmarks.run --suite all --mode live --max-calls 200
    python -m benchmarks.run --suite agent --only A1,B2 --repeat 3

设计约束（对应任务书 Part 2 / Part 4）：

* 每次运行都在 ``benchmarks/.work/<run_id>/`` 里建独立 workspace，
  各自拥有独立的 SQLite / Session / Memory / 上传 / Artifact 目录；
* 运行开始与结束各取一次正式 ``.aieb/`` 目录指纹，若发生变化直接报警；
* 所有 LLM 调用都经过 ``common.patch_llm_counter``，受 ``--max-calls`` 硬约束；
* 失败样本一律进入 CSV / JSON，绝不从分母里剔除。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import traceback
from pathlib import Path
from typing import Any

from benchmarks import common

SUITES = ("rag", "meeting", "agent")

# suite -> (模块名, 模块内可 await 的 run 函数名)
_RUNNERS = {
    "rag": ("benchmarks.runners.rag_runner", "run"),
    "meeting": ("benchmarks.runners.meeting_runner", "run"),
    "agent": ("benchmarks.runners.agent_runner", "run"),
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m benchmarks.run",
        description="WenDream / AI-External-Brain 可复现 Benchmark 评测入口",
    )
    parser.add_argument(
        "--suite",
        default="all",
        choices=(*SUITES, "all"),
        help="要跑的套件（默认 all）",
    )
    parser.add_argument(
        "--mode",
        default="fixture",
        choices=("fixture", "live"),
        help="fixture = 不调用模型（检索/规则层）；live = 真实模型全链路",
    )
    parser.add_argument(
        "--max-calls",
        type=int,
        default=0,
        dest="max_calls",
        help="LLM 调用的硬上限，0 表示不限（正式跑请务必设置）",
    )
    parser.add_argument(
        "--output",
        default="",
        help="结果输出目录（默认 benchmarks/results）",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="每个套件只跑前 N 条（Pilot 用）",
    )
    parser.add_argument(
        "--only",
        default="",
        help="只跑指定 id，逗号分隔（RAG-001,MEET-004,TASK-A1）",
    )
    parser.add_argument(
        "--repeat",
        type=int,
        default=1,
        help="重复次数（用于稳定性统计，只对 agent 套件生效）",
    )
    parser.add_argument(
        "--split",
        default="all",
        choices=("all", "dev", "test_exposed", "test_unseen"),
        help="数据集切分：dev=调参用；test_exposed=已暴露测试；test_unseen=从未参与调参的未暴露测试",
    )
    parser.add_argument(
        "--run-id",
        default="",
        dest="run_id",
        help="自定义运行 ID（默认时间戳）",
    )
    parser.add_argument(
        "--top-k",
        type=int,
        default=5,
        dest="top_k",
        help="RAG/检索类套件的 top_k",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="只输出最终汇总",
    )
    return parser


def _split_only(raw: str) -> list[str]:
    return [item.strip() for item in raw.split(",") if item.strip()]


def _print_banner(ctx, suites: list[str], args) -> None:
    print("=" * 78)
    print("Benchmark 运行")
    print("=" * 78)
    print(f"  运行 ID   : {ctx.run_id}")
    print(f"  套件      : {', '.join(suites) if suites else '（空）'}")
    print(f"  模式      : {ctx.mode}")
    print(f"  模型      : {ctx.model.model}  (provider={ctx.model.name})")
    print(f"  端点主机  : {ctx.model.endpoint_host}")
    print(f"  Git       : {ctx.git.get('commit_short') or 'unknown'}"
          f"{' (dirty)' if ctx.git.get('dirty') else ''}")
    print(f"  调用上限  : {ctx.budget.max_calls or '不限'}")
    print(f"  工作目录  : {ctx.run_dir}")
    print(f"  .aieb/    : {ctx.demo_guard['files']} 个文件，指纹 {ctx.demo_guard['digest']}")
    if args.limit:
        print(f"  限制条数  : 前 {args.limit} 条")
    if args.only:
        print(f"  指定 ID   : {args.only}")
    print("=" * 78)
    print()


def _print_suite_summary(name: str, result: dict[str, Any]) -> None:
    agg = result.get("aggregates", {})
    print()
    print("-" * 78)
    print(f"[{name}] 汇总")
    print("-" * 78)
    print(json.dumps(agg, ensure_ascii=False, indent=2, default=str))


async def run_suite(
    name: str,
    ctx,
    args,
) -> dict[str, Any]:
    module_name, func_name = _RUNNERS[name]
    try:
        module = __import__(module_name, fromlist=[func_name])
    except ImportError as exc:  # 套件尚未实现 / 依赖缺失
        return {
            "status": "unavailable",
            "reason": f"无法导入 {module_name}: {exc}",
            "aggregates": {},
            "cases": [],
        }

    runner = getattr(module, func_name)
    kwargs: dict[str, Any] = {}
    if name == "rag":
        kwargs["top_k"] = args.top_k
    if name in ("rag", "meeting"):
        kwargs["limit"] = args.limit
        kwargs["only"] = _split_only(args.only) or None
        kwargs["split"] = args.split
    if name == "agent":
        kwargs["repeat"] = max(1, args.repeat)
        kwargs["only"] = _split_only(args.only) or None
        kwargs["limit"] = args.limit
        kwargs["split"] = args.split

    started = time.perf_counter()
    try:
        result = await runner(ctx, **kwargs)
    except common.BudgetExceeded as exc:
        print(f"  ! 预算耗尽：{exc}")
        return {
            "status": "budget_exceeded",
            "reason": str(exc),
            "aggregates": {},
            "cases": [],
        }
    except Exception as exc:  # noqa: BLE001 - 整体失败也要记录
        print(f"  ! 套件 {name} 运行失败：{type(exc).__name__}: {exc}")
        traceback.print_exc()
        return {
            "status": "error",
            "reason": f"{type(exc).__name__}: {exc}",
            "aggregates": {},
            "cases": [],
        }
    result["suite"] = name
    result["status"] = "ok"
    result["wall_seconds"] = round(time.perf_counter() - started, 2)
    _print_suite_summary(name, result)
    return result


async def main_async(args) -> int:
    if args.output:
        common.RESULTS_DIR = Path(args.output).resolve()
    common.RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    suites = list(SUITES) if args.suite == "all" else [args.suite]
    ctx = common.prepare_run(
        suite=args.suite, mode=args.mode, max_calls=args.max_calls, run_id=args.run_id
    )
    _print_banner(ctx, suites, args)

    outputs: dict[str, Any] = {}
    exit_code = 0
    with common.patch_llm_counter(ctx.budget):
        for name in suites:
            print(f">>> 开始套件：{name}")
            result = await run_suite(name, ctx, args)
            outputs[name] = result
            if result["status"] == "unavailable":
                print(f"    (跳过) {result['reason']}")
            elif result["status"] != "ok":
                exit_code = 1

    # 收尾：预算 / 隔离校验 / 落盘
    print()
    print("=" * 78)
    print("收尾校验")
    print("=" * 78)
    print(f"  LLM 调用：{ctx.budget.used} / {ctx.budget.max_calls or '不限'}"
          f"（失败 {ctx.budget.failures}）")

    unchanged = common.demo_env_unchanged(ctx.demo_guard)
    print(f"  正式 .aieb/ 未被改动：{'是' if unchanged else '否 —— 请立即排查！'}")
    if not unchanged:
        exit_code = 2

    manifest_path = common.RESULTS_DIR / "run_manifest.json"
    # 运行清单按套件累积：一次失败的 fixture 冒烟测试不应该把正式 live 运行的记录冲掉。
    previous: dict[str, Any] = {}
    if manifest_path.is_file():
        try:
            previous = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            previous = {}
    suites_manifest: dict[str, Any] = dict(previous.get("suites") or {})
    for name, res in outputs.items():
        if res.get("status") != "ok":
            continue
        suites_manifest[name] = {
            "run_id": ctx.run_id,
            "mode": ctx.mode,
            "status": res.get("status"),
            "wall_seconds": res.get("wall_seconds"),
            "case_count": len(res.get("cases") or []),
            "llm_calls": ctx.budget.used,
            "model": ctx.model.model,
            "git_commit": ctx.git.get("commit_short"),
            "created_at": ctx.created_at,
        }

    manifest = {
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "last_run": {
            "run_id": ctx.run_id,
            "suite": ctx.suite,
            "mode": ctx.mode,
            "meta": ctx.meta(),
            "demo_env_unchanged": unchanged,
            "statuses": {
                name: res.get("status") for name, res in outputs.items()
            },
        },
        "suites": suites_manifest,
    }
    common.write_json(manifest_path, manifest)
    print(f"  运行清单：{manifest_path}")

    for name, res in outputs.items():
        if res.get("status") != "ok":
            continue
        module_name, _ = _RUNNERS[name]
        module = __import__(module_name, fromlist=["write_outputs"])
        writer = getattr(module, "write_outputs", None)
        if writer is None:
            continue
        try:
            paths = writer(res, ctx)
        except Exception as exc:  # noqa: BLE001
            print(f"  ! 写入 {name} 结果失败：{type(exc).__name__}: {exc}")
            exit_code = 1
            continue
        for kind, path in paths.items():
            print(f"  {name}/{kind}: {path}")

    print("=" * 78)
    return exit_code


def rebuild_manifest() -> int:
    """从已落盘的结果文件重建运行清单（只读取结果里的真实 meta，不编造任何数字）。"""
    suites: dict[str, Any] = {}
    for name in SUITES:
        path = common.RESULTS_DIR / f"{name}_results.json"
        if not path.is_file():
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        meta = payload.get("meta") or {}
        suites[name] = {
            "run_id": meta.get("run_id"),
            "mode": meta.get("mode"),
            "status": "ok",
            "case_count": len(payload.get("cases") or []),
            "llm_calls": (meta.get("budget") or {}).get("used"),
            "model": (meta.get("model") or {}).get("model"),
            "git_commit": (meta.get("git") or {}).get("commit_short"),
            "created_at": meta.get("created_at"),
            "source": path.name,
        }
    manifest = {
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "rebuilt_from": "benchmarks/results/*_results.json",
        "suites": suites,
    }
    path = common.write_json(common.RESULTS_DIR / "run_manifest.json", manifest)
    print(f"已从结果文件重建运行清单：{path}")
    for name, info in suites.items():
        print(f"  {name}: run_id={info['run_id']} mode={info['mode']} "
              f"cases={info['case_count']} llm={info['llm_calls']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    parser.add_argument("--rebuild-manifest", action="store_true",
                        help="只从已有结果文件重建 run_manifest.json")
    args = parser.parse_args(argv)
    if args.rebuild_manifest:
        return rebuild_manifest()
    try:
        return asyncio.run(main_async(args))
    except KeyboardInterrupt:
        print("\n已中断。")
        return 130


if __name__ == "__main__":
    sys.exit(main())
