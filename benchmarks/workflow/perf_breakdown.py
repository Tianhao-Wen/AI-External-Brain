"""性能开销分解（不调用模型，纯编排/持久化成本）。

真实 LLM 场景下 LLM 占绝对大头（见 docs/LANGGRAPH_LIVE_BENCHMARK.md），
所以这里专门量化"LangGraph 自己引入的固定开销"：

1. 同一张图分别用 **InMemorySaver** 与 **AsyncSqliteSaver** 跑，差值≈SQLite Checkpoint 往返成本；
2. 单次 `aget_state`（读 checkpoint）、`append_event`（写 Trace）、`add_meeting`（写业务库）的耗时；
3. 节点耗时合计 vs 端到端墙钟 → "图调度 + checkpoint + Trace 写入" 的合并项。

    python -m benchmarks.workflow.perf_breakdown
"""

from __future__ import annotations

import asyncio
import statistics
import time
from pathlib import Path
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from aieb.brain.store import BrainStore
from aieb.config import ProviderConfig
from aieb.workflow.graph import build_graph
from aieb.workflow.persistence import WorkflowStore
from benchmarks import common
from benchmarks.workflow.chaos import make_deps

TEXT = (
    "项目周会纪要（2026年11月10日）\n"
    "参会人：张伟、李娜、王强\n"
    "一、关键物料交付延期两周，影响联调窗口，目前尚未恢复。\n"
    "二、决定启动备选供应商，由李娜负责在10月15日前给出备选供应商清单。\n"
)
ROUNDS = 7
OUT = common.RESULTS_DIR / "workflow" / "perf_breakdown.json"


def _payload(workflow_id: str, document_id: str) -> dict[str, Any]:
    return {
        "workflow_id": workflow_id,
        "thread_id": workflow_id,
        "document_id": document_id,
        "source_name": "周会.txt",
        "text_hash": "perf",
        "to_addr": "team@example.com",
        "execution_status": "running",
        "current_step": "start",
        "started_at": "2026-11-11T00:00:00+00:00",
        "retry_count": 0,
    }


async def _run_with_saver(saver: Any, workspace: Path, label: str) -> dict[str, Any]:
    store = BrainStore(workspace)
    wf = WorkflowStore(workspace / ".aieb" / "workflow" / "workflow.sqlite3")
    deps = make_deps(store, wf, workspace)
    graph = build_graph(deps, checkpointer=saver)
    totals: list[float] = []
    node_sums: list[float] = []
    try:
        for index in range(ROUNDS):
            workflow_id = f"wf_{label}_{index}"
            document_id = deps.stage(workflow_id, f"{TEXT}\n（perf 第 {index} 轮）")
            started = time.perf_counter()
            await graph.ainvoke(
                _payload(workflow_id, document_id),
                {"configurable": {"thread_id": workflow_id}},
            )
            totals.append((time.perf_counter() - started) * 1000)
            events = wf.list_events(workflow_id)
            node_sums.append(sum(float(e["duration_ms"]) for e in events if e["event"] in {"node", "interrupt"}))
    finally:
        store.close()
        wf.close()
    return {
        "saver": label,
        "rounds": ROUNDS,
        "total_median_ms": round(statistics.median(totals), 2),
        "total_min_ms": round(min(totals), 2),
        "total_max_ms": round(max(totals), 2),
        "node_sum_median_ms": round(statistics.median(node_sums), 2),
        "overhead_median_ms": round(statistics.median(totals) - statistics.median(node_sums), 2),
    }


async def _micro(workspace: Path) -> dict[str, Any]:
    store = BrainStore(workspace)
    wf = WorkflowStore(workspace / ".aieb" / "workflow" / "workflow.sqlite3")
    deps = make_deps(store, wf, workspace)

    # append_event
    started = time.perf_counter()
    for i in range(50):
        wf.append_event("wf_micro", node="n", event="node", duration_ms=1.0, detail={"i": i})
    append_ms = (time.perf_counter() - started) * 1000 / 50

    # add_meeting（含风险/待办）
    started = time.perf_counter()
    store.add_meeting(
        title="perf",
        summary="perf",
        decisions=[],
        participants=[],
        discussion=[],
        source_name="perf.txt",
        raw_text=TEXT * 20,
        risks=[{"title": "r", "level": "high", "description": "d", "evidence": "关键物料交付延期两周"}],
        todos=[{"content": "t", "owner": "李娜", "due_date": "10月15日", "priority": "high"}],
    )
    add_meeting_ms = (time.perf_counter() - started) * 1000

    # aget_state（读 checkpoint）—— 需要一张已跑过 interrupt 的图
    import aiosqlite

    db = workspace / ".aieb" / "workflow" / "checkpoints_perf.sqlite3"
    conn = await aiosqlite.connect(str(db))
    saver = AsyncSqliteSaver(conn)
    await saver.setup()
    graph = build_graph(deps, checkpointer=saver)
    deps_read: list[float] = []
    try:
        workflow_id = "wf_micro_state"
        document_id = deps.stage(workflow_id, f"{TEXT}\n（micro）")
        await graph.ainvoke(
            _payload(workflow_id, document_id),
            {"configurable": {"thread_id": workflow_id}},
        )
        for _ in range(20):
            started = time.perf_counter()
            await graph.aget_state({"configurable": {"thread_id": workflow_id}})
            deps_read.append((time.perf_counter() - started) * 1000)
    finally:
        await conn.close()
        store.close()
        wf.close()

    return {
        "append_event_ms": round(append_ms, 3),
        "add_meeting_ms": round(add_meeting_ms, 2),
        "aget_state_ms": round(statistics.median(deps_read), 3) if deps_read else None,
        "note": "Windows 11 / Python 3.14 / WAL；单进程单线程，非并发压测",
    }


async def main_async() -> int:
    root = common.WORK_ROOT / "perf"
    import shutil

    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)

    memory_row = await _run_with_saver(InMemorySaver(), root / "mem", "mem")
    micro = await _micro(root / "micro")

    import aiosqlite

    sql_dir = root / "sql" / ".aieb" / "workflow"
    sql_dir.mkdir(parents=True, exist_ok=True)
    conn = await aiosqlite.connect(str(sql_dir / "cp.sqlite3"))
    saver = AsyncSqliteSaver(conn)
    await saver.setup()
    try:
        sql_row = await _run_with_saver(saver, root / "sql", "sql")
    finally:
        await conn.close()

    payload = {
        "environment": {
            "python": "3.14.2",
            "os": "Windows 11",
            "langgraph": "1.2.20",
            "checkpoint_sqlite": "3.1.3",
            "note": "全部使用确定性 fake 依赖（不调用模型），因此只反映编排/持久化成本",
        },
        "graph_run": [memory_row, sql_row],
        "sqlite_checkpoint_extra_ms": round(
            sql_row["total_median_ms"] - memory_row["total_median_ms"], 2
        ),
        "micro": micro,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    common.write_json(OUT, payload)

    print("图执行（同一张图、同一批 fake，仅更换 Checkpointer）")
    for row in (memory_row, sql_row):
        print(
            f"  {row['saver']:<8} total={row['total_median_ms']}ms "
            f"节点合计={row['node_sum_median_ms']}ms 差值={row['overhead_median_ms']}ms"
        )
    print(f"SQLite Checkpoint 额外开销：{payload['sqlite_checkpoint_extra_ms']}ms（中位数差）")
    print(f"单次 append_event：{micro['append_event_ms']}ms；"
          f"单次 add_meeting：{micro['add_meeting_ms']}ms；"
          f"单次 aget_state：{micro['aget_state_ms']}ms")
    print(f"结果 → {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main_async()))
