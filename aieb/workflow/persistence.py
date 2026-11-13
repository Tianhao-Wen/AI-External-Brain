"""工作流编排数据的持久化：运行表、节点级 Trace、幂等键。

**刻意与业务库分开**（`.aieb/workflow/workflow.sqlite3` vs `.aieb/brain/brain.sqlite3`）：

* 业务真相只有一份（会议/风险/待办/邮件），在 `brain.sqlite3`；
* 编排元数据（跑到哪一步、哪些副作用执行过、Trace）在 `workflow.sqlite3`；
* 两者用 `workflow_id` / `meeting_id` / `email_draft_id` 关联，**不把业务数据复制过来**。

这样即使把 workflow.sqlite3 整个删掉，业务数据依然完整；反过来，重置 Checkpoint 也不会
删掉任何业务记录（恢复时靠幂等键 + 业务去重避免重复副作用）。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS workflow_runs (
    workflow_id TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL,
    session_id TEXT NOT NULL DEFAULT '',
    source_name TEXT NOT NULL DEFAULT '',
    text_hash TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    current_step TEXT NOT NULL DEFAULT '',
    meeting_id TEXT NOT NULL DEFAULT '',
    email_draft_id TEXT NOT NULL DEFAULT '',
    email_version INTEGER NOT NULL DEFAULT 0,
    approval_status TEXT NOT NULL DEFAULT 'none',
    error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_runs_status ON workflow_runs(status, updated_at);

CREATE TABLE IF NOT EXISTS workflow_events (
    id TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    node TEXT NOT NULL DEFAULT '',
    event TEXT NOT NULL DEFAULT 'node',
    status TEXT NOT NULL DEFAULT 'ok',
    started_at TEXT NOT NULL DEFAULT '',
    ended_at TEXT NOT NULL DEFAULT '',
    duration_ms REAL NOT NULL DEFAULT 0,
    model TEXT NOT NULL DEFAULT '',
    tool TEXT NOT NULL DEFAULT '',
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    retry INTEGER NOT NULL DEFAULT 0,
    error_type TEXT NOT NULL DEFAULT '',
    checkpoint_id TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_events_run ON workflow_events(workflow_id, seq);

CREATE TABLE IF NOT EXISTS workflow_idempotency (
    key TEXT PRIMARY KEY,
    workflow_id TEXT NOT NULL DEFAULT '',
    action TEXT NOT NULL DEFAULT '',
    object_id TEXT NOT NULL DEFAULT '',
    version INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    result TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_idem_run ON workflow_idempotency(workflow_id, action);
"""

#: 幂等键状态机的合法取值
EFFECT_STATUSES = (
    "pending",
    "approved",
    "executing",
    "succeeded",
    "failed",
    "uncertain",
    "cancelled",
)

