"""Legacy runtime 迁移测试（.mewcode -> .aieb）。

本文件是全仓库少数允许出现旧目录名的位置之一（LEGACY MIGRATION REFERENCE）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from aieb.migration import (  # noqa: E402
    LEGACY_DIR_NAME,
    LEGACY_INSTRUCTIONS,
    MARKER_NAME,
    NEW_DIR_NAME,
    NEW_INSTRUCTIONS,
    migrate_all,
    migrate_instructions,
    migrate_runtime_dir,
)


def _make_legacy(root: Path) -> Path:
    legacy = root / LEGACY_DIR_NAME
    (legacy / "sessions").mkdir(parents=True)
    (legacy / "sessions" / "session_1.jsonl").write_text('{"role":"user"}\n', encoding="utf-8")
    (legacy / "config.yaml").write_text("providers: []\n", encoding="utf-8")
    return legacy


def test_migrates_legacy_dir_when_target_absent(tmp_path):
    _make_legacy(tmp_path)
    result = migrate_runtime_dir(tmp_path)
    assert result.action == "moved"
    assert (tmp_path / NEW_DIR_NAME / "sessions" / "session_1.jsonl").exists()
    assert not (tmp_path / LEGACY_DIR_NAME).exists()
    assert (tmp_path / NEW_DIR_NAME / MARKER_NAME).exists()


def test_migration_is_idempotent(tmp_path):
    _make_legacy(tmp_path)
    first = migrate_runtime_dir(tmp_path)
    second = migrate_runtime_dir(tmp_path)
    assert first.action == "moved"
    assert second.action == "none"


def test_existing_wendream_data_is_never_overwritten(tmp_path):
    legacy = _make_legacy(tmp_path)
    current = tmp_path / NEW_DIR_NAME
    current.mkdir()
    (current / "config.yaml").write_text("providers: [new]\n", encoding="utf-8")

    result = migrate_runtime_dir(tmp_path)
    assert result.action in {"merged", "skipped"}
    # 目标已存在的内容原样保留
    assert (current / "config.yaml").read_text(encoding="utf-8") == "providers: [new]\n"
    # 缺失项被补齐
    assert (current / "sessions" / "session_1.jsonl").exists()
    # 源目录保留（可回退）
    assert legacy.exists()


def test_no_legacy_dir_is_noop(tmp_path):
    result = migrate_runtime_dir(tmp_path)
    assert result.action == "none"
    assert not (tmp_path / NEW_DIR_NAME).exists()


def test_dry_run_does_not_touch_filesystem(tmp_path):
    _make_legacy(tmp_path)
    result = migrate_runtime_dir(tmp_path, dry_run=True)
    assert result.action == "moved"
    assert (tmp_path / LEGACY_DIR_NAME).exists()
    assert not (tmp_path / NEW_DIR_NAME).exists()


def test_instruction_file_renamed(tmp_path):
    legacy_md = tmp_path / LEGACY_INSTRUCTIONS
    legacy_md.write_text("# legacy instructions\n", encoding="utf-8")
    assert migrate_instructions(tmp_path) is True
    assert (tmp_path / NEW_INSTRUCTIONS).read_text(encoding="utf-8") == "# legacy instructions\n"
    assert not legacy_md.exists()


def test_instruction_rename_does_not_clobber(tmp_path):
    (tmp_path / LEGACY_INSTRUCTIONS).write_text("old\n", encoding="utf-8")
    (tmp_path / NEW_INSTRUCTIONS).write_text("new\n", encoding="utf-8")
    assert migrate_instructions(tmp_path) is False
    assert (tmp_path / NEW_INSTRUCTIONS).read_text(encoding="utf-8") == "new\n"


def test_migrate_all_handles_missing_home(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    _make_legacy(project)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "home_missing"))
    results = migrate_all(project, include_home=True)
    assert any(r.action == "moved" for r in results)


def test_migrated_session_data_is_still_readable(tmp_path, monkeypatch):
    """迁移后的会话仍可被 SessionManager 列出并 resume（不丢历史）。"""
    from aieb.memory.session import SessionManager

    # 1. 先在“新”目录下产生一条真实会话
    manager = SessionManager(str(tmp_path))
    session = manager.create()
    session_id = session.session_id
    from aieb.conversation import Message

    session.append(Message(role="user", content="hello"))
    session.close()

    # 2. 把整目录改名回 legacy 名字，模拟“老用户”
    (tmp_path / NEW_DIR_NAME).rename(tmp_path / LEGACY_DIR_NAME)
    assert (tmp_path / LEGACY_DIR_NAME / "sessions" / f"{session_id}.jsonl").exists()

    # 3. 迁移
    result = migrate_runtime_dir(tmp_path)
    assert result.action == "moved"
    assert not (tmp_path / LEGACY_DIR_NAME).exists()

    # 4. 迁移后仍能列出 + resume
    reopened = SessionManager(str(tmp_path))
    ids = {meta.id for meta in reopened.list()}
    assert session_id in ids
    resumed = reopened.resume(session_id)
    assert resumed is not None


def test_migration_output_has_no_secret_content(tmp_path, capsys):
    legacy = _make_legacy(tmp_path)
    (legacy / "config.yaml").write_text('providers:\n  - api_key: "${DEEPSEEK_API_KEY}"\n', encoding="utf-8")
    from aieb.migration import migrate_and_report

    notice = migrate_and_report(tmp_path, include_home=False)
    assert ".aieb" in notice
    assert "api_key" not in notice
    assert "sk-" not in notice
