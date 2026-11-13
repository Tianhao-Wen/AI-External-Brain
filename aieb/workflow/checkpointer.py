"""Checkpointer：把每个 super-step 的快照落到本地 SQLite。

为什么不用 `MemorySaver`：内存快照在进程退出时消失，恰好是本次要解决的核心问题
（"服务重启后继续原工作流"）。`langgraph-checkpoint-sqlite` 的 `SqliteSaver` 把快照写进
独立数据库，重启后只要 `thread_id` 一致就能 `get_state()` 读回来。

数据库位置：`<workspace>/.aieb/workflow/checkpoints.sqlite3`
（与业务库 `brain.sqlite3`、编排库 `workflow.sqlite3` 分开，职责不混）
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from langgraph.checkpoint.sqlite import SqliteSaver


def checkpoints_path(workspace: Path | str) -> Path:
    path = Path(workspace) / ".aieb" / "workflow" / "checkpoints.sqlite3"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def open_checkpointer(
    workspace: Path | str, *, path: Path | str | None = None
) -> tuple[SqliteSaver, sqlite3.Connection]:
    """打开（必要时创建）持久化 Checkpointer。

    返回 `(saver, conn)`，调用方负责在退出时 `conn.close()`。
    `check_same_thread=False` 必须开：图可能在 `asyncio.to_thread` 的工作线程里执行。
    """
    db = Path(path) if path else checkpoints_path(workspace)
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db), check_same_thread=False, timeout=15.0)
    conn.execute("PRAGMA journal_mode=WAL")
    saver = SqliteSaver(conn)
    saver.setup()
    return saver, conn


def thread_config(workflow_id: str) -> dict[str, Any]:
    """工作流与 thread 一一对应：thread_id 稳定，才能跨重启找回同一条执行线。"""
    return {"configurable": {"thread_id": workflow_id}}