#: 允许把同一个键再次推进的状态（终态之外）
_RETRYABLE = {"pending", "failed", "cancelled"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class WorkflowStore:
    """编排元数据仓库（sqlite3，单连接 + 可重入锁 + WAL）。"""

    _RUN_FIELDS = {
        "status",
        "current_step",
        "meeting_id",
        "email_draft_id",
        "email_version",
        "approval_status",
        "error",
        "completed_at",
        "session_id",
        "source_name",
        "text_hash",
    }

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False, timeout=10.0)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- runs ----------------------------------------------------------

    def create_run(
        self,
        workflow_id: str,
        *,
        thread_id: str = "",
        session_id: str = "",
        source_name: str = "",
        text_hash: str = "",
        status: str = "pending",
        current_step: str = "queued",
    ) -> dict[str, Any]:
        now = _now()
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO workflow_runs"
                " (workflow_id,thread_id,session_id,source_name,text_hash,status,current_step,"
                "  created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    workflow_id,
                    thread_id or workflow_id,
                    session_id,
                    source_name,
                    text_hash,
                    status,
                    current_step,
                    now,
                    now,
                ),
            )
            self._conn.commit()
        return self.get_run(workflow_id) or {}

    def update_run(self, workflow_id: str, **fields: Any) -> dict[str, Any] | None:
        items = {k: v for k, v in fields.items() if k in self._RUN_FIELDS}
        if not items:
            return self.get_run(workflow_id)
        assignments = ", ".join(f"{k}=?" for k in items)
        with self._lock:
            self._conn.execute(
                f"UPDATE workflow_runs SET {assignments}, updated_at=? WHERE workflow_id=?",
                (*items.values(), _now(), workflow_id),
            )
            self._conn.commit()
        return self.get_run(workflow_id)

    def get_run(self, workflow_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM workflow_runs WHERE workflow_id=?", (workflow_id,)
            ).fetchone()
        return dict(row) if row else None

    def find_run_by_text(self, text_hash: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM workflow_runs WHERE text_hash=? AND status!='cancelled'"
                " ORDER BY created_at DESC LIMIT 1",
                (text_hash,),
            ).fetchone()
        return dict(row) if row else None

    def list_runs(self, *, limit: int = 50, status: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM workflow_runs"
        params: list[Any] = []
        if status:
            sql += " WHERE status=?"
            params.append(status)
        sql += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, tuple(params)).fetchall()
        return [dict(r) for r in rows]

    # -- events (Trace) ------------------------------------------------

    def append_event(
        self,
        workflow_id: str,
        *,
        node: str = "",
        event: str = "node",
        status: str = "ok",
        started_at: str = "",
        ended_at: str = "",
        duration_ms: float = 0.0,
        model: str = "",
        tool: str = "",
        input_tokens: int = 0,
        output_tokens: int = 0,
        retry: int = 0,
        error_type: str = "",
        checkpoint_id: str = "",
        detail: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            seq = (
                self._conn.execute(
                    "SELECT COALESCE(MAX(seq),0)+1 FROM workflow_events WHERE workflow_id=?",
                    (workflow_id,),
                ).fetchone()[0]
            )
            event_id = f"evt_{uuid.uuid4().hex[:12]}"
            self._conn.execute(
                "INSERT INTO workflow_events (id,workflow_id,seq,node,event,status,started_at,"
                " ended_at,duration_ms,model,tool,input_tokens,output_tokens,retry,error_type,"
                " checkpoint_id,detail) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    event_id,
                    workflow_id,
                    seq,
                    node,
                    event,
                    status,
                    started_at,
                    ended_at,
                    float(duration_ms),
                    model,
                    tool,
                    int(input_tokens),
                    int(output_tokens),
                    int(retry),
                    error_type,
                    checkpoint_id,
                    json.dumps(detail or {}, ensure_ascii=False, default=str),
                ),
            )
            self._conn.commit()
        return {"id": event_id, "seq": seq}

    def list_events(self, workflow_id: str, *, limit: int = 200) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM workflow_events WHERE workflow_id=? ORDER BY seq LIMIT ?",
                (workflow_id, limit),
            ).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            try:
                item["detail"] = json.loads(item.get("detail") or "{}")
            except json.JSONDecodeError:
                item["detail"] = {}
            out.append(item)
        return out

    def list_waiting(self, *, limit: int = 50) -> list[dict[str, Any]]:
        """谁在等用户审批（可观测性问题之一）。"""
        return self.list_runs(limit=limit, status="waiting_approval")

    def token_totals(self, workflow_id: str) -> dict[str, int]:
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(SUM(input_tokens),0) AS i, COALESCE(SUM(output_tokens),0) AS o"
                " FROM workflow_events WHERE workflow_id=?",
                (workflow_id,),
            ).fetchone()
        return {"input_tokens": int(row["i"]), "output_tokens": int(row["o"])}

    # -- 幂等键 ---------------------------------------------------------

    def get_effect(self, key: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM workflow_idempotency WHERE key=?", (key,)
            ).fetchone()
        return dict(row) if row else None

    def begin_effect(
        self,
        key: str,
        *,
        workflow_id: str,
        action: str,
        object_id: str = "",
        version: int = 0,
    ) -> tuple[bool, dict[str, Any]]:
        """尝试把副作用推进到 `executing`。返回 (是否允许执行, 记录)。

        规则（这就是"重复点击/重复恢复不会重复执行"的实现）：

        * 没有记录 → 建 `executing`，允许执行；
        * `succeeded` → 拒绝执行（已完成，直接复用结果）；
        * `executing` → 拒绝执行（有别的执行者正在做，或上次崩在中途，交给人工/上层判定）；
        * `uncertain` → 拒绝执行（SMTP 可能已接受，重试有重复投递风险）；
        * `failed` / `cancelled` / `pending` → 允许重试。
        """
        now = _now()
        with self._lock:
            # BEGIN IMMEDIATE：两个并发请求只能有一个把状态从非 executing 推到 executing
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT * FROM workflow_idempotency WHERE key=?", (key,)
                ).fetchone()
                if row is None:
                    self._conn.execute(
                        "INSERT INTO workflow_idempotency (key,workflow_id,action,object_id,"
                        " version,status,attempts,created_at,updated_at)"
                        " VALUES (?,?,?,?,?,'executing',1,?,?)",
                        (key, workflow_id, action, object_id, int(version), now, now),
                    )
                    self._conn.commit()
                    return True, self.get_effect(key) or {}
                record = dict(row)
                if record["status"] not in _RETRYABLE:
                    self._conn.commit()
                    return False, record
                self._conn.execute(
                    "UPDATE workflow_idempotency SET status='executing', attempts=attempts+1,"
                    " updated_at=?, error='' WHERE key=?",
                    (now, key),
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        return True, self.get_effect(key) or {}

    def finish_effect(
        self,
        key: str,
        status: str,
        *,
        result: dict[str, Any] | None = None,
        error: str = "",
    ) -> dict[str, Any]:
        if status not in EFFECT_STATUSES:
            raise ValueError(f"非法副作用状态：{status}")
        with self._lock:
            self._conn.execute(
                "UPDATE workflow_idempotency SET status=?, result=?, error=?, updated_at=?"
                " WHERE key=?",
                (status, json.dumps(result or {}, ensure_ascii=False, default=str), error, _now(), key),
            )
            self._conn.commit()
        return self.get_effect(key) or {}

    def list_effects(self, workflow_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM workflow_idempotency WHERE workflow_id=? ORDER BY created_at",
                (workflow_id,),
            ).fetchall()
        return [dict(r) for r in rows]


def effect_key(workflow_id: str, action: str, object_id: str = "", version: int = 0) -> str:
    """稳定幂等键：workflow_id + action + object_id + version。"""
    return f"{workflow_id}:{action}:{object_id or '-'}:v{int(version)}"


def rows_to(rows: Iterable[sqlite3.Row]) -> list[dict[str, Any]]:  # pragma: no cover - 兼容辅助
    return [dict(r) for r in rows]
