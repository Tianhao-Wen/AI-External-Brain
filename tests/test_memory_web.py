"""Web 记忆管理 + 写入时来源追踪的测试。"""

from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from aieb.memory import auto_memory, provenance  # noqa: E402
from aieb.memory.auto_memory import MemoryManager  # noqa: E402


# ---------------------------------------------------------------- 辅助


@pytest.fixture
def mm(tmp_path, monkeypatch):
    """项目级 + 隔离的用户级记忆目录。"""
    ws = tmp_path / "ws"
    ws.mkdir()
    user = tmp_path / "user_mem"
    user.mkdir()
    monkeypatch.setattr(
        auto_memory, "get_user_auto_mem_path", lambda: str(user) + os.sep
    )
    manager = MemoryManager(str(ws))
    return manager, ws, user


def write_memory(directory: Path, name: str, body: str = "内容", mtype: str = "project") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.md"
    path.write_text(
        f"---\nname: {name}\ndescription: {name} 的描述\nmetadata:\n  type: {mtype}\n---\n\n{body}\n",
        encoding="utf-8",
    )
    index = directory / "MEMORY.md"
    existing = index.read_text(encoding="utf-8") if index.exists() else ""
    index.write_text(existing + f"- [{name}]({name}.md) — {name} 的描述\n", encoding="utf-8")
    return path


class FakeClient:
    """最小可用的流式客户端，用于驱动 MemoryManager.extract()。"""

    def __init__(self, reply: str) -> None:
        self.reply = reply

    async def stream(self, conversation, system: str = ""):  # noqa: ANN001, ARG002
        from aieb.tools.base import StreamEnd, TextDelta

        yield TextDelta(text=self.reply)
        yield StreamEnd(stop_reason="stop")


def make_conversation(text: str = "我喜欢旅行"):
    from aieb.conversation import ConversationManager, Message

    conv = ConversationManager()
    conv.history = [Message(role="user", content=text)]
    return conv


EXTRACT_REPLY = (
    "---\n"
    "MEMORY_NAME: likes-travel\n"
    "MEMORY_TYPE: user\n"
    "MEMORY_DESC: 喜欢旅行\n"
    "MEMORY_BODY: 用户喜欢旅行和自然风景\n"
)


# ---------------------------------------------------------------- 1. 列表


def test_list_for_web_shows_user_and_project(mm):
    manager, _ws, user = mm
    project_dir = Path(manager.project_mem_dir)
    write_memory(project_dir, "trip-plan", mtype="project")
    write_memory(user, "likes-travel", mtype="user")
    provenance.record_write(project_dir, "trip-plan.md", session_id="s1", source="extract")
    provenance.record_write(user, "likes-travel.md", session_id="s2", source="extract")

    items = manager.list_for_web({"s1": "杭州行程", "s2": "偏好闲聊"})

    assert len(items) == 2
    by_title = {item["title"]: item for item in items}
    assert by_title["trip-plan"]["scope_label"] == "当前项目记忆"
    assert by_title["likes-travel"]["scope_label"] == "我的长期偏好"
    assert "杭州行程" in by_title["trip-plan"]["source_label"]
    assert by_title["likes-travel"]["source_label"] == "来自对话「偏好闲聊」"
    # 不泄漏路径 / 内部 ID
    blob = json.dumps(items, ensure_ascii=False)
    assert str(project_dir) not in blob
    assert "session_" not in blob
    assert by_title["trip-plan"]["id"].startswith("mem_")


def test_list_marks_legacy_memory_as_unknown(mm):
    manager, _ws, _user = mm
    project_dir = Path(manager.project_mem_dir)
    write_memory(project_dir, "legacy-note", mtype="reference")

    items = manager.list_for_web({})
    assert items[0]["source_label"] == "历史记忆，来源未知"


# ---------------------------------------------------------------- 2. 详情


def test_detail_contains_body(mm):
    manager, _ws, _user = mm
    project_dir = Path(manager.project_mem_dir)
    write_memory(project_dir, "detail-me", body="第一行\n第二行", mtype="project")

    item = manager.list_for_web({})[0]
    assert "第一行" in item["body"]
    assert "第二行" in item["body"]
    assert item["preview"]


# ---------------------------------------------------------------- 3. 单条删除


def test_delete_by_id_removes_file_index_and_source(mm):
    manager, _ws, _user = mm
    project_dir = Path(manager.project_mem_dir)
    path = write_memory(project_dir, "to-remove", mtype="project")
    provenance.record_write(project_dir, "to-remove.md", session_id="s1", source="extract")
    mem_id = manager.memory_id("project", "to-remove.md")

    ok, message = manager.delete_by_id(mem_id)

    assert ok, message
    assert not path.exists()
    assert "to-remove.md" not in (project_dir / "MEMORY.md").read_text(encoding="utf-8")
    assert provenance.sessions_of(project_dir, "to-remove.md") == []
    assert manager.list_for_web({}) == []


