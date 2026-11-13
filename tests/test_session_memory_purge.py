"""删除对话时的记忆联动测试（用户级 + 项目级 + 保留/共享/历史）。"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from aieb import session_purge  # noqa: E402
from aieb.memory import auto_memory, provenance  # noqa: E402
from aieb.memory.auto_memory import MemoryManager  # noqa: E402
from aieb.memory.session import SessionManager  # noqa: E402

WEB = (REPO_ROOT / "aieb" / "web_content.py").read_text(encoding="utf-8")
REMOTE = (REPO_ROOT / "aieb" / "remote.py").read_text(encoding="utf-8")

PREF = "用户喜欢自然风景，不喜欢太赶的行程。"


# ---------------------------------------------------------------- 辅助


@pytest.fixture
def env(tmp_path, monkeypatch):
    """隔离的工作区 + 隔离的用户级记忆目录。"""
    ws = tmp_path / "ws"
    ws.mkdir()
    user_mem = tmp_path / "user_mem"
    user_mem.mkdir()
    monkeypatch.setattr(
        auto_memory, "get_user_auto_mem_path", lambda: str(user_mem) + os.sep
    )
    manager = SessionManager(str(ws))
    return SimpleEnv(ws, user_mem, manager, tmp_path)


class SimpleEnv:
    def __init__(self, ws: Path, user_mem: Path, sessions, tmp_path: Path) -> None:
        self.ws = ws
        self.user_mem = user_mem
        self.sessions = sessions
        self.tmp_path = tmp_path
        self.memory = MemoryManager(str(ws))

    @property
    def project_mem(self) -> Path:
        return self.ws / ".aieb" / "memory"

    def new_session(self, title: str = "") -> str:
        session = self.sessions.create()
        session.close()
        if title:
            self.sessions.rename(session.session_id, title)
        return session.session_id

    def save(self, session_id: str, *, scope: str = "user", title: str = "travel-pref", content: str = PREF) -> str:
        result = self.memory.save_memory(
            content, scope=scope, title=title, session_id=session_id
        )
        assert result.ok, result.message
        return result.filename

    def purge(self, session_id: str, *, purge_memories: bool = True, title: str = "") -> session_purge.PurgeReport:
        return session_purge.purge_session(
            session_id,
            work_dir=self.ws,
            session_manager=self.sessions,
            user_memory_dir=self.user_mem,
            purge_memories=purge_memories,
            session_title=title,
        )


def labels(env: SimpleEnv, titles: dict[str, str] | None = None) -> dict[str, str]:
    """当前的「标题 → 来源文案」映射（模拟 Web 记忆面板）。"""
    items = env.memory.list_for_web(titles or {})
    return {item["title"]: item["source_label"] for item in items}


PLACEHOLDER = None


def memory_files(directory: Path) -> list[str]:
    if not directory.is_dir():
        return []
    return sorted(p.name for p in directory.glob("*.md") if p.name != "MEMORY.md")


# ---------------------------------------------------------------- 1. 删除 + 删记忆


def test_delete_session_removes_user_level_memory(env):
    """1. 勾选"同时删除…"时，用户级独占记忆必须真的被删掉。"""
    sid = env.new_session("旅行偏好")
    filename = env.save(sid, scope="user")
    assert (env.user_mem / filename).is_file()

    report = env.purge(sid, purge_memories=True)

    assert report.ok, report.errors
    assert not (env.user_mem / filename).exists()
    assert filename in report.memories_removed
    # sources.json / MEMORY.md 同步清理
    assert filename not in provenance.load_entries(env.user_mem)
    index = env.user_mem / "MEMORY.md"
    assert filename not in (index.read_text(encoding="utf-8") if index.exists() else "")


def test_delete_session_removes_project_level_memory(env):
    """1b. 项目级独占记忆同样被清掉。"""
    sid = env.new_session()
    filename = env.save(sid, scope="project", title="repo-convention")
    assert (env.project_mem / filename).is_file()

    report = env.purge(sid)

    assert report.ok, report.errors
    assert not (env.project_mem / filename).exists()
    assert filename in report.memories_removed


def test_new_session_does_not_read_deleted_memory(env):
    """4. 删除后新建会话不会再从长期记忆里读到它。"""
    sid = env.new_session()
    env.save(sid, scope="user")
    assert "自然风景" in env.memory.load()

    env.purge(sid, purge_memories=True)

    fresh = MemoryManager(str(env.ws))
    assert "自然风景" not in fresh.load()


# ---------------------------------------------------------------- 2. 删除 + 保留


def test_delete_session_but_keep_memory_marks_detached(env):
    """2. 取消勾选：记忆保留，来源改写成 detached，UI 说明"你选择保留"。"""
    sid = env.new_session("旅行偏好")
    filename = env.save(sid, scope="user")

    report = env.purge(sid, purge_memories=False, title="旅行偏好")

    assert report.ok, report.errors
    assert (env.user_mem / filename).is_file()
    assert filename in report.memories_detached

    entries = provenance.load_entries(env.user_mem)
    entry = entries[filename]
    assert entry["sessions"] == []
    assert entry["detached"][0]["id"] == sid
    assert entry["detached"][0]["title"] == "旅行偏好"

    label = next(
        i["source_label"] for i in env.memory.list_for_web({})
        if i["id"] == env.memory.memory_id("user", filename)
    )
    assert "你选择保留" in label
    assert "旅行偏好" in label


def test_kept_memory_is_not_reported_as_unknown(env):
    """保留（detached）的记忆不能被统计成「来源未知」，更不能被 untracked 清理带走。"""
    sid_a = env.new_session()
    filename = env.save(sid_a, scope="user", content="偏好 A")
    env.purge(sid_a, purge_memories=False, title="会话 A")

    sid_b = env.new_session()
    report = env.purge(sid_b, purge_memories=True)

    assert (env.user_mem / filename).is_file()
    assert filename in report.memories_kept_detached
    assert filename not in report.memories_untracked
    assert "来源未知" not in report.summary()

    # 即便有人显式要求清 untracked，也不应该把"选择保留"的记忆删掉
    aggressive = session_purge.purge_session(
        env.new_session(),
        work_dir=env.ws,
        session_manager=env.sessions,
        user_memory_dir=env.user_mem,
        delete_untracked_memories=True,
    )
    assert (env.user_mem / filename).is_file()
    assert filename not in aggressive.memories_removed


def test_kept_memory_is_still_usable_by_new_sessions(env):
    """保留的记忆仍会进入新会话的提示（这是用户选择保留的意义）。"""
    sid = env.new_session("旅行偏好")
    env.save(sid, scope="user")
    env.purge(sid, purge_memories=False, title="旅行偏好")

    assert "自然风景" in MemoryManager(str(env.ws)).load()


# ---------------------------------------------------------------- 3. 多会话共享


def test_shared_memory_survives_until_last_session_removed(env):
    """3. 被多个会话共用的记忆不误删；最后一个来源删除时才清掉。"""
    sid_a = env.new_session()
    sid_b = env.new_session()
    filename = env.save(sid_a, scope="user", content=PREF)
    # 同一内容被另一个会话再次"记住"→ 追加来源而非新建文件
    second = env.memory.save_memory(PREF, scope="user", title="travel-pref", session_id=sid_b)
    assert second.status == "duplicate"
    assert provenance.sessions_of(env.user_mem, filename) == sorted([sid_a, sid_b])

    report_a = env.purge(sid_a, purge_memories=True)
    assert report_a.ok, report_a.errors
    assert (env.user_mem / filename).is_file()
    assert filename in report_a.memories_kept_shared
    assert provenance.sessions_of(env.user_mem, filename) == [sid_b]

    report_b = env.purge(sid_b, purge_memories=True)
    assert report_b.ok, report_b.errors
    assert not (env.user_mem / filename).exists()


def test_purge_does_not_touch_other_sessions_memories(env):
    """9. 不影响其它会话的记忆、文件。"""
    sid_a = env.new_session()
    sid_b = env.new_session()
    file_a = env.save(sid_a, scope="user", title="pref-a", content="偏好 A")
    file_b = env.save(sid_b, scope="user", title="pref-b", content="偏好 B")

    report = env.purge(sid_a, purge_memories=True)

    assert report.ok, report.errors
    assert not (env.user_mem / file_a).exists()
    assert (env.user_mem / file_b).is_file()
    assert provenance.sessions_of(env.user_mem, file_b) == [sid_b]


# ---------------------------------------------------------------- 4. 历史未知


def test_untracked_legacy_memory_is_never_auto_deleted(env):
    """5. 来源无法确认的历史记忆：保守保留，并在报告里列出。"""
    env.user_mem.mkdir(parents=True, exist_ok=True)
    legacy = env.user_mem / "legacy-note.md"
    legacy.write_text("---\nname: legacy\ndescription: 旧记忆\ntype: user\n---\n\n旧内容\n", encoding="utf-8")
    sid = env.new_session()

    report = env.purge(sid, purge_memories=True)

    assert report.ok, report.errors
    assert legacy.is_file()
    assert "legacy-note.md" in report.memories_untracked


def test_legacy_memory_stays_unknown_not_detached(env):
    """没有来源记录 ≠ 已删除会话保留：文案必须是「历史记忆，来源未知」。"""
    env.user_mem.mkdir(parents=True, exist_ok=True)
    (env.user_mem / "legacy-note.md").write_text(
        "---\nname: legacy\ndescription: 旧记忆\ntype: user\n---\n\n旧内容\n", encoding="utf-8"
    )
    item = next(
        i for i in env.memory.list_for_web({}) if i["title"] == "legacy"
    )
    assert item["source_label"] == "历史记忆，来源未知"


# ---------------------------------------------------------------- 5. 一致性


def test_refresh_and_restart_show_consistent_state(env):
    """6. 刷新/重启（重新构造 SessionManager + MemoryManager）后状态一致。"""
    sid_a = env.new_session()
    sid_b = env.new_session()
    env.save(sid_a, scope="user", content="偏好 A")
    kept = env.save(sid_b, scope="user", title="pref-b", content="偏好 B")

    env.purge(sid_a, purge_memories=True)

    # 模拟服务重启：全部重新构造
    fresh_sessions = SessionManager(str(env.ws))
    fresh_memory = MemoryManager(str(env.ws))
    items = fresh_memory.list_for_web({m.id: m.title for m in fresh_sessions.list()})
    assert [i["title"] for i in items] == ["pref-b"]
    assert sid_a not in [m.id for m in fresh_sessions.list()]
    assert sid_b in [m.id for m in fresh_sessions.list()]


# ---------------------------------------------------------------- 6. 单条删除


def test_single_memory_delete_removes_file_and_indexes(env):
    """7. 记忆面板的单条删除：文件、sources.json、MEMORY.md 三处都清掉。"""
    sid = env.new_session()
    filename = env.save(sid, scope="user")
    mem_id = env.memory.memory_id("user", filename)

    ok, message = env.memory.delete_by_id(mem_id)

    assert ok is True
    assert "删除" in message
    assert not (env.user_mem / filename).exists()
    assert filename not in provenance.load_entries(env.user_mem)
    index = env.user_mem / "MEMORY.md"
    assert filename not in (index.read_text(encoding="utf-8") if index.exists() else "")
    assert env.memory.list_for_web({}) == []


def test_single_delete_rejects_path_like_id(env):
    """恶意 id 不能用来删任意文件。"""
    sid = env.new_session()
    env.save(sid, scope="user")
    for bad in ("../../../etc/passwd", "..\\..\\x", "mem_/absolute"):
        ok, _ = env.memory.delete_by_id(bad)
        assert ok is False
    assert memory_files(env.user_mem)


# ---------------------------------------------------------------- 7. Web / WS


def test_confirm_dialog_has_memory_checkbox_default_checked():
    """1. 删除确认框里有勾选项，默认勾选。"""
    assert 'id="confirm-option"' in WEB
    assert "同时删除这段对话产生的长期记忆" in WEB
    assert "checkboxChecked: true" in WEB
    # 文案不再声称"用户级长期记忆不会被删除"
    assert "用户级长期记忆（例如你的个人偏好）不会被删除" not in WEB


def test_delete_session_payload_carries_purge_flag():
    """2. 前端把勾选结果作为 purge_memories 发给后端。"""
    assert "purge_memories: res.checked !== false" in WEB
    assert 'data.get("purge_memories", True)' in REMOTE
    assert "purge_memories=purge_memories" in REMOTE


def test_show_confirm_returns_checkbox_state():
    """确认框解析出 {ok, checked}；取消仍然是 false。"""
    block = WEB[WEB.index("function showConfirm"):]
    block = block[: block.index("function closeConfirm")]
    assert "confirmOptionEl.checked = opts.checkboxChecked !== false" in block
    close = WEB[WEB.index("function closeConfirm"):]
    close = close[: close.index("confirmOkBtn.addEventListener")]
    assert "{ ok: true, checked: !!confirmOptionEl.checked }" in close
    assert "? { ok: true" in close


def test_remote_delete_passes_title_for_detach():
    """保留记忆时把标题写进来源，UI 才能解释是哪段对话。"""
    assert "session_title=session_title" in REMOTE
    assert "session_title" in REMOTE


def test_memory_state_is_refreshed_after_delete():
    """删完对话后记忆面板立刻刷新，不用重开页面。"""
    block = REMOTE[REMOTE.index("async def _handle_delete_session"):]
    block = block[: block.index("async def _handle_rename_session")]
    assert "_broadcast_memory_state()" in block


# ---------------------------------------------------------------- 8. 端到端（WS 处理函数）


class _DeleteStub:
    """只实现 _handle_delete_session 需要的最小状态。"""

    def __init__(self, env: SimpleEnv) -> None:
        self.env = env
        self.session_id = "some-other-session"
        self._streaming = False
        self.session_manager = env.sessions
        self.memory_manager = env.memory
        self.work_dir = env.ws
        self.artifact_root = env.ws / ".aieb" / "artifacts"
        self._stores: dict[str, object] = {}
        self.agent = None
        self.sent: list[dict] = []

    def _workspace_root(self) -> Path:
        return self.env.ws

    def _user_memory_dir(self) -> Path:
        return self.env.user_mem

    async def _broadcast(self, payload):
        self.sent.append(payload)

    async def _broadcast_memory_state(self):
        self.sent.append({"type": "memory_state"})

    async def _broadcast_session_list(self):
        self.sent.append({"type": "session_list"})


def test_handle_delete_session_end_to_end_delete(env):
    """从 WS 处理函数走一遍：勾选删除 → 用户级记忆消失。"""
    from aieb.remote import RemoteServer

    sid = env.new_session("旅行偏好")
    filename = env.save(sid, scope="user")
    stub = _DeleteStub(env)

    asyncio.run(RemoteServer._handle_delete_session(stub, sid, purge_memories=True))

    assert not (env.user_mem / filename).exists()
    assert any(p.get("type") == "memory_state" for p in stub.sent)


def test_handle_delete_session_end_to_end_keep(env):
    """从 WS 处理函数走一遍：不勾选 → 记忆保留 + detached + 面板刷新。"""
    from aieb.remote import RemoteServer

    sid = env.new_session("旅行偏好")
    filename = env.save(sid, scope="user")
    stub = _DeleteStub(env)

    asyncio.run(RemoteServer._handle_delete_session(stub, sid, purge_memories=False))

    assert (env.user_mem / filename).is_file()
    entries = provenance.load_entries(env.user_mem)
    assert entries[filename]["detached"][0]["title"] == "旅行偏好"
    assert any(p.get("type") == "memory_state" for p in stub.sent)
    system = [p for p in stub.sent if p.get("type") == "system"]
    assert system and "保留" in json.dumps(system, ensure_ascii=False)
