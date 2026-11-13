"""生产入口的 workspace 根一致性（P0-1）。

覆盖：
  * create_default_registry(work_dir=X) 让所有工具根指向 X；
  * Agent.set_work_dir() 同时更新 tool root / permission root；
  * worktree 切换后 ReadFile / Grep / Glob / WriteFile / Bash 全部落在 worktree；
  * app.py / __main__.py / remote.py 三个生产入口都显式传 work_dir（静态守卫）。
"""

from __future__ import annotations

import ast
import asyncio
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from aieb.agent import Agent  # noqa: E402
from aieb.permissions import (  # noqa: E402
    DangerousCommandDetector,
    PathSandbox,
    PermissionChecker,
    RuleEngine,
)
from aieb.tools import create_default_registry  # noqa: E402


def _make_agent(work_dir: Path) -> Agent:
    registry = create_default_registry(work_dir=str(work_dir))
    checker = PermissionChecker(
        detector=DangerousCommandDetector(),
        sandbox=PathSandbox(str(work_dir)),
        rule_engine=RuleEngine(),
    )
    return Agent(
        client=None,  # 本测试不调用模型
        registry=registry,
        protocol="anthropic",
        work_dir=str(work_dir),
        permission_checker=checker,
    )


def _tool_roots(registry) -> dict[str, str | None]:
    roots: dict[str, str | None] = {}
    for name in ("ReadFile", "WriteFile", "EditFile"):
        tool = registry.get(name)
        resolver = getattr(tool, "_resolver", None)
        roots[name] = str(resolver.root) if resolver is not None else None
    roots["Grep"] = registry.get("Grep").root
    roots["Glob"] = registry.get("Glob").root
    roots["Bash"] = registry.get("Bash").work_dir
    return roots


# ---------------------------------------------------------------- 基本一致性


def test_registry_roots_follow_work_dir(tmp_path):
    work = (tmp_path / "ws").resolve()
    work.mkdir()
    registry = create_default_registry(work_dir=str(work))
    for name, root in _tool_roots(registry).items():
        assert root is not None, f"{name} 没有 workspace 根"
        assert Path(root) == work, f"{name} 根为 {root}，期望 {work}"


def test_agent_and_registry_roots_match(tmp_path):
    work = (tmp_path / "ws").resolve()
    work.mkdir()
    agent = _make_agent(work)
    for name, root in _tool_roots(agent.registry).items():
        assert Path(root) == Path(agent.work_dir).resolve(), name
    assert agent.permission_checker.sandbox.project_root == work


# ---------------------------------------------------------------- worktree 场景


def test_worktree_switch_moves_every_tool_root(tmp_path):
    original = tmp_path / "original"
    worktree = tmp_path / "worktree"
    original.mkdir()
    worktree.mkdir()
    (original / "same.txt").write_text("ORIGINAL\n", encoding="utf-8")
    (worktree / "same.txt").write_text("WORKTREE\n", encoding="utf-8")

    agent = _make_agent(original)

    # 切换前：读到主仓库
    from aieb.tools.read_file import Params as ReadParams

    before = asyncio.run(agent.registry.get("ReadFile").execute(ReadParams(file_path="same.txt")))
    assert "ORIGINAL" in before.output

    agent.set_work_dir(worktree)

    assert Path(agent.work_dir) == worktree.resolve()
    assert agent.permission_checker.sandbox.project_root == worktree.resolve()
    for name, root in _tool_roots(agent.registry).items():
        assert Path(root) == worktree.resolve(), f"{name} 仍指向 {root}"

    # ReadFile
    from aieb.tools.read_file import Params as ReadParams2

    after = asyncio.run(agent.registry.get("ReadFile").execute(ReadParams2(file_path="same.txt")))
    assert "WORKTREE" in after.output, after.output

    # Grep
    from aieb.tools.grep import Params as GrepParams

    grep_result = asyncio.run(agent.registry.get("Grep").execute(GrepParams(pattern="WORKTREE", path=".")))
    assert "same.txt" in grep_result.output

    # Glob
    from aieb.tools.glob import Params as GlobParams

    glob_result = asyncio.run(agent.registry.get("Glob").execute(GlobParams(pattern="**/*")))
    assert "same.txt" in glob_result.output
    assert str(original) not in glob_result.output

    # WriteFile 落到 worktree
    from aieb.tools.write_file import Params as WriteParams

    asyncio.run(
        agent.registry.get("WriteFile").execute(
            WriteParams(file_path="new.txt", content="hello")
        )
    )
    assert (worktree / "new.txt").exists()
    assert not (original / "new.txt").exists()

    # Bash cwd 也在 worktree
    from aieb.tools.bash import Params as BashParams

    bash_result = asyncio.run(
        agent.registry.get("Bash").execute(BashParams(command="cd"))
    )
    assert worktree.name in bash_result.output


def test_set_work_dir_is_idempotent(tmp_path):
    work = (tmp_path / "ws").resolve()
    work.mkdir()
    agent = _make_agent(work)
    agent.set_work_dir(work)
    agent.set_work_dir(work)
    assert Path(agent.work_dir) == work


# ---------------------------------------------------------------- 生产入口静态守卫


def _calls_to_create_registry(path: Path) -> list[ast.Call]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    calls = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id == "create_default_registry":
                calls.append(node)
    return calls


@pytest.mark.parametrize(
    "relative",
    ["aieb/app.py", "aieb/__main__.py", "aieb/remote.py"],
)
def test_production_entrypoints_pass_work_dir(relative: str):
    path = REPO_ROOT / relative
    calls = _calls_to_create_registry(path)
    assert calls, f"{relative} 中找不到 create_default_registry 调用"
    for call in calls:
        keywords = {kw.arg for kw in call.keywords}
        assert "work_dir" in keywords, (
            f"{relative}:{call.lineno} 调用 create_default_registry 时没有传 work_dir —— "
            "会导致 Agent.work_dir / 工具根 / Permission 根不一致"
        )


def test_no_duplicate_toolsearch_registration():
    """ToolSearch 由 create_default_registry 注册，生产入口不得重复注册。"""
    for relative in ("aieb/app.py", "aieb/__main__.py", "aieb/remote.py"):
        text = (REPO_ROOT / relative).read_text(encoding="utf-8")
        assert "registry.register(ToolSearchTool(" not in text, relative
