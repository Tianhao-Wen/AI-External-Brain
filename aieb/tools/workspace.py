"""统一的路径解析层。

本模块只有一个职责：**把用户/模型给的路径解析成确定的绝对路径**，并保证结果
永远落在 Agent 的 ``work_dir`` 内。

它与 ``aieb.permissions.sandbox.PathSandbox`` 的分工是：

* ``WorkspaceResolver``  —— 语义（relative → work_dir、normalize、containment）
* ``PathSandbox``        —— 策略（禁写清单、额外允许根目录、ask/deny 判定）

工具执行时用 Resolver，权限判定时用 PathSandbox。两者都以同一个 ``work_dir``
为基准，因此"被检查的路径"和"实际被操作的路径"是同一个——这正是 Phase 1
之前缺失的一致性（权限层按 work_dir 判定，执行层却按进程 cwd 落盘）。

设计约束：
1. 相对路径永远相对 ``work_dir``，绝不相对进程 cwd；
2. 解析后必须仍在 ``work_dir`` 内，``..`` 不能静默逃逸；
3. 符号链接 / Windows junction 不能用来绕过边界（比较的是 realpath）；
4. Windows 上大小写不敏感、盘符不同即视为越界；
5. 不猜测、不修复：越界就是硬错误，交给上层决定提示方式。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "WorkspaceResolution",
    "WorkspaceResolver",
    "WorkspaceViolation",
    "is_within",
    "normalize_key",
]


class WorkspaceViolation(Exception):
    """路径不满足 workspace 语义时抛出。

    ``kind`` 用于让上层区分可观测原因，避免用字符串匹配判断错误类型。
    """

    def __init__(self, message: str, kind: str) -> None:
        super().__init__(message)
        self.kind = kind


@dataclass(frozen=True)
class WorkspaceResolution:
    """解析结果：`path` 是可直接用于 I/O 的绝对路径。"""

    path: Path
    raw: str
    # 是否由相对路径解析而来（便于观测/测试）
    relative: bool


def _strip_extended_prefix(text: str) -> str:
    """去掉 Windows 扩展长度前缀，使 ``\\\\?\\C:\\x`` 与 ``C:\\x`` 可比较。"""
    if text.startswith("\\\\?\\UNC\\"):
        return "\\\\" + text[len("\\\\?\\UNC\\") :]
    if text.startswith("\\\\?\\"):
        return text[len("\\\\?\\") :]
    return text


def normalize_key(path: str | os.PathLike[str]) -> str:
    """生成用于**比较**的规范化键（不用于 I/O）。"""
    text = _strip_extended_prefix(str(path))
    text = os.path.normcase(text)
    # 统一分隔符（normcase 已在 Windows 上做，这里兼容 POSIX 风格输入）
    text = text.replace("/", os.sep)
    # 去掉尾部多余分隔符，但保留盘符根 "c:\"
    if len(text) > len(os.sep) * 2 + 1 and text.endswith(os.sep):
        text = text.rstrip(os.sep)
    return text


def is_within(root: str | os.PathLike[str], target: str | os.PathLike[str]) -> bool:
    """``target`` 是否等于 ``root`` 或位于 ``root`` 之下。

    使用规范化字符串前缀比较，可以同时正确处理：
    Windows 大小写差异、``/`` 与 ``\\``、盘符不同（前缀必然不匹配）。
    """
    root_key = normalize_key(root)
    target_key = normalize_key(target)
    if target_key == root_key:
        return True
    prefix = root_key if root_key.endswith(os.sep) else root_key + os.sep
    return target_key.startswith(prefix)


class WorkspaceResolver:
    """把相对/绝对路径解析为 workspace 内的绝对路径。"""

    def __init__(self, work_dir: str | os.PathLike[str]) -> None:
        raw_root = os.fspath(work_dir)
        if not raw_root:
            raise WorkspaceViolation("work_dir 不能为空", kind="invalid_root")
        # root 自身也要 realpath：work_dir 本身可能位于 symlink/junction 之后，
        # 只有比较 realpath 才能发现"看起来在里面、实际在外面"的情况。
        self._root = Path(os.path.realpath(os.path.abspath(os.path.expanduser(raw_root))))

    @property
    def root(self) -> Path:
        return self._root

    def contains(self, path: str | os.PathLike[str]) -> bool:
        return is_within(self._root, path)

    # ------------------------------------------------------------------
    # 公开 API
    # ------------------------------------------------------------------
    def resolve(self, raw: str | os.PathLike[str] | None) -> WorkspaceResolution:
        """解析路径；越界或非法时抛 :class:`WorkspaceViolation`。"""
        if raw is None:
            raise WorkspaceViolation("path 不能为空", kind="empty")

        text = os.fspath(raw).strip()
        if not text:
            raise WorkspaceViolation("path 不能为空", kind="empty")

        candidate = Path(text).expanduser()

        # Windows 盘符相对路径（如 "C:foo"）含义依赖进程 cwd，无法安全解释
        if candidate.drive and not candidate.is_absolute():
            raise WorkspaceViolation(
                f"不支持的盘符相对路径: {text}", kind="drive_relative"
            )

        relative = not candidate.is_absolute()
        if relative:
            candidate = self._root / candidate

        # realpath: 同时完成 normalize("..") 与 symlink/junction 解析
        resolved = Path(os.path.realpath(candidate))

        if not self.contains(resolved):
            raise WorkspaceViolation(
                f"路径 '{text}' 逃逸出 workspace 边界 ({self._root})",
                kind="escape",
            )

        return WorkspaceResolution(path=resolved, raw=text, relative=relative)

    def try_resolve(self, raw: str | os.PathLike[str] | None) -> tuple[Path | None, str]:
        """``resolve`` 的非异常版本，返回 ``(path, error)``。"""
        try:
            return self.resolve(raw).path, ""
        except WorkspaceViolation as exc:
            return None, f"Error: {exc}"
