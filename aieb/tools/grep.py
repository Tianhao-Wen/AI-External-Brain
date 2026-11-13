
from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path

from pydantic import BaseModel, Field

from aieb.tools.base import Tool, ToolResult
from aieb.tools.search_utils import (
    CANCEL_YIELD_EVERY,
    DEFAULT_TIME_BUDGET,
    MAX_MATCHES,
    OutputBudget,
    SearchStats,
    iter_files,
    read_text_bounded,
    resolve_search_root,
    truncation_note,
)


class Params(BaseModel):
    pattern: str = Field(description="Regex pattern to search for")
    path: str = Field(default=".", description="Base directory to search from (relative to workspace)")
    include: str = Field(default="", description="Glob filter for filenames (e.g. '*.py')")


class Grep(Tool):
    name = "Grep"
    description = "Search file contents using a regex pattern, returning file:line:content matches."
    params_model = Params
    category = "read"
    is_concurrency_safe = True


    def __init__(self, root: str | Path | None = None) -> None:
        # root 是 workspace/sandbox 根目录；相对 path 一律相对它解析，而不是进程 cwd。
        self.root = str(root) if root is not None else None
        self.last_stats: dict = {}

    async def execute(self, params: Params) -> ToolResult:
        started = time.monotonic()
        stats = SearchStats()

        base, error = resolve_search_root(params.path, self.root)
        if base is None:
            self.last_stats = stats.as_dict()
            return ToolResult(output=error, is_error=True)

        try:
            regex = re.compile(params.pattern)
        except re.error as e:
            stats.elapsed_seconds = time.monotonic() - started
            self.last_stats = stats.as_dict()
            return ToolResult(output=f"Error: invalid regex: {e}", is_error=True)

        include = params.include.strip()
        deadline = started + DEFAULT_TIME_BUDGET
        results: list[str] = []
        budget = OutputBudget()

        for index, file_path in enumerate(iter_files(base, stats, deadline=deadline), start=1):
            if index % CANCEL_YIELD_EVERY == 0:
                # 让出控制权，确保 asyncio.wait_for 能真正取消长搜索
                await asyncio.sleep(0)
            if include and not file_path.match(include):
                continue
            text = read_text_bounded(file_path, stats)
            if text is None:
                continue
            stats.files_scanned += 1
            try:
                rel = file_path.relative_to(base).as_posix()
            except ValueError:
                rel = file_path.as_posix()
            for line_num, line in enumerate(text.splitlines(), 1):
                if regex.search(line):
                    stats.matches += 1
                    if stats.matches > MAX_MATCHES:
                        stats.truncated = True
                        stats.stopped_reason = "max_matches"
                        break
                    if not budget.append(f"{rel}:{line_num}:{line}"):
                        stats.truncated = True
                        stats.stopped_reason = "max_output_bytes"
                        break
                    results.append(f"{rel}:{line_num}:{line}")
            if stats.stopped_reason in ("max_matches", "max_output_bytes"):
                break

        stats.elapsed_seconds = time.monotonic() - started
        self.last_stats = stats.as_dict()

        # 完整性元数据：只回答“本次搜索是否完整执行”，不判断任务是否完成。
        metadata = {
            "matches_count": stats.matches,
            "truncated": bool(stats.truncated),
            "search_complete": not stats.truncated,
            "stopped_reason": stats.stopped_reason,
            "files_scanned": stats.files_scanned,
            "elapsed_seconds": round(stats.elapsed_seconds, 3),
        }

        if not results:
            return ToolResult(
                output="No matches found." + truncation_note(stats), metadata=metadata
            )
        return ToolResult(output="\n".join(results) + truncation_note(stats), metadata=metadata)

