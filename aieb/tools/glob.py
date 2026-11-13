
from __future__ import annotations

import asyncio
import fnmatch
import time
from pathlib import Path

from pydantic import BaseModel, Field

from aieb.tools.base import Tool, ToolResult
from aieb.tools.search_utils import (
    CANCEL_YIELD_EVERY,
    DEFAULT_TIME_BUDGET,
    MAX_GLOB_RESULTS,
    SearchStats,
    iter_files,
    resolve_search_root,
    truncation_note,
)


class Params(BaseModel):
    pattern: str = Field(description="Glob pattern to match (e.g. '**/*.py')")
    path: str = Field(default=".", description="Base directory to search from (relative to workspace)")


def _glob_match(rel_posix: str, pattern: str) -> bool:
    """与 pathlib.Path.glob 语义对齐的匹配。

    pathlib 里 ``**/`` 可以匹配 **零层或多层** 目录，而 fnmatch 把 ``**``
    当成普通的 ``*``（要求至少一层）。这里补上零层的分支。
    """
    if fnmatch.fnmatch(rel_posix, pattern):
        return True
    if pattern.startswith("**/"):
        return fnmatch.fnmatch(rel_posix, pattern[3:])
    return False


class Glob(Tool):
    name = "Glob"
    description = "Find files matching a glob pattern, returning relative paths."
    params_model = Params
    category = "read"
    is_concurrency_safe = True


    def __init__(self, root: str | Path | None = None) -> None:
        self.root = str(root) if root is not None else None
        self.last_stats: dict = {}

    async def execute(self, params: Params) -> ToolResult:
        started = time.monotonic()
        stats = SearchStats()

        base, error = resolve_search_root(params.path, self.root)
        if base is None:
            self.last_stats = stats.as_dict()
            return ToolResult(output=error, is_error=True)

        pattern = (params.pattern or "**/*").strip()
        match_all = pattern in ("*", "**", "**/*")

        candidates: list[tuple[float, str]] = []
        try:
            for index, file_path in enumerate(
                iter_files(base, stats, deadline=started + DEFAULT_TIME_BUDGET), start=1
            ):
                if index % CANCEL_YIELD_EVERY == 0:
                    await asyncio.sleep(0)
                try:
                    rel = file_path.relative_to(base).as_posix()
                except ValueError:
                    continue
                if match_all or _glob_match(rel, pattern):
                    try:
                        mtime = file_path.stat().st_mtime
                    except OSError:
                        mtime = 0.0
                    candidates.append((mtime, rel))
                    stats.files_scanned += 1
                    if len(candidates) >= MAX_GLOB_RESULTS * 10:
                        stats.truncated = True
                        stats.stopped_reason = "max_candidates"
                        break
        except OSError as e:  # pragma: no cover
            stats.elapsed_seconds = time.monotonic() - started
            self.last_stats = stats.as_dict()
            return ToolResult(output=f"Error: {e}", is_error=True)

        candidates.sort(key=lambda item: item[0], reverse=True)
        if len(candidates) > MAX_GLOB_RESULTS:
            stats.truncated = True
            stats.stopped_reason = stats.stopped_reason or "max_results"
            candidates = candidates[:MAX_GLOB_RESULTS]

        matches = [rel for _, rel in candidates]
        stats.matches = len(matches)
        stats.elapsed_seconds = time.monotonic() - started
        self.last_stats = stats.as_dict()

        metadata = {
            "matches_count": len(matches),
            "truncated": bool(stats.truncated),
            "search_complete": not stats.truncated,
            "stopped_reason": stats.stopped_reason,
            "files_scanned": stats.files_scanned,
            "elapsed_seconds": round(stats.elapsed_seconds, 3),
        }

        if not matches:
            return ToolResult(
                output="No files matched the pattern." + truncation_note(stats), metadata=metadata
            )
        return ToolResult(output="\n".join(matches) + truncation_note(stats), metadata=metadata)

