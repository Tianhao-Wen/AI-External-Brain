"""原生业务流程 vs LangGraph 工作流：公平对照实验。

公平性约束（缺一条就不算对比）：

* **同一批会议输入**、同一批场景（`scenarios.py`，40 个，含故障注入）；
* **同一套组件**：分析 / 草稿 / 发送都由相同的 fake（或同一个真实 provider）提供；
* **同一套业务规则**：复用 `meetings.sanitize` / `store.add_meeting` / 状态字段语义；
* **同一套评分**：终态 + 业务行数 + 副作用次数，不看"哪边代码更优雅"。

能力差异**单独标注**，不算进任务成功率：
原流程（改造前）没有 Checkpoint、没有中断恢复，所以"重启恢复"类场景对它是
`recovery_supported=False`，从恢复率分母里剔除，并单独报告。

用法：
    python -m benchmarks.workflow.runner            # 全部 40 个场景
    python -m benchmarks.workflow.runner --limit 5  # 只跑前 5 个

关于“为什么不用真实 LLM 跑对照”：本实验比的是**编排能力**（状态、检查点、重试、
幂等、恢复），不是模型质量。用确定性 fake 才能把差异归因到编排层；真实 LLM 的
非确定性会让“风险数量”本身变成噪声。真实 LLM 的联通性由
`tests/test_workflow_langchain.py` 中的显式用例验证（需 `AIEB_TEST_LIVE_LLM=1`）。
发送一律用 Fake Sender / 进程内 Fake SMTP，**不会发出真实邮件**。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import time
from pathlib import Path
from typing import Any

from aieb.brain import meetings
from aieb.brain.meetings import MeetingAnalysis, RiskIn, TodoIn
from aieb.brain.store import BrainStore
from aieb.workflow.deps import WorkflowDeps
from aieb.workflow.persistence import WorkflowStore
from aieb.workflow.runner import WorkflowRunner
from aieb.workflow.state import ResumeCommand
from benchmarks import common
from benchmarks.workflow.scenarios import Scenario, build_scenarios, summary
from tests.fake_smtp import FakeSMTPServer
from tests.workflow_harness import Fakes, FakeWorld

OUT_DIR = common.RESULTS_DIR / "workflow"

TO_ADDR = "team@example.com"

#: 分析节点允许的尝试次数（原生流程 = 1，即不重试；LangGraph = 节点内显式重试）
NATIVE_ATTEMPTS = 1
LANGGRAPH_ATTEMPTS = int(WorkflowDeps.__dataclass_fields__["retry_attempts"].default)


def analysis_for(scenario: Scenario) -> MeetingAnalysis:
    """按场景期望造一份确定性分析结果，并保证 evidence 能在原文里找到。"""
    evidence = scenario.text[:16]
    risks = [
        RiskIn(
            title=f"风险{'ABCDE'[i % 5]}{i}",
            level="high" if i == 0 else "medium",
            risk_type="进度" if i % 2 == 0 else "质量",
            status="open",
            description=f"第{i}类问题的说明：交付进度与质量均出现偏差",
            # 用不同片段做依据，避免被 risk_rules 的相似度去重合并
            evidence=scenario.text[i * 3 : i * 3 + 14] or evidence,
            suggestion=f"建议措施{i}：安排专项跟进",
        )
        for i in range(scenario.expected_risks)
    ]
    todos = [
        TodoIn(content=f"待办{i}", owner="", due_date="", priority="medium")
        for i in range(scenario.expected_todos)
    ]
    return MeetingAnalysis(
        title="会议",
        summary="摘要",
        participants=[],
        discussion=[],
        decisions=[],
        risks=risks,
        todos=todos,
    )


# ---------------------------------------------------------------------------
# 原生流程（改造前的编排方式：一把梭，无 Checkpoint、无中断）
# ---------------------------------------------------------------------------


async def run_native(
    scenario: Scenario,
    *,
    store: BrainStore,
    world: FakeWorld,
    fakes: Fakes,
    sender,
) -> dict[str, Any]:
    started = time.perf_counter()
    status = "failed"
    sends_before = fakes.send_calls
    error = ""
    try:
        text = scenario.text
        # 原生流程没有重试：fake 分析器第一次就抛错，整轮直接失败（不预先造失败，
        # 让失败真的发生在调用点上，这样 analysis_calls 才是可解释的）。
        outcome = await world.analyzer("会议.txt", text)
        cleaned = meetings.sanitize(outcome.analysis, text)
        existing = store.find_meeting_by_text(text)
        meeting = existing or meetings.save(store, cleaned, text, "会议.txt")
        draft = await world.drafter(store.get_meeting(meeting["id"]) or {}, TO_ADDR, "")
        email = store.create_email(
            to_addr=TO_ADDR,
            subject=draft.subject,
            body=draft.body,
            meeting_id=meeting["id"],
            version=1,
            draft_hash=f"native-{meeting['id']}",
        )
        if scenario.decision == "reject":
            store.update_email(email["id"], status="cancelled", error="用户取消")
            status = "cancelled"
        elif scenario.decision == "edit":
            store.update_email(
                email["id"], subject=draft.subject + "（改）", body=draft.body + "（改）"
            )
        if scenario.decision in {"approve", "edit"}:
            current = store.get_email(email["id"]) or {}
            if current.get("status") != "sent":
                result = sender(
                    current.get("to_addr", ""), current.get("subject", ""), current.get("body", "")
                )
                store.update_email(
                    email["id"],
                    status="sent" if result.ok else "failed",
                    error="" if result.ok else result.message,
                )
                if scenario.duplicate_confirm:
                    # 原生流程的重复点击：靠"已发送就不发"这个状态判断兜底
                    again = store.get_email(email["id"]) or {}
                    if again.get("status") != "sent":
                        sender(again.get("to_addr", ""), again.get("subject", ""), again.get("body", ""))
            # 原生流程没有“工作流状态”这个概念，这里用邮件结果近似：发送失败即任务失败
            final = store.get_email(email["id"]) or {}
            status = "completed" if final.get("status") == "sent" else "failed"
    except Exception as exc:  # noqa: BLE001 - 原生流程失败就是失败
        error = f"{type(exc).__name__}: {exc}"
        status = "failed"
    elapsed = (time.perf_counter() - started) * 1000
    return {
        "status": status,
        "latency_ms": round(elapsed, 1),
        "sends": fakes.send_calls - sends_before,
        "error": error,
        "recovery_supported": False,
        "recovered": None,
    }


# ---------------------------------------------------------------------------
# LangGraph 流程
# ---------------------------------------------------------------------------


async def run_langgraph(
    scenario: Scenario,
    *,
    workspace: Path,
    store: BrainStore,
    wf: WorkflowStore,
    world: FakeWorld,
    fakes: Fakes,
    sender,
    restart: bool,
) -> dict[str, Any]:
    from aieb.workflow.deps import WorkflowDeps

    deps = WorkflowDeps(
        store=store,
        wf=wf,
        workspace=workspace,
        analyzer=world.analyzer,
        drafter=world.drafter,
        sender=sender,
        model_name="fixture",
        activity=store.add_activity,
    )
    runner = WorkflowRunner(deps=deps, workspace=workspace)
    started = time.perf_counter()
    status = "failed"
    error = ""
    recovered: bool | None = None
    checkpoint_recovery_ms: float | None = None
    sends_before = fakes.send_calls
    try:
        snapshot = await runner.start(name="会议.txt", text=scenario.text, to_addr=TO_ADDR)
        if restart and snapshot.resumable:
            runner.close()
            runner = WorkflowRunner(deps=deps, workspace=workspace)
            t_resume = time.perf_counter()
            snapshot = await runner.status(snapshot.workflow_id)
            checkpoint_recovery_ms = round((time.perf_counter() - t_resume) * 1000, 1)
            recovered = snapshot.resumable
        if scenario.decision == "none":
            status = snapshot.execution_status
        else:
            pending = await runner.pending_approval(snapshot.workflow_id)
            if pending is None:
                status = snapshot.execution_status
            else:
                command = ResumeCommand(
                    decision=scenario.decision,  # type: ignore[arg-type]
                    version=pending.version,
                    draft_hash=pending.draft_hash,
                )
                result = await runner.resume(snapshot.workflow_id, command)
                if scenario.decision == "edit" and result.resumable:
                    # 产品语义：修改后必须**再确认一次**（版本已 +1）
                    again = await runner.pending_approval(snapshot.workflow_id)
                    if again is not None:
                        result = await runner.resume(
                            snapshot.workflow_id,
                            ResumeCommand(
                                decision="approve",
                                version=again.version,
                                draft_hash=again.draft_hash,
                            ),
                        )
                if scenario.duplicate_confirm:
                    for _ in range(2):
                        result = await runner.resume(snapshot.workflow_id, command)
                status = result.execution_status
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"
        status = "failed"
    finally:
        elapsed = (time.perf_counter() - started) * 1000
        events = wf.list_events(scenario.scenario_id)  # 仅占位，真实事件按 workflow_id 查
        del events
        runner.close()
    return {
        "status": status,
        "latency_ms": round(elapsed, 1),
        "sends": fakes.send_calls - sends_before,
        "error": error,
        "recovery_supported": True,
        "recovered": recovered,
        "checkpoint_recovery_ms": checkpoint_recovery_ms,
        "workflow_id": snapshot.workflow_id if "snapshot" in dir() else "",
    }


# ---------------------------------------------------------------------------
# 评测
# ---------------------------------------------------------------------------


def evaluate(
    scenario: Scenario,
    result: dict[str, Any],
    *,
    meetings_count: int,
    risks_count: int,
    todos_count: int,
    sent_total: int,
    expect_risks: int,
    expect_todos: int,
    expect_sends: int | None = None,
    expect_meetings: int = 1,
    capability_gap: bool = False,
) -> dict[str, Any]:
    if expect_sends is not None:
        expected_sends = expect_sends
    elif scenario.expected_sends is not None:
        expected_sends = scenario.expected_sends
    else:
        expected_sends = 1 if scenario.decision in {"approve", "edit"} else 0
    duplicates = max(0, sent_total - expected_sends)
    business_ok = (
        meetings_count == expect_meetings
        and risks_count == expect_risks
        and todos_count == expect_todos
    )
    status_ok = result["status"] == scenario.expect_status
    sends_ok = sent_total == expected_sends
    success = bool(business_ok and status_ok and sends_ok)
    return {
        "success": success,
        "business_ok": business_ok,
        "status_ok": status_ok,
        "sends_ok": sends_ok,
        "capability_gap": capability_gap,
        "duplicate_side_effects": duplicates,
        "meetings": meetings_count,
        "risks": risks_count,
        "todos": todos_count,
        "sends": sent_total,
    }


def pct(values: list[float], p: float) -> float:
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


async def run_backend(name: str, scenarios: list[Scenario], root: Path) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for scenario in scenarios:
        workspace = root / name / scenario.scenario_id
        shutil.rmtree(workspace, ignore_errors=True)
        workspace.mkdir(parents=True, exist_ok=True)
        store = BrainStore(workspace)
        wf = WorkflowStore(workspace / ".aieb" / "workflow" / "workflow.sqlite3")
        fakes = Fakes()
        fakes.analysis = analysis_for(scenario)
        world = FakeWorld(fakes)
        fakes.analysis_failures = scenario.analysis_failures
        fakes.draft_failures = scenario.draft_failures
        fakes.send_raises = scenario.send_raises
        fakes.send_fails = scenario.send_fails
        fakes.send_uncertain = scenario.send_uncertain

        def sender(to_addr: str, subject: str, body: str):  # noqa: ANN001
            return world.sender(to_addr, subject, body)

        try:
            if name == "native":
                result = await run_native(
                    scenario, store=store, world=world, fakes=fakes, sender=sender
                )
            else:
                result = await run_langgraph(
                    scenario,
                    workspace=workspace,
                    store=store,
                    wf=wf,
                    world=world,
                    fakes=fakes,
                    sender=sender,
                    restart=scenario.restart_before_approval,
                )
            counts = {
                "meetings_count": len(store.list_meetings()),
                "risks_count": len(store.list_risks()),
                "todos_count": len(store.list_todos()),
                "sent_total": len(fakes.sent),
            }
            # 期望值以**确定性校验器**（meetings.sanitize）的输出为准：
            # 风险/待办数量本来就由业务规则决定，不应该在基准里写死一个数字。
            cleaned = meetings.sanitize(analysis_for(scenario), scenario.text)
            # 分析阶段**永久失败**时，任何正确的实现都不应落库、也不应发邮件：
            # 这是规格（期望值），不是"照抄实测结果"。
            attempts = NATIVE_ATTEMPTS if name == "native" else LANGGRAPH_ATTEMPTS
            permanent = scenario.analysis_failures >= attempts
            score = evaluate(
                scenario,
                result,
                expect_risks=0 if permanent else len(cleaned.risks),
                expect_todos=0 if permanent else len(cleaned.todos),
                expect_sends=0 if permanent else None,
                expect_meetings=0 if permanent else 1,
                # 原生流程（改造前）根本没有“暂停等人工/中断恢复”这项能力，
                # 按任务书要求：这是**能力差异**，不能算成它的任务失败。
                capability_gap=(name == "native" and scenario.decision == "none"),
                **counts,
            )
        finally:
            store.close()
            wf.close()
        rows.append(
            {
                "scenario_id": scenario.scenario_id,
                "name": scenario.name,
                "tags": scenario.tags,
                "decision": scenario.decision,
                "expect_status": scenario.expect_status,
                "analysis_calls": fakes.analysis_calls,
                "draft_calls": fakes.draft_calls,
                **result,
                **score,
            }
        )
        print(
            f"  [{name}] {scenario.scenario_id} {scenario.name}: "
            f"{'OK ' if score['success'] else 'x  '} status={result['status']} "
            f"sends={score['sends']} {result['latency_ms']}ms",
            flush=True,
        )

    latencies = [r["latency_ms"] for r in rows]
    scorable = [r for r in rows if not r.get("capability_gap")]
    success = [r for r in scorable if r["success"]]
    recovery_rows = [r for r in rows if r["recovery_supported"] and r["recovered"] is not None]
    return {
        "backend": name,
        "scenarios": len(rows),
        "scorable": len(scorable),
        "capability_gaps": [r["scenario_id"] for r in rows if r.get("capability_gap")],
        "task_success_rate": round(len(success) / max(1, len(scorable)), 4),
        "p50_latency_ms": pct(latencies, 50),
        "p95_latency_ms": pct(latencies, 95),
        "llm_calls": sum(r["analysis_calls"] + r["draft_calls"] for r in rows),
        "tool_calls": sum(r["sends"] for r in rows),
        "duplicate_side_effects": sum(r["duplicate_side_effects"] for r in rows),
        "recovery_supported": name != "native",
        "recovery_scenarios": len(recovery_rows),
        "recovery_success_rate": (
            round(sum(1 for r in recovery_rows if r["recovered"]) / len(recovery_rows), 4)
            if recovery_rows
            else None
        ),
        "rows": rows,
    }


async def main_async(args: argparse.Namespace) -> int:
    scenarios = build_scenarios()[: args.limit] if args.limit else build_scenarios()
    root = common.WORK_ROOT / "wf_bench"
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)

    print("=" * 88)
    print(f"工作流对照实验：{len(scenarios)} 个场景")
    print(json.dumps(summary(scenarios), ensure_ascii=False))
    print("=" * 88)

    results = {}
    for backend in ("native", "langgraph"):
        print(f">>> {backend}")
        results[backend] = await run_backend(backend, scenarios, root)

    payload = {
        "mode": "fixture",
        "dataset": summary(scenarios),
        "results": {k: {kk: vv for kk, vv in v.items() if kk != "rows"} for k, v in results.items()},
        "rows": {k: v["rows"] for k, v in results.items()},
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / "workflow_bench_fixture.json"
    common.write_json(path, payload)

    print()
    print("=" * 88)
    print(f"{'指标':<26}{'原生流程':>18}{'LangGraph':>18}")
    print("-" * 88)
    a, b = results["native"], results["langgraph"]
    for key, label in (
        ("task_success_rate", "任务成功率"),
        ("p50_latency_ms", "P50 延迟 (ms)"),
        ("p95_latency_ms", "P95 延迟 (ms)"),
        ("llm_calls", "LLM 调用次数"),
        ("tool_calls", "工具/发送次数"),
        ("duplicate_side_effects", "重复副作用次数"),
        ("recovery_success_rate", "恢复成功率"),
    ):
        left = a.get(key)
        right = b.get(key)
        print(f"{label:<26}{str(left):>18}{str(right):>18}")
    print("-" * 88)
    print("恢复能力：原生流程 = 不支持（无 Checkpoint）；LangGraph = 支持（SQLite Checkpoint）")
    print(f"结果 → {path}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="原生流程 vs LangGraph 对照实验")
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
