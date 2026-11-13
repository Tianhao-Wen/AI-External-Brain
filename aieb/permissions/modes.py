
from __future__ import annotations

from enum import Enum
from typing import Literal

from aieb.tools.base import ToolCategory


DecisionEffect = Literal["allow", "deny", "ask"]


class PermissionMode(str, Enum):
    DEFAULT = "default"
    ACCEPT_EDITS = "acceptEdits"
    PLAN = "plan"
    BYPASS = "bypassPermissions"


_MODE_MATRIX: dict[PermissionMode, dict[ToolCategory, DecisionEffect]] = {
    # 默认策略：最少打扰。普通读取/写入和常规命令直接放行，真正危险的
    # 命令由 DangerousCommandDetector 在更早层拦截，并返回 deny，而不是 ask。
    PermissionMode.DEFAULT: {"read": "allow", "write": "allow", "command": "allow"},
    PermissionMode.ACCEPT_EDITS: {"read": "allow", "write": "allow", "command": "allow"},
    PermissionMode.PLAN: {"read": "allow", "write": "ask", "command": "ask"},
    PermissionMode.BYPASS: {"read": "allow", "write": "allow", "command": "allow"},
}


def mode_decide(mode: PermissionMode, category: ToolCategory) -> DecisionEffect:
    return _MODE_MATRIX[mode][category]
