"""外脑业务数据存储：SQLite 单文件，结构化保存文档、会议、风险、待办、邮件与活动。

设计取向
--------
* 只用一个 SQLite 文件（``.aieb/brain/brain.sqlite3``），不引入额外数据库进程。
* 所有写入走同一把线程锁，Web 端（asyncio 线程）与后台任务都能安全调用。
* 删除文档/会议时级联清理关联的分块、风险、待办与检索索引，避免"UI 里没了但
  磁盘里还在"的不一致。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from aieb.brain import paths
from aieb.brain.chunkkey import content_hash

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS documents (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    ext TEXT NOT NULL,
    size INTEGER NOT NULL DEFAULT 0,
    pages INTEGER NOT NULL DEFAULT 0,
    chars INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending',
    error TEXT NOT NULL DEFAULT '',
    stored_path TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chunks (
    id TEXT PRIMARY KEY,
    doc_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    page INTEGER NOT NULL DEFAULT 0,
    idx INTEGER NOT NULL DEFAULT 0,
    text TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks(doc_id);

CREATE TABLE IF NOT EXISTS meetings (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    meeting_time TEXT NOT NULL DEFAULT '',
    location TEXT NOT NULL DEFAULT '',
    participants TEXT NOT NULL DEFAULT '[]',
    summary TEXT NOT NULL DEFAULT '',
    decisions TEXT NOT NULL DEFAULT '[]',
    discussion TEXT NOT NULL DEFAULT '[]',
    source_name TEXT NOT NULL DEFAULT '',
    raw_text TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS risks (
    id TEXT PRIMARY KEY,
    meeting_id TEXT NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    level TEXT NOT NULL DEFAULT 'medium',
    description TEXT NOT NULL DEFAULT '',
    evidence TEXT NOT NULL DEFAULT '',
    suggestion TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'open',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_risks_meeting ON risks(meeting_id);

CREATE TABLE IF NOT EXISTS todos (
    id TEXT PRIMARY KEY,
    meeting_id TEXT REFERENCES meetings(id) ON DELETE CASCADE,
    risk_id TEXT,
    content TEXT NOT NULL,
    owner TEXT NOT NULL DEFAULT '',
    due_date TEXT NOT NULL DEFAULT '',
    priority TEXT NOT NULL DEFAULT 'medium',
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_todos_meeting ON todos(meeting_id);

CREATE TABLE IF NOT EXISTS emails (
    id TEXT PRIMARY KEY,
    meeting_id TEXT REFERENCES meetings(id) ON DELETE SET NULL,
    kind TEXT NOT NULL DEFAULT 'reminder',
    to_addr TEXT NOT NULL DEFAULT '',
    subject TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'draft',
    error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    sent_at TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS activities (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS memory_facts (
    id TEXT PRIMARY KEY,
    category TEXT NOT NULL,
    content TEXT NOT NULL,
    source_kind TEXT NOT NULL DEFAULT '',
    source_id TEXT NOT NULL DEFAULT '',
    source_label TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_facts_category ON memory_facts(category);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _normalize_meeting_text(text: str) -> str:
    """会议原文归一化：用于判断"这是不是同一份会议记录"。

    只做无损归一化（去首尾空白 + 连续空白折叠），不改动任何实质内容，
    避免因为多一个换行/缩进就把同一份记录当成新会议再分析一遍。
    """
    return " ".join((text or "").split())


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class BrainStore:
    """外脑业务数据仓库。"""

    def __init__(self, work_dir: str | Path | None = None, *, path: Path | None = None) -> None:
        self.work_dir = Path(work_dir) if work_dir else Path.cwd()
        self.path = Path(path) if path else paths.db_path(self.work_dir)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._migrate()
            self._conn.commit()

    #: 后加的列：(表, 列, 列定义)。只用 ADD COLUMN，不动旧数据。
    _ADDED_COLUMNS = (
        ("documents", "document_key", "TEXT NOT NULL DEFAULT ''"),
        ("documents", "scope_id", "TEXT NOT NULL DEFAULT 'default'"),
        ("chunks", "content_hash", "TEXT NOT NULL DEFAULT ''"),
        # 邮件草稿版本：审批必须绑定到具体版本，旧版本审批不得发送新内容
        ("emails", "version", "INTEGER NOT NULL DEFAULT 1"),
        ("emails", "draft_hash", "TEXT NOT NULL DEFAULT ''"),
        ("emails", "workflow_id", "TEXT NOT NULL DEFAULT ''"),
    )

    def _migrate(self) -> None:
        """轻量迁移：补上 Hybrid RAG 需要的稳定标识列。

        现有库（含正式 `.aieb/`）里已存在表结构，直接 ALTER 补列即可，
        不重建表、不搬数据，失败也只影响新能力的元数据，不影响原业务。
        """
        for table, column, ddl in self._ADDED_COLUMNS:
            existing = {
                row["name"]
                for row in self._conn.execute(f"PRAGMA table_info({table})")
            }
            if column in existing:
                continue
            try:
                self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
            except sqlite3.OperationalError:  # pragma: no cover - 并发建库冲突
                pass
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_documents_key ON documents(document_key)"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_chunks_doc_hash ON chunks(doc_id, content_hash)"
        )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------
    # 通用
    # ------------------------------------------------------------------
    def _exec(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, tuple(params))
            self._conn.commit()
            return cur

    def _query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(sql, tuple(params)))

    def add_activity(self, kind: str, title: str, detail: str = "") -> dict[str, Any]:
        item = {
            "id": new_id("act"),
            "kind": kind,
            "title": title,
            "detail": detail,
            "created_at": _now(),
        }
        self._exec(
            "INSERT INTO activities (id, kind, title, detail, created_at) VALUES (?,?,?,?,?)",
            (item["id"], kind, title, detail, item["created_at"]),
        )
        return item

    def list_activities(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self._query(
            "SELECT * FROM activities ORDER BY created_at DESC LIMIT ?", (limit,)
        )
        return [dict(r) for r in rows]

    def delete_activity(self, activity_id: str) -> bool:
        """删除单条活动记录；返回是否确实删掉了。"""
        cur = self._exec("DELETE FROM activities WHERE id=?", (activity_id,))
        return cur.rowcount > 0

    def clear_activities(self) -> int:
        """清空全部活动记录；返回删除条数。"""
        cur = self._exec("DELETE FROM activities")
        return cur.rowcount

    def delete_activities_by_titles(self, titles: Iterable[str]) -> int:
        """按标题精确匹配批量删除活动记录（用于业务对象被删除时级联清理）。"""
        items = [t for t in titles if t]
        if not items:
            return 0
        marks = ",".join("?" for _ in items)
        cur = self._exec(f"DELETE FROM activities WHERE title IN ({marks})", items)
        return cur.rowcount

    # ------------------------------------------------------------------
    # 知识库（文档 + 分块）
    # ------------------------------------------------------------------
    def add_document(
        self,
        *,
        name: str,
        ext: str,
        size: int,
        stored_path: str,
        status: str = "parsing",
        pages: int = 0,
        chars: int = 0,
        error: str = "",
        document_key: str = "",
        scope_id: str = "default",
    ) -> dict[str, Any]:
        doc = {
            "id": new_id("doc"),
            "name": name,
            "ext": ext,
            "size": int(size),
            "pages": int(pages),
            "chars": int(chars),
            "status": status,
            "error": error,
            "stored_path": stored_path,
            "document_key": document_key,
            "scope_id": scope_id or "default",
            "created_at": _now(),
        }
        self._exec(
            "INSERT INTO documents"
            " (id,name,ext,size,pages,chars,status,error,stored_path,document_key,scope_id,created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                doc["id"], doc["name"], doc["ext"], doc["size"], doc["pages"],
                doc["chars"], doc["status"], doc["error"], doc["stored_path"],
                doc["document_key"], doc["scope_id"], doc["created_at"],
            ),
        )
        return doc

    def find_document_by_key(self, document_key: str) -> dict[str, Any] | None:
        """按文件内容哈希查已入库文档 —— 重复上传的判定依据。"""
        if not document_key:
            return None
        rows = self._query(
            "SELECT * FROM documents WHERE document_key=?"
            " AND status IN ('ready','partial','no_text') ORDER BY created_at LIMIT 1",
            (document_key,),
        )
        return dict(rows[0]) if rows else None

    def document_ids_in_scope(self, scope_id: str) -> set[str]:
        """某作用域下可检索的文档 ID 集合。

        BM25 是纯内存索引、没有 payload 过滤的能力，因此作用域隔离必须在
        应用层把"允许的 doc_id"算出来再传进去 —— 否则"混合检索做了隔离、
        BM25 路没做"就会出现跨租户泄漏。
        """
        rows = self._query(
            "SELECT id FROM documents WHERE scope_id=? AND status IN ('ready','partial')",
            (scope_id or "default",),
        )
        return {str(r["id"]) for r in rows}

    def update_document(self, doc_id: str, **fields: Any) -> None:
        if not fields:
            return
        allowed = {"status", "error", "pages", "chars", "name"}
        items = {k: v for k, v in fields.items() if k in allowed}
        if not items:
            return
        assignments = ", ".join(f"{k}=?" for k in items)
        self._exec(
            f"UPDATE documents SET {assignments} WHERE id=?",
            (*items.values(), doc_id),
        )

    def add_chunks(self, doc_id: str, chunks: list[dict[str, Any]]) -> None:
        with self._lock:
            self._conn.executemany(
                "INSERT INTO chunks (id, doc_id, page, idx, text, content_hash)"
                " VALUES (?,?,?,?,?,?)",
                [
                    (
                        new_id("chk"),
                        doc_id,
                        int(c.get("page", 0)),
                        int(c.get("idx", i)),
                        str(c.get("text", "")),
                        str(c.get("content_hash") or content_hash(str(c.get("text", "")))),
                    )
                    for i, c in enumerate(chunks)
                ],
            )
            self._conn.commit()

    def list_documents(self) -> list[dict[str, Any]]:
        rows = self._query("SELECT * FROM documents ORDER BY created_at DESC")
        out = []
        for r in rows:
            item = dict(r)
            item.pop("stored_path", None)
            out.append(item)
        return out

    def get_document(self, doc_id: str) -> dict[str, Any] | None:
        rows = self._query("SELECT * FROM documents WHERE id=?", (doc_id,))
        return dict(rows[0]) if rows else None

    def delete_document(self, doc_id: str) -> bool:
        """删除文档及其分块（级联），并返回是否确实删除了记录。"""
        with self._lock:
            cur = self._conn.execute("DELETE FROM documents WHERE id=?", (doc_id,))
            self._conn.commit()
            return cur.rowcount > 0

    def all_chunks(self) -> list[dict[str, Any]]:
        """所有分块 + 文档元数据（供 BM25 索引与向量索引重建）。"""
        rows = self._query(
            "SELECT c.id, c.doc_id, c.page, c.idx, c.text, c.content_hash,"
            " d.name AS doc_name, d.document_key, d.scope_id, d.created_at AS doc_created_at"
            " FROM chunks c JOIN documents d ON d.id = c.doc_id"
            " WHERE d.status IN ('ready','partial')"
        )
        return [dict(r) for r in rows]

    def chunks_of(self, doc_id: str) -> list[dict[str, Any]]:
        rows = self._query(
            "SELECT id, page, idx, text FROM chunks WHERE doc_id=? ORDER BY idx",
            (doc_id,),
        )
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # 会议 / 风险 / 待办
    # ------------------------------------------------------------------
    def add_meeting(
        self,
        *,
        title: str,
        meeting_time: str = "",
        location: str = "",
        participants: list[str] | None = None,
        summary: str = "",
        decisions: list[str] | None = None,
        discussion: list[str] | None = None,
        source_name: str = "",
        raw_text: str = "",
        risks: list[dict[str, Any]] | None = None,
        todos: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        meeting_id = new_id("mtg")
        created = _now()
        with self._lock:
            self._conn.execute(
                "INSERT INTO meetings (id,title,meeting_time,location,participants,summary,"
                "decisions,discussion,source_name,raw_text,created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    meeting_id, title, meeting_time, location,
                    json.dumps(participants or [], ensure_ascii=False),
                    summary,
                    json.dumps(decisions or [], ensure_ascii=False),
                    json.dumps(discussion or [], ensure_ascii=False),
                    source_name, raw_text, created,
                ),
            )
            risk_ids: list[str] = []
            for risk in risks or []:
                risk_id = new_id("risk")
                self._conn.execute(
                    "INSERT INTO risks (id,meeting_id,title,level,description,evidence,"
                    "suggestion,status,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        risk_id, meeting_id, risk.get("title", ""), risk.get("level", "medium"),
                        risk.get("description", ""), risk.get("evidence", ""),
                        risk.get("suggestion", ""), "open", created,
                    ),
                )
                risk_ids.append(risk_id)
            for todo in todos or []:
                self._conn.execute(
                    "INSERT INTO todos (id,meeting_id,risk_id,content,owner,due_date,priority,"
                    "status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        new_id("todo"), meeting_id, None,
                        todo.get("content", ""), todo.get("owner", ""),
                        todo.get("due_date", ""), todo.get("priority", "medium"),
                        todo.get("status", "pending"), created, created,
                    ),
                )
            self._conn.commit()
        return self.get_meeting(meeting_id) or {}

    def list_meetings(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._query(
            "SELECT m.*, "
            " (SELECT COUNT(*) FROM risks r WHERE r.meeting_id=m.id) AS risk_count,"
            " (SELECT COUNT(*) FROM todos t WHERE t.meeting_id=m.id) AS todo_count"
            " FROM meetings m ORDER BY m.created_at DESC LIMIT ?",
            (limit,),
        )
        return [self._meeting_row(r) for r in rows]

    def list_meeting_texts(self) -> list[dict[str, str]]:
        """返回 [(id, raw_text)]。

        ``get_meeting``/``list_meetings`` 会主动剔除 ``raw_text``（界面不需要），
        所以按原文去重必须单独走这个接口，否则拿到永远是空串。
        """
        rows = self._query("SELECT id, raw_text FROM meetings ORDER BY created_at")
        return [{"id": r["id"], "raw_text": r["raw_text"] or ""} for r in rows]

    def get_meeting_raw_text(self, meeting_id: str) -> str:
        """取单场会议的原始记录文本（``get_meeting`` 会剔除该字段）。"""
        rows = self._query("SELECT raw_text FROM meetings WHERE id=?", (meeting_id,))
        if not rows:
            return ""
        return rows[0]["raw_text"] or ""

    def find_meeting_by_text(self, raw_text: str) -> dict[str, Any] | None:
        """按原始文本找已分析过的会议（用于重复上传时复用，而不是再存一份）。

        归一化后再比较：去首尾空白、把连续空白压成一个，避免同一份记录因为
        多一个换行就被当成新会议。
        """
        normalized = _normalize_meeting_text(raw_text)
        if not normalized:
            return None
        for row in self.list_meeting_texts():
            if _normalize_meeting_text(row["raw_text"]) == normalized:
                return self.get_meeting(row["id"])
        return None

    @staticmethod
    def _meeting_row(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["participants"] = json.loads(item.get("participants") or "[]")
        item["decisions"] = json.loads(item.get("decisions") or "[]")
        item["discussion"] = json.loads(item.get("discussion") or "[]")
        item.pop("raw_text", None)
        return item

    def get_meeting(self, meeting_id: str) -> dict[str, Any] | None:
        rows = self._query("SELECT * FROM meetings WHERE id=?", (meeting_id,))
        if not rows:
            return None
        meeting = self._meeting_row(rows[0])
        risk_rows = self._query(
            "SELECT * FROM risks WHERE meeting_id=? ORDER BY"
            " CASE level WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END, created_at",
            (meeting_id,),
        )
        meeting["risks"] = [dict(r) for r in risk_rows]
        todo_rows = self._query(
            "SELECT * FROM todos WHERE meeting_id=? ORDER BY created_at", (meeting_id,)
        )
        meeting["todos"] = [dict(r) for r in todo_rows]
        return meeting

    def delete_meeting(self, meeting_id: str) -> bool:
        """删除会议，并同步清理它的风险、待办与记忆事实。

        只删风险/待办会留下指向已删除会议的“幽灵记忆”，日后的问答会把
        不存在的会议当作事实引用。因此这里必须一起清理。
        """
        with self._lock:
            cur = self._conn.execute("DELETE FROM meetings WHERE id=?", (meeting_id,))
            if cur.rowcount > 0:
                self._conn.execute(
                    "DELETE FROM memory_facts WHERE source_kind='meeting' AND source_id=?",
                    (meeting_id,),
                )
            self._conn.commit()
            return cur.rowcount > 0

    def list_todos(
        self,
        *,
        status: str | None = None,
        priority: str | None = None,
    ) -> list[dict[str, Any]]:
        sql = (
            "SELECT t.*, m.title AS meeting_title, r.title AS risk_title, r.level AS risk_level"
            " FROM todos t"
            " LEFT JOIN meetings m ON m.id = t.meeting_id"
            " LEFT JOIN risks r ON r.id = t.risk_id"
        )
        clauses: list[str] = []
        params: list[Any] = []
        if status and status != "all":
            clauses.append("t.status=?")
            params.append(status)
        if priority and priority != "all":
            clauses.append("t.priority=?")
            params.append(priority)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY CASE t.status WHEN 'pending' THEN 0 WHEN 'doing' THEN 1 ELSE 2 END," \
               " CASE t.priority WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END," \
               " t.created_at DESC"
        return [dict(r) for r in self._query(sql, params)]

    def get_todo(self, todo_id: str) -> dict[str, Any] | None:
        rows = self._query(
            "SELECT t.*, m.title AS meeting_title FROM todos t"
            " LEFT JOIN meetings m ON m.id = t.meeting_id WHERE t.id=?",
            (todo_id,),
        )
        return dict(rows[0]) if rows else None

    def update_todo(self, todo_id: str, **fields: Any) -> dict[str, Any] | None:
        allowed = {"content", "owner", "due_date", "priority", "status"}
        items = {k: v for k, v in fields.items() if k in allowed and v is not None}
        if items:
            assignments = ", ".join(f"{k}=?" for k in items)
            self._exec(
                f"UPDATE todos SET {assignments}, updated_at=? WHERE id=?",
                (*items.values(), _now(), todo_id),
            )
        return self.get_todo(todo_id)

    def add_todo(self, **fields: Any) -> dict[str, Any]:
        created = _now()
        todo_id = new_id("todo")
        self._exec(
            "INSERT INTO todos (id,meeting_id,risk_id,content,owner,due_date,priority,status,"
            "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                todo_id, fields.get("meeting_id"), fields.get("risk_id"),
                fields.get("content", ""), fields.get("owner", ""),
                fields.get("due_date", ""), fields.get("priority", "medium"),
                fields.get("status", "pending"), created, created,
            ),
        )
        return self.get_todo(todo_id) or {}

    def delete_todo(self, todo_id: str) -> bool:
        cur = self._exec("DELETE FROM todos WHERE id=?", (todo_id,))
        return cur.rowcount > 0

    def delete_todos(self, todo_ids: Iterable[str]) -> int:
        """批量删除待办；返回实际删除条数。"""
        items = [t for t in todo_ids if t]
        if not items:
            return 0
        marks = ",".join("?" for _ in items)
        cur = self._exec(f"DELETE FROM todos WHERE id IN ({marks})", items)
        return cur.rowcount

    def list_risks(self, status: str | None = None) -> list[dict[str, Any]]:
        sql = (
            "SELECT r.*, m.title AS meeting_title FROM risks r"
            " LEFT JOIN meetings m ON m.id = r.meeting_id"
        )
        params: list[Any] = []
        if status:
            sql += " WHERE r.status=?"
            params.append(status)
        sql += " ORDER BY CASE r.level WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END, r.created_at DESC"
        return [dict(r) for r in self._query(sql, params)]

    def update_risk_status(self, risk_id: str, status: str) -> bool:
        cur = self._exec("UPDATE risks SET status=? WHERE id=?", (status, risk_id))
        return cur.rowcount > 0

    # ------------------------------------------------------------------
    # 邮件
    # ------------------------------------------------------------------
    def create_email(
        self, *, to_addr: str, subject: str, body: str,
        meeting_id: str | None = None, kind: str = "reminder",
        version: int = 1, draft_hash: str = "", workflow_id: str = "",
    ) -> dict[str, Any]:
        email_id = new_id("mail")
        created = _now()
        self._exec(
            "INSERT INTO emails (id,meeting_id,kind,to_addr,subject,body,status,error,"
            "created_at,sent_at,version,draft_hash,workflow_id)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                email_id, meeting_id, kind, to_addr, subject, body, "draft", "",
                created, "", int(version), draft_hash, workflow_id,
            ),
        )
        return self.get_email(email_id) or {}

    def get_email(self, email_id: str) -> dict[str, Any] | None:
        rows = self._query("SELECT * FROM emails WHERE id=?", (email_id,))
        return dict(rows[0]) if rows else None

    def update_email(self, email_id: str, **fields: Any) -> dict[str, Any] | None:
        allowed = {
            "to_addr", "subject", "body", "status", "error", "sent_at",
            "version", "draft_hash", "workflow_id",
        }
        items = {k: v for k, v in fields.items() if k in allowed and v is not None}
        if items:
            assignments = ", ".join(f"{k}=?" for k in items)
            self._exec(f"UPDATE emails SET {assignments} WHERE id=?", (*items.values(), email_id))
        return self.get_email(email_id)

    def list_emails(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._query("SELECT * FROM emails ORDER BY created_at DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # 结构化记忆事实（人物/时间/地点/决策/待办）
    # ------------------------------------------------------------------
    def add_memory_facts(self, facts: list[dict[str, Any]]) -> int:
        """写入结构化记忆事实。

        同一来源（如同一会议）里完全相同的条目只保留一条：重复分析同一份
        会议记录不应让记忆页出现成倍的重复卡片。
        """
        created = _now()
        count = 0
        with self._lock:
            for fact in facts:
                content = (fact.get("content") or "").strip()
                if not content:
                    continue
                source_id = fact.get("source_id", "")
                category = fact.get("category", "other")
                if source_id:
                    exists = self._conn.execute(
                        "SELECT 1 FROM memory_facts WHERE source_id=? AND category=? AND content=?",
                        (source_id, category, content),
                    ).fetchone()
                    if exists:
                        continue
                self._conn.execute(
                    "INSERT INTO memory_facts (id,category,content,source_kind,source_id,"
                    "source_label,created_at) VALUES (?,?,?,?,?,?,?)",
                    (
                        new_id("fact"), category, content,
                        fact.get("source_kind", ""), source_id,
                        fact.get("source_label", ""), created,
                    ),
                )
                count += 1
            self._conn.commit()
        return count

    def list_memory_facts(self) -> list[dict[str, Any]]:
        rows = self._query(
            "SELECT * FROM memory_facts ORDER BY category, created_at DESC"
        )
        return [dict(r) for r in rows]

    def delete_memory_fact(self, fact_id: str) -> bool:
        cur = self._exec("DELETE FROM memory_facts WHERE id=?", (fact_id,))
        return cur.rowcount > 0

    def delete_memory_facts(self, fact_ids: Iterable[str]) -> int:
        """批量删除结构化记忆事实；返回实际删除条数。"""
        items = [f for f in fact_ids if f]
        if not items:
            return 0
        marks = ",".join("?" for _ in items)
        cur = self._exec(f"DELETE FROM memory_facts WHERE id IN ({marks})", items)
        return cur.rowcount

    # ------------------------------------------------------------------
    # 工作台统计
    # ------------------------------------------------------------------
    def stats(self) -> dict[str, Any]:
        def scalar(sql: str, params: Iterable[Any] = ()) -> int:
            rows = self._query(sql, params)
            return int(rows[0][0]) if rows else 0

        return {
            "documents": scalar("SELECT COUNT(*) FROM documents"),
            "documents_ready": scalar(
                "SELECT COUNT(*) FROM documents WHERE status IN ('ready','partial')"
            ),
            "documents_failed": scalar("SELECT COUNT(*) FROM documents WHERE status='failed'"),
            "meetings": scalar("SELECT COUNT(*) FROM meetings"),
            "todos_open": scalar(
                "SELECT COUNT(*) FROM todos WHERE status IN ('pending','doing')"
            ),
            "todos_done": scalar("SELECT COUNT(*) FROM todos WHERE status='done'"),
            "risks_open": scalar("SELECT COUNT(*) FROM risks WHERE status='open'"),
            "risks_high": scalar(
                "SELECT COUNT(*) FROM risks WHERE status='open' AND level='high'"
            ),
        }
