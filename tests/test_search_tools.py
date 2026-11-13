"""Grep / Glob 搜索语义与性能边界的回归测试（不经过 LLM）。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from aieb.tools.glob import Glob
from aieb.tools.glob import Params as GlobParams
from aieb.tools.grep import Grep
from aieb.tools.grep import Params as GrepParams
from aieb.tools.search_utils import MAX_MATCHES, MAX_GLOB_RESULTS, resolve_search_root

TARGET = "SEARCH_TARGET_TOKEN"


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _make_workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "workspace"
    _write(ws / "src" / "keep.py", f"# {TARGET}\n")
    _write(ws / "src" / "other.py", "no token here\n")
    for noise in ("node_modules", ".git", "__pycache__", "build", "dist", ".conda", "venv"):
        _write(ws / noise / "decoy.py", f"# {TARGET} decoy in {noise}\n")
    return ws


def test_grep_dot_resolves_to_configured_root_not_cwd(tmp_path, monkeypatch):
    ws = _make_workspace(tmp_path)
    other = tmp_path / "elsewhere"
    _write(other / "outside.py", f"# {TARGET} outside\n")

    # 把进程 cwd 切到外部目录：修复前 "." 会从这里开始搜
    monkeypatch.chdir(other)

    tool = Grep(root=str(ws))
    result = asyncio.run(tool.execute(GrepParams(pattern=TARGET, path=".")))

    assert "src/keep.py" in result.output
    assert "outside.py" not in result.output
    assert tool.last_stats["directories_scanned"] > 0


def test_grep_excludes_noise_directories(tmp_path):
    ws = _make_workspace(tmp_path)
    tool = Grep(root=str(ws))
    result = asyncio.run(tool.execute(GrepParams(pattern=TARGET, path=".")))

    for noise in ("node_modules", ".git", "__pycache__", "build", "dist", ".conda", "venv"):
        assert f"{noise}/decoy.py" not in result.output
    assert "src/keep.py" in result.output


def test_search_path_cannot_escape_root(tmp_path):
    ws = _make_workspace(tmp_path)
    outside = tmp_path / "outside"
    _write(outside / "secret.py", "SECRET\n")

    base, error = resolve_search_root("../outside", ws)
    assert base is None
    assert "escapes the workspace root" in error

    tool = Grep(root=str(ws))
    result = asyncio.run(tool.execute(GrepParams(pattern="SECRET", path="../outside")))
    assert result.is_error
    assert "escapes the workspace root" in result.output


def test_grep_missing_string_returns_no_matches_without_false_positive(tmp_path):
    ws = _make_workspace(tmp_path)
    tool = Grep(root=str(ws))
    result = asyncio.run(tool.execute(GrepParams(pattern="THIS_STRING_ABSENT_98765", path=".")))

    assert "No matches found." in result.output
    assert not result.is_error
    assert tool.last_stats["matches"] == 0


def test_grep_truncates_at_max_matches(tmp_path):
    ws = tmp_path / "ws"
    for i in range(MAX_MATCHES + 50):
        _write(ws / "many" / f"f_{i}.py", f"{TARGET}\n")

    tool = Grep(root=str(ws))
    result = asyncio.run(tool.execute(GrepParams(pattern=TARGET, path="many")))

    assert "[truncated" in result.output
    assert tool.last_stats["truncated"] is True
    assert len([ln for ln in result.output.splitlines() if "f_" in ln]) <= MAX_MATCHES


def test_glob_caps_results_and_reports_truncation(tmp_path):
    ws = tmp_path / "ws"
    for i in range(MAX_GLOB_RESULTS + 100):
        _write(ws / "many" / f"f_{i}.py", "x\n")

    tool = Glob(root=str(ws))
    result = asyncio.run(tool.execute(GlobParams(pattern="**/*.py", path="many")))

    lines = [ln for ln in result.output.splitlines() if ln and not ln.startswith("[")]
    assert len(lines) <= MAX_GLOB_RESULTS
    assert tool.last_stats["truncated"] is True


def test_glob_excludes_noise_directories(tmp_path):
    ws = _make_workspace(tmp_path)
    tool = Glob(root=str(ws))
    result = asyncio.run(tool.execute(GlobParams(pattern="**/*.py", path=".")))

    for noise in ("node_modules", ".git", "__pycache__", "build", "dist", ".conda", "venv"):
        assert f"{noise}/decoy.py" not in result.output
    assert "src/keep.py" in result.output


def test_glob_double_star_matches_root_level_files(tmp_path):
    """pathlib 语义：**/ 可以匹配零层目录。"""
    ws = tmp_path / "ws"
    _write(ws / "top.py", "x\n")
    _write(ws / "pkg" / "nested.py", "x\n")

    tool = Glob(root=str(ws))
    result = asyncio.run(tool.execute(GlobParams(pattern="**/*.py", path=".")))

    assert "top.py" in result.output
    assert "pkg/nested.py" in result.output


def test_registry_scopes_search_and_shell_to_work_dir(tmp_path):
    from aieb.tools import create_default_registry

    ws = tmp_path / "ws"
    ws.mkdir()

    scoped = create_default_registry(work_dir=str(ws))
    assert scoped.get("Grep").root == str(ws)
    assert scoped.get("Glob").root == str(ws)
    assert scoped.get("Bash").work_dir == str(ws)

    # 未指定 work_dir 时保持旧行为（回落到进程 cwd）
    legacy = create_default_registry()
    assert legacy.get("Grep").root is None
    assert legacy.get("Bash").work_dir is None


def test_search_is_cancellable(tmp_path):
    ws = tmp_path / "ws"
    for i in range(400):
        _write(ws / "many" / f"f_{i}.py", "line\n" * 50)

    tool = Grep(root=str(ws))

    async def run():
        await asyncio.wait_for(tool.execute(GrepParams(pattern="nomatch", path=".")), timeout=0.001)

    with pytest.raises((asyncio.TimeoutError, TimeoutError)):
        asyncio.run(run())
