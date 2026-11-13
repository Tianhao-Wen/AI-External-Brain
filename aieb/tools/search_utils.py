"""文件搜索的公共基础设施（Grep / Glob 共用）。

解决三个通用问题：
1. 相对路径必须解析到 **调用方配置的 root（workspace/sandbox）**，而不是进程 cwd；
2. 遍历前就跳过高成本目录（node_modules/.git/.conda/build/...），而不是走完再过滤；
3. 搜索必须有边界（文件数/字节数/匹配数/时间预算），并且可以被 asyncio 取消。
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from aieb.tools.workspace import WorkspaceResolver

# 通用默认排除目录（按名称匹配，任意层级生效）
DEFAULT_EXCLUDE_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "node_modules",
        ".venv",
        "venv",
        "env",
        "__pycache__",
        ".tox",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".conda",
        "conda-meta",
        "dist",
        "build",
        "target",
        "coverage",
        "htmlcov",
        ".next",
        ".nuxt",
        ".cache",
        ".idea",
        ".vs",
        "site-packages",
    }
)

# 相对 root 的路径前缀排除（产物目录，体积大且无检索价值）
DEFAULT_EXCLUDE_REL_PREFIXES: tuple[str, ...] = (
    "benchmark/results",
    "benchmark/workspaces",
    "benchmark/tool_bench/fixtures",
)

MAX_FILES = 20_000
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_MATCHES = 200
MAX_GLOB_RESULTS = 500
MAX_GLOB_CANDIDATES = 20_000
MAX_OUTPUT_CHARS = 100_000
DEFAULT_TIME_BUDGET = 20.0
BINARY_SNIFF_BYTES = 8192
CANCEL_YIELD_EVERY = 200


@dataclass
class SearchStats:
    files_scanned: int = 0
    directories_scanned: int = 0
    bytes_scanned: int = 0
    matches: int = 0
    files_seen: int = 0
    truncated: bool = False
    stopped_reason: str = ""
    elapsed_seconds: float = 0.0

    def as_dict(self) -> dict:
        return {
            "files_scanned": self.files_scanned,
            "directories_scanned": self.directories_scanned,
            "bytes_scanned": self.bytes_scanned,
            "matches": self.matches,
            "truncated": self.truncated,
            "stopped_reason": self.stopped_reason,
            "elapsed_seconds": round(self.elapsed_seconds, 3),
        }


def resolve_search_root(raw_path: str, root: str | Path | None) -> tuple[Path | None, str]:
    """把用户给的 path 解析成安全的搜索根目录。

    - 相对路径相对 **root**（workspace/sandbox），不是进程 cwd；
    - resolve 之后必须仍在 root 内，否则拒绝（阻止 ../ 逃逸）；
    - 路径语义与 ReadFile/WriteFile/EditFile/Bash 统一由 WorkspaceResolver 负责。
    """
    if root:
        resolver = WorkspaceResolver(root)
        base_root = resolver.root
        resolved, error = resolver.try_resolve(raw_path or ".")
        if resolved is None:
            return None, f"Error: path '{raw_path}' escapes the workspace root ({base_root})"
        candidate = resolved
    else:
        base_root = Path.cwd().resolve()
        candidate = Path(raw_path or ".")
        if not candidate.is_absolute():
            candidate = base_root / candidate
        try:
            candidate = candidate.resolve()
        except OSError as exc:  # pragma: no cover - 保留旧错误面
            return None, f"Error: cannot resolve path '{raw_path}': {exc}"
        try:
            candidate.relative_to(base_root)
        except ValueError:
            return None, f"Error: path '{raw_path}' escapes the workspace root ({base_root})"

    if not candidate.exists():
        return None, f"Error: path not found: {raw_path}"
    if not candidate.is_dir():
        return None, f"Error: path is not a directory: {raw_path}"
    return candidate, ""


def _excluded(rel_posix: str, exclude_dirs: frozenset[str], exclude_rel_prefixes: tuple[str, ...]) -> bool:
    if rel_posix:
        for prefix in exclude_rel_prefixes:
            if rel_posix == prefix or rel_posix.startswith(prefix + "/"):
                return True
    return False


def _is_link_like(entry: os.DirEntry) -> bool:
    """判断 entry 是否是链接类文件系统对象（symlink / Windows junction）。

    Windows 的 junction 是 reparse point 而非 symlink：``is_dir(follow_symlinks=False)``
    对它返回 True，一旦递归进去就会穿过 workspace 边界（且可能形成环路）。
    因此必须显式用 ``is_junction()`` 排除。
    """
    try:
        if entry.is_symlink():
            return True
    except OSError:
        return True
    is_junction = getattr(entry, "is_junction", None)
    if is_junction is not None:
        try:
            if is_junction():
                return True
        except OSError:
            return True
    return False


def iter_files(
    root: Path,
    stats: SearchStats,
    *,
    deadline: float,
    exclude_dirs: frozenset[str] = DEFAULT_EXCLUDE_DIRS,
    exclude_rel_prefixes: tuple[str, ...] = DEFAULT_EXCLUDE_REL_PREFIXES,
    max_files: int = MAX_FILES,
):
    """迭代 root 下的普通文件（不跟随 symlink/junction，跳过高成本目录）。"""
    stack: list[Path] = [root]
    while stack:
        current = stack.pop()
        stats.directories_scanned += 1
        try:
            entries = list(os.scandir(current))
        except OSError:
            continue
        for entry in entries:
            if stats.files_seen >= max_files:
                stats.truncated = True
                stats.stopped_reason = "max_files"
                return
            if time.monotonic() > deadline:
                stats.truncated = True
                stats.stopped_reason = "time_budget"
                return
            if _is_link_like(entry):
                continue
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
                is_file = entry.is_file(follow_symlinks=False)
            except OSError:
                continue
            if is_dir:
                if entry.name in exclude_dirs:
                    continue
                try:
                    rel = Path(entry.path).relative_to(root).as_posix()
                except ValueError:
                    continue
                if _excluded(rel, exclude_dirs, exclude_rel_prefixes):
                    continue
                stack.append(Path(entry.path))
            elif is_file:
                try:
                    rel = Path(entry.path).relative_to(root).as_posix()
                except ValueError:
                    continue
                if _excluded(rel, exclude_dirs, exclude_rel_prefixes):
                    continue
                stats.files_seen += 1
                yield Path(entry.path)


def read_text_bounded(path: Path, stats: SearchStats) -> str | None:
    """读取文本文件：限制单文件字节数，跳过明显的二进制文件。"""
    try:
        with open(path, "rb") as fh:
            head = fh.read(BINARY_SNIFF_BYTES)
            if b"\x00" in head:
                return None
            rest = fh.read(MAX_FILE_BYTES - len(head))
    except OSError:
        return None
    raw = head + rest
    stats.bytes_scanned += len(raw)
    return raw.decode("utf-8", errors="ignore")


@dataclass
class OutputBudget:
    max_chars: int = MAX_OUTPUT_CHARS
    used: int = 0

    def append(self, line: str) -> bool:
        cost = len(line) + 1
        if self.used + cost > self.max_chars:
            return False
        self.used += cost
        return True


def truncation_note(stats: SearchStats) -> str:
    if not stats.truncated:
        return ""
    reason = stats.stopped_reason or "limit"
    return (
        f"\n[truncated: {reason}; files_scanned={stats.files_scanned} "
        f"dirs={stats.directories_scanned} matches={stats.matches}]"
    )
