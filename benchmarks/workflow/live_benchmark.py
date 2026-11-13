"""真实 LLM 工作流 Benchmark（DeepSeek，25 场会议，含人工审批与 Fake SMTP）。

与 `benchmarks/workflow/runner.py`（fixture 对照实验）的区别：
* 本文件**真的调用 DeepSeek**，用来衡量"模型在真实会议文本上的抽取质量 + 工作流编排正确性"；
* 那个文件用确定性 fake，用来对比"原生流程 vs LangGraph 编排"，两者不可混用、不可互相替代。

执行链路（每场会议真的走一遍）：
    会议输入 → LLM 分析 → 风险校验 → 待办抽取 → 业务落库 → 生成邮件草稿
    → interrupt 等待审批 → 控制器发出 approve/edit/reject → resume
    → Fake SMTP 投递（或取消）→ 审计

预算保护
--------
* `--max-calls`：真实 LLM 调用上限（默认 60；25 场 × 2 次调用 = 50 次）
* 每场执行前预估 2 次调用，超出预算立即停止，并如实报告已完成样本数
* 单价按 DeepSeek 官方公示价（USD / 1M tokens）换算，输入按 **cache miss 峰值价** 取上界，
  成本只作为量级参考；所有 token 数据都来自 provider 回传的真实 usage

用法
----
    python -m benchmarks.workflow.live_benchmark                # 全量 25 场
    python -m benchmarks.workflow.live_benchmark --limit 3       # Pilot
    python -m benchmarks.workflow.live_benchmark --cases A01,B01
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import time
from pathlib import Path
from typing import Any

from aieb.brain.store import BrainStore
from aieb.config import ProviderConfig
from aieb.workflow.deps import build_real_deps
from aieb.workflow.persistence import WorkflowStore
from aieb.workflow.runner import WorkflowRunner
from aieb.workflow.state import ResumeCommand
from benchmarks import common
from benchmarks.workflow import gold_scoring
from tests.fake_smtp import FakeSMTPServer

ROOT = Path(__file__).resolve().parents[2]
DATASET = ROOT / "benchmarks" / "datasets" / "live_workflow" / "meetings.json"
OUT_DIR = common.RESULTS_DIR / "workflow"
TO_ADDR = "audit-demo@example.com"

#: 真正会调用模型的节点（用于统计 LLM 调用次数与 token）
LLM_NODES = {"analyze_meeting", "generate_email_draft"}

#: DeepSeek 官方公示价（USD / 1M tokens）。取**峰值 + cache miss** 作为成本上界。
PRICE_INPUT_PER_M = 0.30
PRICE_OUTPUT_PER_M = 1.20


class _Service:
    """把 `build_real_deps` 需要的少量接口拼出来（不启动整个 Web 服务）。"""

    def __init__(self, provider: ProviderConfig, store: BrainStore) -> None:
        self.provider = provider
        self.store = store


def load_cases() -> list[dict[str, Any]]:
    return json.loads(DATASET.read_text(encoding="utf-8"))["cases"]


def live_provider() -> ProviderConfig:
    key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not key:
        raise SystemExit("缺少 DEEPSEEK_API_KEY，无法执行真实 LLM Benchmark")
    return ProviderConfig(
        name="deepseek-live",
        protocol="openai-compat",
        base_url=os.environ.get("AIEB_LIVE_BASE_URL", "https://api.deepseek.com"),
        model=os.environ.get("AIEB_LIVE_MODEL", "deepseek-chat"),
        api_key=key,
    )


def smtp_env(server: FakeSMTPServer) -> dict[str, str]:
    """让被测代码走"真实 SMTP 协议"，但连的是进程内假服务器。"""
    env = dict(server.env())
    env.pop("AIEB_EMAIL_DRY_RUN", None)
    return env


def _with_env(env: dict[str, str]):
    """临时注入 SMTP 环境变量，退出时还原（避免污染其它测试）。"""

    class _Ctx:
        def __enter__(self):
            self.saved = {k: os.environ.get(k) for k in env}
            self.saved.pop("AIEB_EMAIL_DRY_RUN", None)
            os.environ.pop("AIEB_EMAIL_DRY_RUN", None)
            os.environ.update(env)
            return self

        def __exit__(self, *exc):  # noqa: ANN002
            for key, value in self.saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            return False

    return _Ctx()


async def run_case(
    case: dict[str, Any],
    *,
    provider: ProviderConfig,
    root: Path,
    smtp: FakeSMTPServer,
) -> dict[str, Any]:
    workspace = root / case["id"]
    shutil.rmtree(workspace, ignore_errors=True)
    workspace.mkdir(parents=True, exist_ok=True)
    store = BrainStore(workspace)
    wf = WorkflowStore(workspace / ".aieb" / "workflow" / "workflow.sqlite3")
    service = _Service(provider, store)
    deps = build_real_deps(service=service, wf_store=wf, workspace=workspace)
    runner = WorkflowRunner(deps=deps, workspace=workspace)
    mails_before = len(smtp.messages)

    result: dict[str, Any] = {
        "id": case["id"],
        "category": case["category"],
        "length": case["length"],
        "controller_decision": case["controller_decision"],
        "text_chars": len(case["text"]),
    }
    try:
        t0 = time.perf_counter()
        snapshot = await runner.start(
            name=f"{case['id']}.txt", text=case["text"], to_addr=TO_ADDR
        )
        t_wait = time.perf_counter()
        status = snapshot.execution_status
        error = ""
        pending = await runner.pending_approval(snapshot.workflow_id)
        if pending is None and status == "waiting_approval":
            error = "状态为 waiting_approval 但拿不到审批载荷"
        if pending is not None:
            decision = case["controller_decision"]
            command = ResumeCommand(
                decision=decision,  # type: ignore[arg-type]
                version=pending.version,
                draft_hash=pending.draft_hash,
                note="live benchmark 控制器",
                decided_by="benchmark-controller",
            )
            edit: dict[str, Any] = {}
            if decision == "edit":
                edit = {
                    "edit_subject": f"{pending.subject}（已按审批意见修订）",
                    "edit_body": f"{pending.body}\n\n[审批意见] 请各责任人在会上确认后回复。",
                }
                snapshot = await runner.resume(snapshot.workflow_id, command, edit=edit)
                again = await runner.pending_approval(snapshot.workflow_id)
                if again is not None:
                    snapshot = await runner.resume(
                        snapshot.workflow_id,
                        ResumeCommand(
                            decision="approve",
                            version=again.version,
                            draft_hash=again.draft_hash,
                            note="修改后再次确认",
                            decided_by="benchmark-controller",
                        ),
                    )
            else:
                snapshot = await runner.resume(snapshot.workflow_id, command)
            status = snapshot.execution_status
        t_done = time.perf_counter()

        history = await runner.history_async(snapshot.workflow_id)
        events = history["events"]
        node_ms: dict[str, float] = {}
        tokens_in = tokens_out = 0
        llm_calls = 0
        checkpoint_ids = set()
        for event in events:
            node = event["node"]
            node_ms[node] = round(node_ms.get(node, 0.0) + float(event["duration_ms"]), 2)
            if event["event"] == "node" and node in LLM_NODES and event["status"] == "ok":
                tokens_in += int(event["input_tokens"])
                tokens_out += int(event["output_tokens"])
                llm_calls += 1
            if event["event"] == "retry":
                llm_calls += 1
            if event["checkpoint_id"]:
                checkpoint_ids.add(event["checkpoint_id"])

        emails = store.list_emails(limit=10)
        effects = history["effects"]
        sends = len(smtp.messages) - mails_before
        duplicate = max(0, sends - (case["expected_side_effects"]["sends"]))

        predicted = {
            "risks": list(snapshot.risks or []),
            "todos": list(snapshot.todos or []),
            "decisions": _decisions_from_events(events) or _decisions_from_store(store),
            "meetings": len(store.list_meetings()),
            "emails": len(emails),
        }
        score = gold_scoring.score_case(
            case, predicted, status=status, sent_count=sends, duplicate_side_effects=duplicate
        )
        score_loose = gold_scoring.score_case(
            case,
            predicted,
            status=status,
            sent_count=sends,
            duplicate_side_effects=duplicate,
            loose=True,
        )
        result.update(
            {
                "status": status,
                "error": error or next(
                    (e["detail"].get("error", "") for e in events if e["status"] == "error"), ""
                ),
                "email_id": emails[0]["id"] if emails else "",
                "email_version": int(emails[0].get("version") or 0) if emails else 0,
                "email_status": emails[0].get("status", "") if emails else "",
                "sends": sends,
                "llm_calls": llm_calls,
                "input_tokens": tokens_in,
                "output_tokens": tokens_out,
                "total_ms": round((t_done - t0) * 1000, 1),
                "to_first_interrupt_ms": round((t_wait - t0) * 1000, 1),
                "approval_roundtrip_ms": round((t_done - t_wait) * 1000, 1),
                "node_ms": node_ms,
                "checkpoints": len(checkpoint_ids),
                "effects": {e["action"]: e["status"] for e in effects},
                "score": score,
                "score_loose": score_loose,
                "predicted": {
                    "risks": [r.get("title") for r in predicted["risks"]],
                    "todos": [t.get("content") for t in predicted["todos"]],
                    "decisions": list(predicted["decisions"]),
                },
            }
        )
    except Exception as exc:  # noqa: BLE001 - 单个用例失败不能中断整体
        result.update(
            {
                "status": "error",
                "error": f"{type(exc).__name__}: {exc}"[:400],
                "score": gold_scoring.score_case(
                    case,
                    {"risks": [], "todos": [], "decisions": [], "meetings": 0, "emails": 0},
                    status="error",
                    sent_count=len(smtp.messages) - mails_before,
                    duplicate_side_effects=0,
                ),
            }
        )
    finally:
        runner.close()
        store.close()
        wf.close()
    return result


def _decisions_from_store(store: BrainStore) -> list[str]:
    meetings = store.list_meetings(limit=1)
    if not meetings:
        return []
    return list(meetings[0].get("decisions") or [])


def _decisions_from_events(events: list[dict[str, Any]]) -> list[str]:
    """决策来自 analyze_meeting 的 detail（如果有），否则回落到业务库。"""
    for event in events:
        detail = event.get("detail") or {}
        if isinstance(detail, dict) and detail.get("decisions"):
            return list(detail["decisions"])
    return []


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    scored = [r for r in rows if r.get("score")]
    aggregate = gold_scoring.aggregate([r["score"] for r in scored]) if scored else {}
    loose_rows = [r["score_loose"] for r in rows if r.get("score_loose")]
    sensitivity = gold_scoring.aggregate(loose_rows) if loose_rows else {}
    latencies = sorted(r["total_ms"] for r in rows if "total_ms" in r)
    node_totals: dict[str, float] = {}
    for row in rows:
        for node, ms in (row.get("node_ms") or {}).items():
            node_totals[node] = round(node_totals.get(node, 0.0) + float(ms), 2)
    tokens_in = sum(r.get("input_tokens", 0) for r in rows)
    tokens_out = sum(r.get("output_tokens", 0) for r in rows)
    cost = tokens_in / 1_000_000 * PRICE_INPUT_PER_M + tokens_out / 1_000_000 * PRICE_OUTPUT_PER_M
    return {
        "cases_executed": len(rows),
        "cases_scored": len(scored),
        "metrics": aggregate,
        "sensitivity_loose": sensitivity,
        "llm_calls": sum(r.get("llm_calls", 0) for r in rows),
        "input_tokens": tokens_in,
        "output_tokens": tokens_out,
        "estimated_cost_usd": round(cost, 4),
        "price_assumption": {
            "input_per_1m_usd": PRICE_INPUT_PER_M,
            "output_per_1m_usd": PRICE_OUTPUT_PER_M,
            "note": "DeepSeek 公示价，取峰值+cache miss 上界；off-peak 为半价",
        },
        "latency_p50_ms": _pct(latencies, 50),
        "latency_p95_ms": _pct(latencies, 95),
        "node_total_ms": node_totals,
        "errors": [{"id": r["id"], "error": r.get("error", "")} for r in rows if r.get("error")],
    }


def _pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    data = sorted(values)
    if len(data) == 1:
        return round(data[0], 1)
    rank = (len(data) - 1) * (p / 100.0)
    low = int(rank)
    high = min(low + 1, len(data) - 1)
    weight = rank - low
    return round(data[low] * (1 - weight) + data[high] * weight, 1)


async def main_async(args: argparse.Namespace) -> int:
    cases = load_cases()
    if args.cases:
        wanted = {c.strip() for c in args.cases.split(",") if c.strip()}
        cases = [c for c in cases if c["id"] in wanted]
    if args.limit:
        cases = cases[: args.limit]

    provider = live_provider()
    root = common.WORK_ROOT / "live_bench"
    if not args.keep:
        shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)

    smtp = FakeSMTPServer()
    smtp.start()
    rows: list[dict[str, Any]] = []
    budget_calls = int(args.max_calls)
    calls_used = 0
    stopped_reason = ""
    try:
        with _with_env(smtp_env(smtp)):
            for case in cases:
                if calls_used + 2 > budget_calls:
                    stopped_reason = f"预算用尽：已用 {calls_used} 次调用，上限 {budget_calls}"
                    print(f"[stop] {stopped_reason}")
                    break
                print(f"[run ] {case['id']} ({case['category']}/{case['length']}) ...", flush=True)
                row = await run_case(
                    case, provider=provider, root=root, smtp=smtp
                )
                calls_used += int(row.get("llm_calls", 0))
                rows.append(row)
                score = row.get("score") or {}
                print(
                    f"[done] {case['id']} status={row.get('status')} "
                    f"task_ok={score.get('task_ok')} "
                    f"risk_f1={score.get('risks', {}).get('f1')} "
                    f"tokens={row.get('input_tokens')}/{row.get('output_tokens')} "
                    f"{row.get('total_ms')}ms",
                    flush=True,
                )
    finally:
        smtp.stop()

    summary = summarize(rows)
    summary["budget"] = {
        "max_calls": budget_calls,
        "calls_used": calls_used,
        "stopped_reason": stopped_reason,
        "dataset_cases": len(load_cases()),
        "requested_cases": len(cases),
    }
    payload = {
        "mode": "live_deepseek",
        "model": provider.model,
        "base_url": provider.base_url,
        "dataset": json.loads(
            (ROOT / "benchmarks" / "datasets" / "live_workflow" / "manifest.json").read_text(
                encoding="utf-8"
            )
        )
        if (ROOT / "benchmarks" / "datasets" / "live_workflow" / "manifest.json").exists()
        else {},
        "summary": summary,
        "rows": rows,
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / (args.out or "live_benchmark.json")
    common.write_json(out, payload)

    m = summary["metrics"]
    print()
    print("=" * 78)
    print(f"真实 LLM Benchmark：执行 {summary['cases_executed']} 场"
          f"（模型 {provider.model}）")
    print(f"Workflow Completion Rate : {m.get('workflow_completion_rate')}")
    print(f"Task Success Rate        : {m.get('task_success_rate')}")
    print(f"Risk  P/R/F1 (micro)     : {m.get('risk_micro')}")
    print(f"Todo  P/R/F1 (micro)     : {m.get('todo_micro')}")
    print(f"Decision Accuracy        : {m.get('decision_accuracy')}")
    print(f"False Positive Rate      : {m.get('false_positive_rate')}")
    print(f"LLM 调用 / tokens        : {summary['llm_calls']} / "
          f"{summary['input_tokens']} in + {summary['output_tokens']} out")
    print(f"估算成本 (USD, 上界)      : {summary['estimated_cost_usd']}")
    print(f"端到端 P50 / P95 (ms)    : {summary['latency_p50_ms']} / {summary['latency_p95_ms']}")
    print(f"失败用例                  : {m.get('failed_cases')}")
    print(f"结果 → {out}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="真实 LLM 工作流 Benchmark")
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 个用例")
    parser.add_argument("--cases", default="", help="逗号分隔的用例 ID，如 A01,B01")
    parser.add_argument("--max-calls", type=int, default=60, help="真实 LLM 调用上限")
    parser.add_argument("--keep", action="store_true", help="保留上一次的工作目录")
    parser.add_argument("--out", default="", help="输出文件名（写在 benchmarks/results/workflow/ 下）")
    return asyncio.run(main_async(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
