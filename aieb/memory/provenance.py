"""记忆来源追踪（provenance）。

记忆文件由 Agent 写到磁盘，本身不带会话信息。为了让"删除对话"能精准清掉该会话
专属的记忆，这里维护旁路索引 ``sources.json``。

## 记录方式（v2，写入时显式记录）

以文件名为键，记录 ``sessions`` / ``source`` / ``updated_at`` / ``digest``：

* **主路径：写入时显式记录**
  - ``MemoryManager.extract()`` → ``source=extract``
  - 文件工具写入受管记忆目录 → ``source=tool``（由 Agent 在工具执行后登记）
  - 用户手动删除/清理 → ``source=user``
* **辅助路径：mtime 快照**（``source=snapshot``）
  只用于"本轮新建、且完全没有来源记录"的文件（例如通过 Bash 写文件），
  已有显式来源的记录不会被它降级。

一条记忆可以有多个来源会话；只有最后一个来源会话被删除时才删文件。

## 并发与一致性

* 进程内 ``threading.RLock`` 保护"读-改-写"；
* ``sources.json`` 用临时文件 + ``os.replace`` 原子替换；
* 墓碑 ``.purged-sessions.json``：会话删除时先写墓碑，正在跑的记忆提取在落盘前
  检查墓碑，避免"删完又被写回来"。

## 兼容性

旧版 ``sources.json``（v1：``{"name": ["s1"]}`` 或 ``{"name": {"sessions": [...]}}``）
读取时统一归一化；没有来源记录的记忆保持 unknown，不猜测、不自动删除。
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

SOURCES_FILE = "sources.json"
TOMBSTONE_FILE = ".purged-sessions.json"
INDEX_NAME = "MEMORY.md"
SETTINGS_FILE = "settings.json"
_RESERVED = {INDEX_NAME, SOURCES_FILE, TOMBSTONE_FILE, SETTINGS_FILE}

SOURCE_EXTRACT = "extract"
SOURCE_TOOL = "tool"
SOURCE_USER = "user"
SOURCE_SNAPSHOT = "snapshot"
SOURCE_UNKNOWN = "unknown"
# 通过受控记忆接口（SaveMemory）显式保存。与 SOURCE_TOOL 区分开，
# 便于在 Web 面板里区分"模型主动记住"与"通用文件工具顺手写入"。
SOURCE_SAVE = "save"

_LOCK = threading.RLock()


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------


def content_digest(text: str) -> str:
    return "sha1:" + hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:16]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_write(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(payload, encoding="utf-8")
    os.replace(tmp, path)


def write_text_atomic(path: Path | str, payload: str) -> None:
    """原子写入文本（记忆文件也走这条路径，避免读到半个文件）。"""
    _atomic_write(Path(path), payload)


# ---------------------------------------------------------------------------
# 目录快照
# ---------------------------------------------------------------------------


def snapshot(dir_path: Path | str) -> dict[str, int]:
    """记录目录内记忆文件的 mtime（只关心 *.md，排除索引与旁路文件）。"""
    result: dict[str, int] = {}
    path = Path(dir_path)
    if not path.is_dir():
        return result
    try:
        entries = list(path.iterdir())
    except OSError:
        return result
    for entry in entries:
        if entry.name in _RESERVED or entry.suffix != ".md" or not entry.is_file():
            continue
        try:
            result[entry.name] = entry.stat().st_mtime_ns
        except OSError:
            continue
    return result


# ---------------------------------------------------------------------------
# 索引读写
# ---------------------------------------------------------------------------


def load_entries(dir_path: Path | str) -> dict[str, dict]:
    """读取来源索引，兼容 v1/v2 两种结构。"""
    path = Path(dir_path) / SOURCES_FILE
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    entries = raw.get("entries")
    if not isinstance(entries, dict):
        entries = {k: v for k, v in raw.items() if k not in {"version", "updated_at"}}
    result: dict[str, dict] = {}
    for name, item in entries.items():
        if isinstance(item, list):
            result[str(name)] = {
                "sessions": [str(s) for s in item if s],
                "source": SOURCE_UNKNOWN,
                "updated_at": "",
                "digest": "",
                "detached": [],
            }
        elif isinstance(item, dict):
            sessions = item.get("sessions") or []
            detached = item.get("detached") or []
            result[str(name)] = {
                "sessions": [str(s) for s in sessions if s] if isinstance(sessions, list) else [],
                "source": str(item.get("source") or SOURCE_UNKNOWN),
                "updated_at": str(item.get("updated_at") or ""),
                "digest": str(item.get("digest") or ""),
                "detached": _normalize_detached(detached),
            }
    return result


def _normalize_detached(raw: object) -> list[dict]:
    """归一化 detached 列表：``[{"id":..., "title":..., "at":...}]``。"""
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for item in raw:
        if isinstance(item, dict) and item.get("id"):
            out.append({
                "id": str(item["id"]),
                "title": str(item.get("title") or ""),
                "at": str(item.get("at") or ""),
            })
        elif isinstance(item, str) and item:
            out.append({"id": item, "title": "", "at": ""})
    return out


def save_entries(dir_path: Path | str, entries: dict[str, dict]) -> None:
    path = Path(dir_path) / SOURCES_FILE
    payload = {
        "version": 2,
        "updated_at": _now(),
        "entries": {
            name: {
                "sessions": sorted({str(s) for s in (item.get("sessions") or []) if s}),
                "source": item.get("source") or SOURCE_UNKNOWN,
                "updated_at": item.get("updated_at") or "",
                "digest": item.get("digest") or "",
                "detached": _normalize_detached(item.get("detached")),
            }
            for name, item in sorted(entries.items())
            if (item.get("sessions") or item.get("source") or item.get("detached"))
        },
    }
    _atomic_write(path, json.dumps(payload, ensure_ascii=False, indent=2))


# 兼容旧接口 ---------------------------------------------------------------


def load_sources(dir_path: Path | str) -> dict[str, list[str]]:
    return {name: entry["sessions"] for name, entry in load_entries(dir_path).items()}


def save_sources(dir_path: Path | str, sources: dict[str, object]) -> None:
    """兼容旧接口：值可以是会话列表，也可以是完整 entry 字典。"""
    entries: dict[str, dict] = {}
    for name, value in sources.items():
        if isinstance(value, dict):
            entries[str(name)] = value
        else:
            entries[str(name)] = {
                "sessions": [str(s) for s in value],  # type: ignore[union-attr]
                "source": SOURCE_UNKNOWN,
                "updated_at": "",
                "digest": "",
            }
    save_entries(dir_path, entries)


def sessions_of(dir_path: Path | str, name: str) -> list[str]:
    return load_entries(dir_path).get(name, {}).get("sessions", [])


def source_of(dir_path: Path | str, name: str) -> str:
    return load_entries(dir_path).get(name, {}).get("source", SOURCE_UNKNOWN)


# ---------------------------------------------------------------------------
# 写入时显式记录（主路径）
# ---------------------------------------------------------------------------


def record_write(
    dir_path: Path | str,
    name: str,
    *,
    session_id: str | None,
    source: str = SOURCE_TOOL,
    digest: str | None = None,
) -> bool:
    """登记一次记忆写入。返回 False 表示目录不存在或文件名非法。"""
    path = Path(dir_path)
    if not path.is_dir() or not name.endswith(".md") or name in _RESERVED:
        return False
    with _LOCK:
        entries = load_entries(path)
        entry = entries.setdefault(
            name, {"sessions": [], "source": SOURCE_UNKNOWN, "updated_at": "", "digest": ""}
        )
        if session_id:
            entry["sessions"] = sorted({*(entry.get("sessions") or []), session_id})
        # 显式来源优先，快照不能覆盖显式
        if source != SOURCE_SNAPSHOT or entry.get("source") in (SOURCE_UNKNOWN, SOURCE_SNAPSHOT):
            entry["source"] = source
        entry["updated_at"] = _now()
        if digest:
            entry["digest"] = digest
        entries[name] = entry
        try:
            save_entries(path, entries)
        except OSError:
            return False
    return True


def record_changes(
    dir_path: Path | str, before: dict[str, int], session_id: str | None
) -> list[str]:
    """mtime 快照兜底：只给"本轮新建且无显式来源"的文件打 snapshot 标记。"""
    after = snapshot(dir_path)
    changed = [name for name, mtime in after.items() if before.get(name) != mtime]
    if not changed:
        return []
    touched: list[str] = []
    with _LOCK:
        entries = load_entries(dir_path)
        for name in changed:
            existing = entries.get(name)
            if existing and existing.get("source") not in (SOURCE_UNKNOWN, SOURCE_SNAPSHOT):
                continue  # 已有显式来源，不降级
            digest = ""
            try:
                digest = content_digest((Path(dir_path) / name).read_text(encoding="utf-8"))
            except OSError:
                pass
            sessions = set((existing or {}).get("sessions") or [])
            if session_id:
                sessions.add(session_id)
            entries[name] = {
                "sessions": sorted(sessions),
                "source": SOURCE_SNAPSHOT,
                "updated_at": _now(),
                "digest": digest,
            }
            touched.append(name)
        if touched:
            try:
                save_entries(dir_path, entries)
            except OSError:
                return []
    return touched


# ---------------------------------------------------------------------------
# 墓碑：防止正在跑的记忆提取把已删除会话的记忆写回来
# ---------------------------------------------------------------------------


def _load_tombstones(dir_path: Path | str) -> set[str]:
    path = Path(dir_path) / TOMBSTONE_FILE
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return {str(s) for s in raw.get("sessions", [])}
    except (OSError, json.JSONDecodeError):
        return set()


def mark_purged(dir_path: Path | str, session_id: str) -> None:
    path = Path(dir_path) / TOMBSTONE_FILE
    sessions = _load_tombstones(dir_path)
    sessions.add(session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"sessions": sorted(sessions)}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def is_purged(dir_path: Path | str, session_id: str | None) -> bool:
    if not session_id:
        return False
    return session_id in _load_tombstones(dir_path)


# ---------------------------------------------------------------------------
# 索引与单条记忆维护
# ---------------------------------------------------------------------------


def delete_memory_file(dir_path: Path | str, name: str, *, source: str = SOURCE_USER) -> bool:
    """删除单条记忆，同时清理 MEMORY.md 索引行与来源记录。"""
    path = Path(dir_path) / name
    removed = False
    try:
        if path.is_file():
            path.unlink()
            removed = True
    except OSError:
        return False
    with _LOCK:
        entries = load_entries(dir_path)
        if entries.pop(name, None) is not None:
            try:
                save_entries(dir_path, entries)
            except OSError:
                pass
    rewrite_index(dir_path)
    return removed


def rewrite_index(dir_path: Path | str) -> int:
    """移除 MEMORY.md 中指向已不存在文件的索引行，返回移除行数。"""
    path = Path(dir_path) / INDEX_NAME
    if not path.is_file():
        return 0
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return 0

    kept: list[str] = []
    removed = 0
    for line in lines:
        target = _index_target(line)
        if target and not (Path(dir_path) / target).is_file():
            removed += 1
            continue
        kept.append(line)

    if removed:
        try:
            _atomic_write(path, "\n".join(kept) + ("\n" if kept else ""))
        except OSError:
            return 0
    return removed


def _index_target(line: str) -> str | None:
    """从 ``- [标题](文件.md) — 描述`` 中取出文件名。"""
    start = line.find("](")
    if not line.lstrip().startswith(("-", "*")) or start < 0:
        return None
    end = line.find(")", start + 2)
    if end < 0:
        return None
    target = line[start + 2:end].strip()
    return target or None


# ---------------------------------------------------------------------------
# 按会话清理
# ---------------------------------------------------------------------------


@dataclass
class PruneResult:
    deleted: list[str] = field(default_factory=list)
    kept_shared: list[str] = field(default_factory=list)
    # 来源会话早已删除、但用户当时选择保留的记忆（不能当成 untracked 删）
    kept_detached: list[str] = field(default_factory=list)
    untracked: list[str] = field(default_factory=list)
    index_lines_removed: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def removed_count(self) -> int:
        return len(self.deleted)


def prune_session(
    dir_path: Path | str, session_id: str, *, delete_untracked: bool = False
) -> PruneResult:
    """从记忆目录里移除该会话的贡献。

    * 只有这一个来源的记忆 → 删除文件 + 清索引行；
    * 还有其它来源 → 保留文件，只把该会话从来源列表里摘掉；
    * 完全没有来源记录的旧记忆 → 默认保留（``untracked``），除非显式要求删除。
    """
    result = PruneResult()
    path = Path(dir_path)
    if not path.is_dir():
        return result

    with _LOCK:
        entries = load_entries(path)
        current = snapshot(path)

        for name in sorted(current):
            entry = entries.get(name)
            if not entry or not entry.get("sessions"):
                # 用户主动选择保留的（来源会话已删）与「来源未知的历史记忆」是两回事：
                # 前者是被有意留下的，不能跟着 untracked 一起删。
                if entry and entry.get("detached"):
                    result.kept_detached.append(name)
                    continue
                if delete_untracked:
                    if _unlink(path / name, result):
                        result.deleted.append(name)
                        entries.pop(name, None)
                else:
                    result.untracked.append(name)
                continue
            sessions = entry.get("sessions") or []
            if session_id not in sessions:
                continue
            remaining = [s for s in sessions if s != session_id]
            if remaining:
                entry["sessions"] = remaining
                entry["updated_at"] = _now()
                entries[name] = entry
                result.kept_shared.append(name)
            else:
                entries.pop(name, None)
                if _unlink(path / name, result):
                    result.deleted.append(name)

        # 清掉指向不存在文件的历史条目
        for name in list(entries):
            if not (path / name).is_file():
                entries.pop(name, None)
        try:
            save_entries(path, entries)
        except OSError as exc:
            result.errors.append(f"写入记忆来源索引失败: {exc}")

    result.index_lines_removed = rewrite_index(path)
    return result


# ---------------------------------------------------------------------------
# 保留记忆：会话被删了，但用户选择保留它产生的记忆
# ---------------------------------------------------------------------------


@dataclass
class DetachResult:
    """把某个会话从记忆来源里摘掉的统计（不删文件）。"""

    detached: list[str] = field(default_factory=list)
    kept_shared: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def detach_session(
    dir_path: Path | str, session_id: str, *, title: str = ""
) -> DetachResult:
    """会话被删除但记忆要保留：把该会话从 ``sessions`` 移到 ``detached``。

    为什么要动元数据：否则 UI 只能笼统显示「来自一个已删除的对话」，
    用户不知道这条记忆是"被有意保留"还是"清理漏了"。搬到 ``detached``
    之后：

    * 该会话不再参与后续的独占判定（不会因为再删一次而被连带删除）；
    * UI 能显示「来自已删除的对话「标题」（你选择保留）」；
    * 文件本身完全不动（满足"选择保留"的语义）。
    """
    result = DetachResult()
    path = Path(dir_path)
    if not path.is_dir():
        return result

    with _LOCK:
        entries = load_entries(path)
        changed = False
        for name, entry in entries.items():
            sessions = entry.get("sessions") or []
            if session_id not in sessions:
                continue
            remaining = [s for s in sessions if s != session_id]
            detached = _normalize_detached(entry.get("detached"))
            if not any(item["id"] == session_id for item in detached):
                detached.append({"id": session_id, "title": title, "at": _now()})
            entry["sessions"] = remaining
            entry["detached"] = detached
            entry["updated_at"] = _now()
            entries[name] = entry
            changed = True
            if remaining:
                result.kept_shared.append(name)
            else:
                result.detached.append(name)
        if changed:
            try:
                save_entries(path, entries)
            except OSError as exc:
                result.errors.append(f"写入记忆来源索引失败: {exc}")
    return result


def _unlink(target: Path, result: PruneResult) -> bool:
    try:
        target.unlink()
        return True
    except FileNotFoundError:
        return True
    except OSError as exc:  # Windows 文件占用等
        result.errors.append(f"删除记忆文件失败 {target.name}: {exc}")
        return False


def purge_marker_dirs() -> list[str]:
    """预留给未来清理逻辑使用的目录列表（当前仅返回空列表）。"""
    return []


def source_summary(dir_path: Path | str, name: str) -> str:
    sessions = sessions_of(dir_path, name)
    if not sessions:
        return "来源未知（旧记忆）"
    if len(sessions) == 1:
        return f"来源会话 1 个"
    return f"来源会话 {len(sessions)} 个"


def dirs_for(work_dir: str, user_dir: str | None = None) -> list[Path]:
    """返回需要跟踪来源的记忆目录（项目级 + 用户级）。"""
    result = [Path(work_dir) / ".aieb" / "memory"]
    if user_dir:
        result.append(Path(user_dir))
    return [p for p in result if os.fspath(p)]