def test_delete_unknown_id_fails_cleanly(mm):
    manager, _ws, _user = mm
    ok, message = manager.delete_by_id("mem_not_a_real_id")
    assert not ok
    assert "找不到" in message


def test_delete_does_not_touch_other_memories(mm):
    manager, _ws, _user = mm
    project_dir = Path(manager.project_mem_dir)
    keep = write_memory(project_dir, "keep", mtype="project")
    write_memory(project_dir, "drop", mtype="project")

    manager.delete_by_id(manager.memory_id("project", "drop.md"))
    assert keep.exists()


# ---------------------------------------------------------------- 4. 清空


def test_clear_project_keeps_user_memories(mm):
    manager, _ws, user = mm
    project_dir = Path(manager.project_mem_dir)
    write_memory(project_dir, "p1", mtype="project")
    write_memory(user, "u1", mtype="user")

    ok, message, removed = manager.clear_scope("project")

    assert ok, message
    assert removed == 1
    assert not (project_dir / "p1.md").exists()
    assert (user / "u1.md").exists()


def test_clear_all_removes_both_and_keeps_settings(mm):
    manager, _ws, user = mm
    project_dir = Path(manager.project_mem_dir)
    write_memory(project_dir, "p1", mtype="project")
    write_memory(user, "u1", mtype="user")
    manager.set_enabled(False)

    ok, message, removed = manager.clear_scope("all")

    assert ok, message
    assert removed == 2
    assert not (project_dir / "p1.md").exists()
    assert not (user / "u1.md").exists()
    # 开关设置不能被清空操作带走
    assert manager.enabled is False


# ---------------------------------------------------------------- 5. 开关


