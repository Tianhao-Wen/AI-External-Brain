
"""OS 级沙箱：限制 Bash 命令的文件写入和网络访问。

macOS 使用 sandbox-exec（Seatbelt），Linux 使用 bubblewrap（bwrap）。
与 permissions/sandbox.py 中的 PathSandbox（路径级权限检查）不同，
这里是操作系统层面的强制隔离——即使命令绕过了路径检查，
内核也会阻止越权的写操作。
"""

from __future__ import annotations

import platform
from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class SandboxConfig:
    """沙箱配置：控制可写路径、禁写路径和网络访问。"""

    # 允许写入的路径列表（白名单）
    allow_write: list[str] = field(default_factory=list)
    # 强制只读的路径列表（优先级高于 allow_write）
    deny_write: list[str] = field(default_factory=list)
    # 是否允许网络访问
    network_enabled: bool = False


class Sandbox(ABC):
    """沙箱抽象基类，各平台实现 wrap() 和 available()。"""

    @abstractmethod
    def wrap(self, command: str, config: SandboxConfig) -> str:
        """将原始命令包装为沙箱内执行的命令字符串。"""
        ...

    @abstractmethod
    def available(self) -> bool:
        """检测当前环境是否支持该沙箱。"""
        ...


def create_sandbox() -> Sandbox | None:
    """根据操作系统自动选择沙箱实现。

    macOS -> SeatbeltSandbox（基于 sandbox-exec）
    Linux -> BwrapSandbox（基于 bubblewrap）
    其他系统（包括 Windows）-> None：**没有 OS 级隔离**，
    此时只能依赖 permissions 层的权限规则与路径校验。
    """
    system = platform.system()
    if system == "Darwin":
        from .seatbelt import SeatbeltSandbox
        return SeatbeltSandbox()
    elif system == "Linux":
        from .bwrap import BwrapSandbox
        return BwrapSandbox()
    return None


def sandbox_support_summary() -> str:
    """返回当前平台实际可用的隔离手段描述（用于 UI / 文档，不夸大）。"""
    system = platform.system()
    if system == "Darwin":
        return "OS-level sandbox: macOS Seatbelt (sandbox-exec)"
    if system == "Linux":
        return "OS-level sandbox: Linux bubblewrap (bwrap)"
    return (
        f"No OS-level sandbox on {system}. Enforcement relies on permission rules "
        "and filesystem path validation only."
    )
