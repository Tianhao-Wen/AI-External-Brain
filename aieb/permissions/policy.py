"""非交互（-p / print）模式下的权限策略。

核心原则：**ASK ≠ ALLOW**。
非交互模式没有人在终端里确认，因此需要确认的决策必须默认拒绝；
只有用户显式进入危险模式（PermissionMode.BYPASS）时才允许。
"""

from __future__ import annotations

from aieb.permissions.modes import DecisionEffect


def noninteractive_response(
    decision_effect: DecisionEffect, allow_ask: bool = False
) -> str:
    """把权限决策映射为非交互模式要回复的处理结果。

    * allow  -> "allow"
    * deny   -> "deny"
    * ask    -> allow_ask 时 "allow"，否则 "deny"
    """
    if decision_effect == "allow":
        return "allow"
    if decision_effect == "ask":
        return "allow" if allow_ask else "deny"
    return "deny"


def bypass_mode_allows_ask(mode_value: str | None) -> bool:
    """只有显式 ``bypassPermissions`` 才算“用户授权自动放行 ASK”。"""
    return (mode_value or "") == "bypassPermissions"
