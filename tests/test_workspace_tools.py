"""Workspace 语义一致性测试：所有文件类工具都必须以 Agent work_dir 为基准。

布局：
    repo_root/                  <- 进程 cwd（故意放同名文件做"诱饵"）
        foo.txt                 = "REPO_ROOT"
        workspace/              <- Agent work_dir
            foo.txt             = "WORKSPACE"
            sub/...
        outside.txt             = "OUTSIDE"
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import time
from pathlib import Path

import pytest

from aieb.tools import create_default_registry
from aieb.tools.bash import Params as BashParams
from aieb.tools.bash import heuristic_workspace_violation
from aieb.tools.edit_file import Params as EditParams
from aieb.tools.glob import Params as GlobParams
from aieb.tools.grep import Params as GrepParams
from aieb.tools.read_file import Params as ReadParams
from aieb.tools.write_file import Params as WriteParams
from aieb.tools.workspace import WorkspaceResolver, WorkspaceViolation, is_within


@pytest.fixture()
def layout(tmp_path, monkeypatch):
    repo = tmp_path / "repo_root"
    ws = repo / "workspace"
    (ws / "sub").mkdir(parents=True)
    (repo / "foo.txt").write_text("REPO_ROOT", encoding="utf-8")
    (ws / "foo.txt").write_text("WORKSPACE", encoding="utf-8")
    (ws / "sub" / "nested.txt").write_text("NESTED", encoding="utf-8")
    (repo / "outside.txt").write_text("OUTSIDE", encoding="utf-8")
    # 进程 cwd 指向 repo_root：修复前相对路径会落在这里
    monkeypatch.chdir(repo)
    return repo, ws


def _registry(ws: Path):
    return create_default_registry(work_dir=str(ws))


def run(tool, params):
    return asyncio.run(tool.execute(params))


# ---------------------------------------------------------------- 相对路径基准


def test_read_file_relative_resolves_to_work_dir(layout):
    repo, ws = layout
    tool = _registry(ws).get("ReadFile")
    result = run(tool, ReadParams(file_path="foo.txt"))
    assert not result.is_error
    assert "WORKSPACE" in result.output
    assert "REPO_ROOT" not in result.output


def test_write_file_relative_resolves_to_work_dir(layout):
    repo, ws = layout
    tool = _registry(ws).get("WriteFile")
    result = run(tool, WriteParams(file_path="result.txt", content="written"))
    assert not result.is_error
    assert (ws / "result.txt").read_text(encoding="utf-8") == "written"
    assert not (repo / "result.txt").exists()


def test_edit_file_relative_resolves_to_work_dir(layout):
    repo, ws = layout
    reg = _registry(ws)
    # EditFile 要求先 ReadFile（FileStateCache 守卫），这是既有语义
    run(reg.get("ReadFile"), ReadParams(file_path="foo.txt"))
    result = run(reg.get("EditFile"), EditParams(file_path="foo.txt", old_string="WORKSPACE", new_string="EDITED"))
    assert not result.is_error, result.output
    assert (ws / "foo.txt").read_text(encoding="utf-8") == "EDITED"
    assert (repo / "foo.txt").read_text(encoding="utf-8") == "REPO_ROOT"


def test_grep_dot_resolves_to_work_dir(layout):
    repo, ws = layout
    tool = _registry(ws).get("Grep")
    result = run(tool, GrepParams(pattern="WORKSPACE", path="."))
    assert "foo.txt" in result.output
    result2 = run(tool, GrepParams(pattern="REPO_ROOT", path="."))
    assert "No matches found." in result2.output


def test_glob_pattern_resolves_to_work_dir(layout):
    repo, ws = layout
    tool = _registry(ws).get("Glob")
    result = run(tool, GlobParams(pattern="**/*.txt", path="."))
    assert "foo.txt" in result.output
    assert "sub/nested.txt" in result.output
    # outside.txt 在 repo_root，不在 workspace
    assert "outside.txt" not in result.output


def test_bash_cwd_is_work_dir(layout):
    repo, ws = layout
    tool = _registry(ws).get("Bash")
    result = run(tool, BashParams(command='python -c "import os;print(os.getcwd())"'))
    assert not result.is_error, result.output
    printed = Path(result.output.strip().splitlines()[0])
    assert printed.resolve() == ws.resolve()


def test_bash_relative_commands_see_workspace_files(layout):
    repo, ws = layout
    tool = _registry(ws).get("Bash")
    result = run(tool, BashParams(command='python -c "print(open(\'foo.txt\').read())"'))
    assert not result.is_error, result.output
    assert "WORKSPACE" in result.output


# ---------------------------------------------------------------- 同名文件选择


def test_same_name_file_prefers_workspace_copy(layout):
    repo, ws = layout
    tool = _registry(ws).get("ReadFile")
    # 显式绝对路径指向 workspace 副本
    result = run(tool, ReadParams(file_path=str(ws / "foo.txt")))
    assert "WORKSPACE" in result.output
    # 相对路径也必须选 workspace 副本
    result = run(tool, ReadParams(file_path="./foo.txt"))
    assert "WORKSPACE" in result.output


def test_write_and_edit_do_not_touch_repo_root(layout):
    repo, ws = layout
    reg = _registry(ws)
    run(reg.get("WriteFile"), WriteParams(file_path="new.txt", content="x"))
    run(reg.get("ReadFile"), ReadParams(file_path="foo.txt"))
    run(reg.get("EditFile"), EditParams(file_path="foo.txt", old_string="WORKSPACE", new_string="Y"))
    assert not (repo / "new.txt").exists()
    assert (repo / "foo.txt").read_text(encoding="utf-8") == "REPO_ROOT"


# ---------------------------------------------------------------- ../ 逃逸


@pytest.mark.parametrize(
    "tool_name,params",
    [
        ("ReadFile", lambda: ReadParams(file_path="../outside.txt")),
        ("WriteFile", lambda: WriteParams(file_path="../outside.txt", content="hack")),
        ("EditFile", lambda: EditParams(file_path="../foo.txt", old_string="REPO_ROOT", new_string="hack")),
        ("Grep", lambda: GrepParams(pattern="REPO_ROOT", path="..")),
        ("Glob", lambda: GlobParams(pattern="**/*.txt", path="..")),
    ],
)
def test_parent_directory_escape_is_denied(layout, tool_name, params):
    repo, ws = layout
    tool = _registry(ws).get(tool_name)
    result = run(tool, params())
    assert result.is_error, f"{tool_name} 应当拒绝 ../ 逃逸"
    assert "workspace" in result.output.lower()
    # 文件系统未被污染
    assert (repo / "outside.txt").read_text(encoding="utf-8") == "OUTSIDE"
    assert (repo / "foo.txt").read_text(encoding="utf-8") == "REPO_ROOT"


def test_nested_parent_segments_inside_workspace_are_allowed(layout):
    repo, ws = layout
    tool = _registry(ws).get("ReadFile")
    result = run(tool, ReadParams(file_path="sub/../foo.txt"))
    assert not result.is_error, result.output
    assert "WORKSPACE" in result.output


def test_absolute_path_outside_workspace_is_denied(layout):
    repo, ws = layout
    tool = _registry(ws).get("ReadFile")
    result = run(tool, ReadParams(file_path=str(repo / "outside.txt")))
    assert result.is_error


def test_absolute_path_inside_workspace_is_allowed(layout):
    repo, ws = layout
    tool = _registry(ws).get("ReadFile")
    result = run(tool, ReadParams(file_path=str(ws / "sub" / "nested.txt")))
    assert not result.is_error
    assert "NESTED" in result.output


# ---------------------------------------------------------------- 解析器单元测试


def test_resolver_rejects_drive_relative_path(tmp_path):
    resolver = WorkspaceResolver(tmp_path)
    if os.name != "nt":
        pytest.skip("盘符相对路径仅 Windows 存在")
    with pytest.raises(WorkspaceViolation) as exc:
        resolver.resolve("C:foo.txt")
    assert exc.value.kind == "drive_relative"


def test_resolver_accepts_absolute_inside_and_rejects_case_insensitive_outside(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    resolver = WorkspaceResolver(ws)
    assert resolver.resolve(str(ws)).path == ws.resolve()
    with pytest.raises(WorkspaceViolation):
        resolver.resolve(str(tmp_path / "not_in_ws"))


def test_is_within_prefix_confusion_is_rejected(tmp_path):
    ws = tmp_path / "ws"
    sibling = tmp_path / "ws_evil"
    ws.mkdir()
    sibling.mkdir()
    assert is_within(ws, ws / "a.txt")
    assert not is_within(ws, sibling / "a.txt")


# ---------------------------------------------------------------- symlink / junction


def _make_dir_link(link: Path, target: Path) -> bool:
    """尽量创建一个目录链接（symlink 优先，Windows 回退 junction）。"""
    try:
        os.symlink(target, link, target_is_directory=True)
        return True
    except (OSError, NotImplementedError, AttributeError):
        pass
    if os.name == "nt":
        proc = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
            text=True,
            errors="replace",
        )
        if proc.returncode == 0 and link.exists():
            return True
    return False


def test_symlink_or_junction_escape_is_rejected(layout):
    repo, ws = layout
    link = ws / "escape_link"
    if not _make_dir_link(link, repo):
        pytest.skip("当前环境无法创建 symlink 或 junction（需要权限），无法稳定测试")

    tool = _registry(ws).get("ReadFile")
    # 通过链接读取 workspace 外的 outside.txt 必须被拒绝
    result = run(tool, ReadParams(file_path="escape_link/outside.txt"))
    assert result.is_error
    assert "workspace" in result.output.lower()

    # Grep 也不跟随链接（Phase 1 行为）
    grep = _registry(ws).get("Grep")
    grep_result = run(grep, GrepParams(pattern="OUTSIDE", path="."))
    assert "No matches found." in grep_result.output


# ---------------------------------------------------------------- Bash 限定模式


def test_bash_restrict_mode_rejects_escape(layout):
    repo, ws = layout
    tool = _registry(ws).get("Bash")
    tool.restrict_to_workspace = True

    denied = run(tool, BashParams(command="type ..\\outside.txt"))
    assert denied.is_error
    assert "workspace" in denied.output.lower()

    denied_abs = run(tool, BashParams(command=f'type "{repo / "outside.txt"}"'))
    assert denied_abs.is_error

    # workspace 内命令与 PATH 中的可执行程序必须照常可用
    ok = run(tool, BashParams(command='python -c "print(1+1)"'))
    assert not ok.is_error, ok.output
    assert "2" in ok.output


def test_heuristic_violation_allows_plain_commands(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    assert heuristic_workspace_violation("python script.py", str(ws)) == ""
    assert heuristic_workspace_violation("git status", str(ws)) == ""
    assert heuristic_workspace_violation("ls -la", str(ws)) == ""
    assert heuristic_workspace_violation("cat ../x", str(ws)) != ""
    assert heuristic_workspace_violation("type C:\\Windows\\win.ini", str(ws)) != ""


# ---------------------------------------------------------------- Bash 运行时健壮性


def test_bash_does_not_block_on_inherited_stdin(layout):
    """会读标准输入的命令必须立刻 EOF 退出，而不是把工具调用挂死。

    `cmd //c ...` 在 cmd.exe 下是非法开关，会启动交互式 cmd 并等待 stdin。
    历史上（stdin 继承）它会永久阻塞，连 timeout 都救不回来。
    """
    repo, ws = layout
    tool = _registry(ws).get("Bash")

    async def run_with_hard_cap():
        return await asyncio.wait_for(
            tool.execute(BashParams(command="cmd //c dir /a /s", timeout=5)), timeout=20
        )

    start = time.monotonic()
    result = asyncio.run(run_with_hard_cap())
    assert time.monotonic() - start < 15, "Bash 不应在 stdin 上挂住"
    assert isinstance(result.output, str)


def test_bash_timeout_returns_and_does_not_hang(layout):
    repo, ws = layout
    tool = _registry(ws).get("Bash")

    async def run_with_hard_cap():
        return await asyncio.wait_for(
            tool.execute(BashParams(command='python -c "import time;time.sleep(30)"', timeout=1)),
            timeout=20,
        )

    start = time.monotonic()
    result = asyncio.run(run_with_hard_cap())
    assert result.is_error
    assert "timed out" in result.output
    assert time.monotonic() - start < 15, "超时路径本身也不能挂住"


# ---------------------------------------------------------------- 向后兼容


def test_registry_without_work_dir_keeps_legacy_cwd_behaviour(layout):
    repo, ws = layout
    reg = create_default_registry()
    assert reg.get("ReadFile")._resolver is None
    result = run(reg.get("ReadFile"), ReadParams(file_path="foo.txt"))
    # 未配置 workspace 时仍按进程 cwd（repo_root）解析
    assert "REPO_ROOT" in result.output
