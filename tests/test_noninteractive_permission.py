"""非交互模式（-p）的权限语义：ASK ≠ ALLOW（P0-2）。

覆盖：
  1. 规则 ALLOW           -> allow
  2. 规则 DENY            -> deny
  3. 规则 ASK             -> 非交互默认 deny
  4. 显式危险模式（bypass）-> ASK 才允许
  5. 默认非交互下 WriteFile / Bash 的 ASK 不会被静默放行
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from aieb.permissions import (  # noqa: E402
    DangerousCommandDetector,
    PathSandbox,
    PermissionChecker,
    PermissionMode,
    RuleEngine,
    bypass_mode_allows_ask,
    noninteractive_response,
)
from aieb.tools import create_default_registry  # noqa: E402


def _checker(rule: str | None, mode: PermissionMode = PermissionMode.DEFAULT) -> PermissionChecker:
    engine = RuleEngine()
    if rule is not None:
        tool_name, pattern, effect = rule.split(":", 2)
        rule_file = Path(tempfile.mkdtemp()) / "rules.yaml"
        rule_file.write_text(
            f"- effect: {effect}\n  rule: '{tool_name}({pattern})'\n", encoding="utf-8"
        )
        engine = RuleEngine(local_rules_path=rule_file)
    return PermissionChecker(
        detector=DangerousCommandDetector(),
        sandbox=PathSandbox(str(REPO_ROOT)),
        rule_engine=engine,
        mode=mode,
    )


def _decide(checker: PermissionChecker, tool_name: str, args: dict) -> str:
    registry = create_default_registry(work_dir=str(REPO_ROOT))
    tool = registry.get(tool_name)
    assert tool is not None
    return checker.check(tool, args).effect


# ---------------------------------------------------------------- 规则 → 决策


def test_rule_allow_is_allow():
    checker = _checker("WriteFile:*:allow")
    assert _decide(checker, "WriteFile", {"file_path": "x.txt", "content": "y"}) == "allow"


def test_rule_deny_is_deny():
    checker = _checker("WriteFile:*:deny")
    assert _decide(checker, "WriteFile", {"file_path": "x.txt", "content": "y"}) == "deny"


def test_rule_ask_is_ask():
    checker = _checker("Bash:*:ask")
    assert _decide(checker, "Bash", {"command": "echo hi > out.txt"}) == "ask"


# ---------------------------------------------------------------- ASK 的两种处理


def test_ask_is_denied_in_noninteractive_mode():
    assert noninteractive_response("ask", allow_ask=False) == "deny"


def test_ask_allowed_only_in_explicit_bypass_mode():
    assert noninteractive_response("ask", allow_ask=True) == "allow"
    assert bypass_mode_allows_ask(PermissionMode.BYPASS.value) is True
    assert bypass_mode_allows_ask(PermissionMode.DEFAULT.value) is False
    assert bypass_mode_allows_ask(PermissionMode.ACCEPT_EDITS.value) is False
    assert bypass_mode_allows_ask(None) is False


def test_allow_and_deny_are_unaffected_by_allow_ask_flag():
    assert noninteractive_response("allow", allow_ask=False) == "allow"
    assert noninteractive_response("allow", allow_ask=True) == "allow"
    assert noninteractive_response("deny", allow_ask=True) == "deny"


@pytest.mark.parametrize(
    "tool_name,args",
    [
        ("WriteFile", {"file_path": "sneaky.txt", "content": "x"}),
        ("Bash", {"command": "echo hi > sneaky.txt"}),
    ],
)
def test_ask_tools_are_not_silently_allowed(tool_name: str, args: dict):
    """默认 -p：ASK 必须映射成 deny，不能被静默放行。"""
    checker = _checker("Bash:*:ask" if tool_name == "Bash" else "WriteFile:*:ask")
    effect = _decide(checker, tool_name, args)
    assert effect == "ask"
    assert noninteractive_response(effect, allow_ask=False) == "deny"


def test_cli_source_does_not_hardcode_allow():
    """守卫：防止以后又把 -p 的 PermissionRequest 写成无条件 ALLOW。"""
    text = (REPO_ROOT / "aieb" / "__main__.py").read_text(encoding="utf-8")
    assert "自动批准所有权限请求" not in text
    assert "noninteractive_response" in text