def test_enabled_toggle_persists(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    user = tmp_path / "user"
    user.mkdir()
    monkeypatch.setattr(auto_memory, "get_user_auto_mem_path", lambda: str(user) + os.sep)

    MemoryManager(str(ws)).set_enabled(False)
    assert MemoryManager(str(ws)).enabled is False
    MemoryManager(str(ws)).set_enabled(True)
    assert MemoryManager(str(ws)).enabled is True


def test_disabled_skips_read_and_extract(mm):
    import asyncio

    manager, _ws, _user = mm
    write_memory(Path(manager.project_mem_dir), "existing", mtype="project")
    manager.set_enabled(False)

    assert manager.load() == ""  # 不注入已有记忆

    asyncio.run(manager.extract(FakeClient(EXTRACT_REPLY), make_conversation(), "openai", "s1"))
    files = sorted(p.name for p in Path(manager.project_mem_dir).glob("*.md") if p.name != "MEMORY.md")
    assert files == ["existing.md"]  # 没有新写入


# ---------------------------------------------------------------- 6. 来源追踪


def test_extract_records_session_source(mm):
    import asyncio

    manager, _ws, _user = mm
    asyncio.run(manager.extract(FakeClient(EXTRACT_REPLY), make_conversation(), "openai", "s1"))

    user_dir = Path(manager.user_mem_dir)
    entry = provenance.load_entries(user_dir)["likes-travel.md"]
    assert entry["sessions"] == ["s1"]
    assert entry["source"] == provenance.SOURCE_EXTRACT
    assert entry["digest"].startswith("sha1:")


def test_two_sessions_writing_concurrently_do_not_mix(mm):
    manager, _ws, _user = mm
    project_dir = Path(manager.project_mem_dir)
    write_memory(project_dir, "a", mtype="project")
    write_memory(project_dir, "b", mtype="project")

    errors: list[Exception] = []

    def worker(name: str, session: str) -> None:
        try:
            for _ in range(20):
                provenance.record_write(project_dir, f"{name}.md", session_id=session, source="tool")
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [
        threading.Thread(target=worker, args=("a", "s1")),
        threading.Thread(target=worker, args=("b", "s2")),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    entries = provenance.load_entries(project_dir)
    assert entries["a.md"]["sessions"] == ["s1"]
    assert entries["b.md"]["sessions"] == ["s2"]
    # 索引仍然是合法 JSON
    json.loads((project_dir / "sources.json").read_text(encoding="utf-8"))


def test_shared_memory_keeps_other_session(mm):
    manager, _ws, _user = mm
    project_dir = Path(manager.project_mem_dir)
    write_memory(project_dir, "shared", mtype="project")
    provenance.record_write(project_dir, "shared.md", session_id="s1", source="extract")
    provenance.record_write(project_dir, "shared.md", session_id="s2", source="extract")

    result = provenance.prune_session(project_dir, "s1")

    assert (project_dir / "shared.md").exists()
    assert result.kept_shared == ["shared.md"]
    assert provenance.sessions_of(project_dir, "shared.md") == ["s2"]


def test_tombstone_blocks_late_extraction(mm):
    import asyncio

    manager, _ws, _user = mm
    user_dir = Path(manager.user_mem_dir)
    provenance.mark_purged(Path(manager.project_mem_dir), "s1")
    provenance.mark_purged(user_dir, "s1")

    asyncio.run(manager.extract(FakeClient(EXTRACT_REPLY), make_conversation(), "openai", "s1"))

    assert not (user_dir / "likes-travel.md").exists()


def test_legacy_v1_sources_file_is_readable(tmp_path):
    directory = tmp_path / "mem"
    directory.mkdir()
    (directory / "sources.json").write_text(
        json.dumps({"version": 1, "entries": {"old.md": {"sessions": ["s9"]}}}), encoding="utf-8"
    )
    assert provenance.sessions_of(directory, "old.md") == ["s9"]
    assert provenance.source_of(directory, "old.md") == provenance.SOURCE_UNKNOWN

    (directory / "sources.json").write_text(
        json.dumps({"old2.md": ["s8"]}), encoding="utf-8"
    )
    assert provenance.sessions_of(directory, "old2.md") == ["s8"]


def test_sources_survive_restart(mm):
    manager, ws, _user = mm
    project_dir = Path(manager.project_mem_dir)
    write_memory(project_dir, "persisted", mtype="project")
    provenance.record_write(project_dir, "persisted.md", session_id="s7", source="extract")

    fresh = MemoryManager(str(ws))
    items = fresh.list_for_web({"s7": "重启前的对话"})
    assert items[0]["source_label"] == "来自对话「重启前的对话」"


# ---------------------------------------------------------------- 7. 安全


def test_note_write_rejects_paths_outside_memory(mm, tmp_path):
    manager, _ws, _user = mm
    outside = tmp_path / "outside.md"
    outside.write_text("x", encoding="utf-8")

    assert manager.note_write(outside, session_id="s1") is False
    assert manager.note_write("../../etc/passwd", session_id="s1") is False


def test_path_like_memory_id_is_not_resolved(mm, tmp_path):
    manager, _ws, _user = mm
    write_memory(Path(manager.project_mem_dir), "real", mtype="project")
    for evil in ("../../real.md", "../real", "C:\\Windows\\win.ini", "mem_x/../../real"):
        ok, _message = manager.delete_by_id(evil)
        assert not ok
    assert (Path(manager.project_mem_dir) / "real.md").exists()


def test_memory_payload_keeps_content_as_plain_text(mm):
    """记忆内容是纯文本载荷，前端用 textContent 渲染，不会被当作 HTML。"""
    manager, _ws, _user = mm
    project_dir = Path(manager.project_mem_dir)
    payload = '<img src=x onerror=alert(1)><script>alert(2)</script>'
    write_memory(project_dir, "xss-check", body=payload, mtype="project")

    item = manager.list_for_web({})[0]
    assert payload in item["body"]
    # 载荷是 JSON 字段，不含任何执行上下文
    dumped = json.dumps(item, ensure_ascii=False)
    assert "<script>" in dumped


def test_web_source_uses_textcontent_for_memory_fields():
    """渲染记忆卡片时必须用 textContent，禁止 innerHTML 拼接记忆内容。"""
    source = (REPO_ROOT / "aieb" / "web_content.py").read_text(encoding="utf-8")
    start = source.find("function renderMemoryState")
    assert start > 0
    body = source[start:start + 3000]
    assert "textContent" in body
    assert "title.innerHTML" not in body


# ---------------------------------------------------------------- 8. 删除会话联动


def test_purge_deletes_only_session_owned_memories(mm):
    from aieb import session_purge
    from aieb.memory.session import SessionManager

    manager, ws, _user = mm
    project_dir = Path(manager.project_mem_dir)
    write_memory(project_dir, "only-a", mtype="project")
    write_memory(project_dir, "shared", mtype="project")
    provenance.record_write(project_dir, "only-a.md", session_id="sA", source="extract")
    provenance.record_write(project_dir, "shared.md", session_id="sA", source="extract")
    provenance.record_write(project_dir, "shared.md", session_id="sB", source="extract")

    sessions = SessionManager(str(ws))
    report = session_purge.purge_session(
        "sA", work_dir=ws, session_manager=sessions, user_memory_dir=Path(manager.user_mem_dir)
    )

    assert report.memories_removed == ["only-a.md"]
    assert (project_dir / "shared.md").exists()
    assert "only-a.md" not in (project_dir / "MEMORY.md").read_text(encoding="utf-8")


def test_web_delete_memory_does_not_touch_sessions(mm):
    manager, _ws, _user = mm
    project_dir = Path(manager.project_mem_dir)
    write_memory(project_dir, "m1", mtype="project")
    provenance.record_write(project_dir, "m1.md", session_id="s1", source="extract")

    ok, _ = manager.delete_by_id(manager.memory_id("project", "m1.md"))
    assert ok
    # 会话文件与来源记录是两回事：删记忆不能连带删会话
    assert not (project_dir / "m1.md").exists()
