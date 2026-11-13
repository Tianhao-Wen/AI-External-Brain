"""删除会话的数据清理测试：消息、快照、产物、专属记忆、其它会话不受影响。"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from aieb import artifacts  # noqa: E402
from aieb import session_purge  # noqa: E402
from aieb.memory import provenance  # noqa: E402
from aieb.memory.auto_memory import MemoryManager  # noqa: E402
from aieb.memory.session import SessionManager  # noqa: E402


# ---------------------------------------------------------------- 辅助


def make_workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    return ws


def write_memory(mem_dir: Path, name: str, body: str = "内容", mtype: str = "project") -> Path:
    mem_dir.mkdir(parents=True, exist_ok=True)
    path = mem_dir / f"{name}.md"
    path.write_text(
        f"---\nname: {name}\ndescription: {name} 的描述\nmetadata:\n  type: {mtype}\n---\n\n{body}\n",
        encoding="utf-8",
    )
    index = mem_dir / "MEMORY.md"
    existing = index.read_text(encoding="utf-8") if index.exists() else ""
    if f"{name}.md" not in existing:
        index.write_text(existing + f"- [{name}]({name}.md) — {name} 的描述\n", encoding="utf-8")
    return path


def make_session(ws: Path) -> tuple[SessionManager, str]:
    manager = SessionManager(str(ws))
    session = manager.create()
    session.close()
    return manager, session.session_id


# ---------------------------------------------------------------- 1. 消息记录


def test_purge_removes_session_records(tmp_path):
    ws = make_workspace(tmp_path)
    manager, sid = make_session(ws)
    assert (ws / ".aieb" / "sessions" / f"{sid}.jsonl").exists()
    assert (ws / ".aieb" / "sessions" / f"{sid}.meta").exists()

    report = session_purge.purge_session(sid, work_dir=ws, session_manager=manager)

    assert report.ok, report.errors
    assert not (ws / ".aieb" / "sessions" / f"{sid}.jsonl").exists()
    assert not (ws / ".aieb" / "sessions" / f"{sid}.meta").exists()
    assert not any(m.id == sid for m in manager.list())
    assert report.session_files_removed == 2


def test_purge_removes_file_history(tmp_path):
    ws = make_workspace(tmp_path)
    manager, sid = make_session(ws)
    hist = ws / ".aieb" / "file-history" / sid
    hist.mkdir(parents=True)
    (hist / "abc@v1").write_bytes(b"backup")

    report = session_purge.purge_session(sid, work_dir=ws, session_manager=manager)

    assert report.ok, report.errors
    assert not hist.exists()
    assert report.file_history_removed == 1


# ---------------------------------------------------------------- 2. 产物


def test_purge_removes_artifacts_and_invalidates_links(tmp_path):
    ws = make_workspace(tmp_path)
    manager, sid = make_session(ws)
    art_root = tmp_path / "artifacts"

    store = artifacts.ArtifactStore(sid, root=art_root, workspace=ws)
    src = ws / "报告.md"
    src.write_text("交付内容", encoding="utf-8")
    item = store.add_file(src, turn=1, name="报告.md")
    assert item is not None

    from aieb.remote import RemoteServer

    server = RemoteServer(providers=[SimpleNamespace(name="p", protocol="anthropic", model="m")])
    server.agent = SimpleNamespace(work_dir=str(ws), session_id=sid)
    server.session_id = sid
    server.artifact_root = art_root
    url = server.artifact_url(item.id)
    assert server._process_http_request(None, SimpleNamespace(path=url, headers={})).status_code == 200

    report = session_purge.purge_session(
        sid, work_dir=ws, session_manager=manager, artifact_root=art_root
    )

    assert report.ok, report.errors
    assert not (art_root / sid).exists()
    assert report.artifacts_removed == ["报告.md"]
    # 用户原始工作文件不能被删
    assert src.exists()
    # 旧下载链接失效
    assert server._process_http_request(None, SimpleNamespace(path=url, headers={})).status_code == 404


# ---------------------------------------------------------------- 3. 记忆


def test_project_memory_from_single_session_is_deleted(tmp_path):
    ws = make_workspace(tmp_path)
    manager, sid = make_session(ws)
    mem = ws / ".aieb" / "memory"
    write_memory(mem, "trip-plan", mtype="project")
    write_memory(mem, "keep-me", mtype="project")
    # trip-plan 只由本会话贡献；keep-me 还有另一个来源会话
    provenance.save_sources(mem, {"trip-plan.md": [sid], "keep-me.md": [sid, "session_other"]})

    report = session_purge.purge_session(sid, work_dir=ws, session_manager=manager)

    assert report.ok, report.errors
    assert "trip-plan.md" in report.memories_removed
    assert not (mem / "trip-plan.md").exists()
    # 另一个会话贡献过的记忆保留
    assert (mem / "keep-me.md").exists()
    assert "keep-me.md" in report.memories_kept_shared
    # 索引里不能残留已删条目
    index = (mem / "MEMORY.md").read_text(encoding="utf-8")
    assert "trip-plan.md" not in index
    assert "keep-me.md" in index


def test_shared_memory_survives_and_drops_source(tmp_path):
    ws = make_workspace(tmp_path)
    manager, sid = make_session(ws)
    mem = ws / ".aieb" / "memory"
    write_memory(mem, "shared", mtype="project")
    provenance.record_changes(mem, {}, sid)
    provenance.record_changes(mem, {"shared.md": 0}, "session_b")

    report = session_purge.purge_session(sid, work_dir=ws, session_manager=manager)

    assert (mem / "shared.md").exists()
    assert "shared.md" in report.memories_kept_shared
    assert provenance.sessions_of(mem, "shared.md") == ["session_b"]


def test_user_level_memory_is_deleted_by_default(tmp_path):
    """默认行为：删除对话时，它产生的用户级长期记忆也一起删掉。

    这正是历史 bug：以前用户级目录只落墓碑、从不 prune，所以删完对话后
    「记忆管理」里还留着一条「来自一个已删除的对话」。
    """
    ws = make_workspace(tmp_path)
    manager, sid = make_session(ws)
    project_mem = ws / ".aieb" / "memory"
    user_mem = tmp_path / "user_mem"
    write_memory(project_mem, "proj-note", mtype="project")
    write_memory(user_mem, "prefers-short-answers", mtype="user")
    provenance.record_changes(project_mem, {}, sid)
    provenance.record_changes(user_mem, {}, sid)

    report = session_purge.purge_session(
        sid, work_dir=ws, session_manager=manager, user_memory_dir=user_mem
    )

    assert report.ok, report.errors
    assert not (project_mem / "proj-note.md").exists()
    assert not (user_mem / "prefers-short-answers.md").exists()
    assert "prefers-short-answers.md" in report.memories_removed


def test_user_level_memory_is_kept_when_user_opts_out(tmp_path):
    """用户取消勾选：记忆保留，但来源改成 detached，UI 能说明是"有意保留"。"""
    ws = make_workspace(tmp_path)
    manager, sid = make_session(ws)
    user_mem = tmp_path / "user_mem"
    write_memory(user_mem, "prefers-short-answers", mtype="user")
    provenance.record_changes(user_mem, {}, sid)

    report = session_purge.purge_session(
        sid,
        work_dir=ws,
        session_manager=manager,
        user_memory_dir=user_mem,
        purge_memories=False,
        session_title="旅行偏好",
    )

    assert report.ok, report.errors
    assert (user_mem / "prefers-short-answers.md").exists()
    assert "prefers-short-answers.md" in report.memories_detached
    entries = provenance.load_entries(user_mem)
    assert entries["prefers-short-answers.md"]["sessions"] == []
    assert entries["prefers-short-answers.md"]["detached"][0]["title"] == "旅行偏好"


def test_untracked_legacy_memory_is_kept_and_reported(tmp_path):
    ws = make_workspace(tmp_path)
    manager, sid = make_session(ws)
    mem = ws / ".aieb" / "memory"
    write_memory(mem, "legacy-note", mtype="reference")  # 没有任何来源记录

    report = session_purge.purge_session(sid, work_dir=ws, session_manager=manager)

    assert (mem / "legacy-note.md").exists()
    assert "legacy-note.md" in report.memories_untracked


def test_tombstone_blocks_rewrite_after_purge(tmp_path):
    ws = make_workspace(tmp_path)
    manager, sid = make_session(ws)
    mem = ws / ".aieb" / "memory"
    mem.mkdir(parents=True)

    session_purge.purge_session(sid, work_dir=ws, session_manager=manager)

    assert provenance.is_purged(mem, sid) is True
    assert provenance.is_purged(mem, "session_other") is False


# ---------------------------------------------------------------- 4. 隔离


def test_other_sessions_unaffected(tmp_path):
    ws = make_workspace(tmp_path)
    manager = SessionManager(str(ws))
    keep = manager.create()
    keep_id = keep.session_id
    keep.close()
    drop = manager.create()
    drop_id = drop.session_id
    drop.close()

    keep_hist = ws / ".aieb" / "file-history" / keep_id
    keep_hist.mkdir(parents=True)
    (keep_hist / "x@v1").write_bytes(b"keep")

    report = session_purge.purge_session(drop_id, work_dir=ws, session_manager=manager)

    assert report.ok, report.errors
    assert any(m.id == keep_id for m in manager.list())
    assert (ws / ".aieb" / "sessions" / f"{keep_id}.jsonl").exists()
    assert keep_hist.exists()


# ---------------------------------------------------------------- 5. 失败上报


def test_purge_reports_errors_instead_of_silent_success(tmp_path, monkeypatch):
    ws = make_workspace(tmp_path)
    manager, sid = make_session(ws)

    def boom(_session_id):
        raise RuntimeError("文件被占用")

    monkeypatch.setattr(manager, "delete", boom)

    report = session_purge.purge_session(sid, work_dir=ws, session_manager=manager)

    assert not report.ok
    assert any("文件被占用" in e for e in report.errors)


# ---------------------------------------------------------------- 6. 记忆管理


def test_memory_manager_delete_removes_file_and_index(tmp_path):
    ws = make_workspace(tmp_path)
    mm = MemoryManager(str(ws))
    mem = Path(mm.project_mem_dir)
    write_memory(mem, "to-remove", mtype="project")

    ok, message = mm.delete("to-remove")

    assert ok, message
    assert not (mem / "to-remove.md").exists()
    assert "to-remove.md" not in (mem / "MEMORY.md").read_text(encoding="utf-8")


def test_memory_list_shows_sources(tmp_path):
    ws = make_workspace(tmp_path)
    mm = MemoryManager(str(ws))
    mem = Path(mm.project_mem_dir)
    write_memory(mem, "with-source", mtype="project")
    provenance.record_changes(mem, {}, "session_x")

    joined = "\n".join(mm.get_memories_with_sources())
    assert "with-source" in joined
    assert "来源会话 1 个" in joined


# ---------------------------------------------------------------- 7. 重启/刷新后不可恢复


def test_purged_session_not_recoverable_after_restart(tmp_path):
    ws = make_workspace(tmp_path)
    manager, sid = make_session(ws)
    session_purge.purge_session(sid, work_dir=ws, session_manager=manager)

    # 模拟服务重启：全新 manager 从磁盘重建列表
    fresh = SessionManager(str(ws))
    assert fresh.resume(sid) is None
    assert all(meta.id != sid for meta in fresh.list())

    # 新建会话不会把旧会话的 id 复活
    new_session = fresh.create()
    assert new_session.session_id != sid
    new_session.close()


def test_deleted_session_content_not_returned_by_memory_store(tmp_path):
    ws = make_workspace(tmp_path)
    manager, sid = make_session(ws)
    mem = ws / ".aieb" / "memory"
    write_memory(mem, "only-from-this-chat", body="暗号 ZEBRA-42", mtype="reference")
    provenance.record_changes(mem, {}, sid)

    session_purge.purge_session(sid, work_dir=ws, session_manager=manager)

    remaining = "\n".join(p.read_text(encoding="utf-8") for p in mem.glob("*.md"))
    assert "ZEBRA-42" not in remaining


# ---------------------------------------------------------------- 8. Web 删除处理


@pytest.mark.asyncio
async def test_web_delete_handler_purges_and_reports(tmp_path):
    ws = make_workspace(tmp_path)
    manager = SessionManager(str(ws))
    keep = manager.create()
    keep_id = keep.session_id
    keep.close()
    drop = manager.create()
    drop_id = drop.session_id
    drop.close()

    from aieb.remote import RemoteServer

    server = RemoteServer(providers=[SimpleNamespace(name="p", protocol="anthropic", model="m")])
    server.session_manager = manager
    server.work_dir = ws
    server.artifact_root = tmp_path / "artifacts"
    server.session_id = keep_id  # 删除的是非当前会话
    server.agent = SimpleNamespace(work_dir=str(ws), session_id=keep_id, _extracting=False)

    messages: list[dict] = []

    async def fake_broadcast(msg: dict) -> None:
        messages.append(msg)

    server._broadcast = fake_broadcast  # type: ignore[assignment]

    await server._handle_delete_session(drop_id)

    assert all(meta.id != drop_id for meta in manager.list())
    assert any(meta.id == keep_id for meta in manager.list())
    system_msgs = [m for m in messages if m["type"] == "system"]
    assert any("已删除" in m["data"]["message"] for m in system_msgs)
    assert not [m for m in messages if m["type"] == "error"]
