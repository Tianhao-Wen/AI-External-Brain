
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from aieb.tools.base import Tool, ToolResult
from aieb.tools.workspace import WorkspaceResolver

if TYPE_CHECKING:
    from aieb.cache import FileCache
    from aieb.tools.file_state_cache import FileStateCache


class Params(BaseModel):
    file_path: str = Field(description="Absolute or relative path to the file to read")
    offset: int = Field(default=0, description="Line offset to start reading from (0-based)")
    limit: int = Field(default=2000, description="Maximum number of lines to read")


class ReadFile(Tool):
    name = "ReadFile"
    description = "Read a file and return its contents with line numbers."
    params_model = Params
    category = "read"
    is_concurrency_safe = True


    def __init__(
        self,
        file_cache: FileCache | None = None,
        file_state_cache: FileStateCache | None = None,
        work_dir: str | None = None,
    ) -> None:
        self._cache = file_cache
        self._state_cache = file_state_cache
        # work_dir 为 None 时保持旧行为（相对进程 cwd），供未配置 workspace 的调用方使用
        self._resolver = WorkspaceResolver(work_dir) if work_dir else None


    def _resolve(self, raw: str) -> tuple[Path | None, str]:
        if self._resolver is None:
            return Path(raw), ""
        return self._resolver.try_resolve(raw)


    async def execute(self, params: Params) -> ToolResult:
        path, error = self._resolve(params.file_path)
        if path is None:
            return ToolResult(output=error, is_error=True)
        if not path.exists():
            return ToolResult(output=f"Error: file not found: {params.file_path}", is_error=True)
        if not path.is_file():
            return ToolResult(output=f"Error: not a file: {params.file_path}", is_error=True)

        resolved = str(path.resolve())

        try:
            text = self._cache.get(resolved) if self._cache else None
            if text is None:
                text = path.read_text(encoding="utf-8")
                if self._cache:
                    self._cache.put(resolved, text)
        except Exception as e:
            return ToolResult(output=f"Error reading file: {e}", is_error=True)

        if self._state_cache:
            try:
                mtime_ns = path.stat().st_mtime_ns
                self._state_cache.record(resolved, text, mtime_ns)
            except OSError:
                pass

        lines = text.splitlines()
        selected = lines[params.offset : params.offset + params.limit]
        numbered = [f"{i + params.offset + 1}\t{line}" for i, line in enumerate(selected)]
        # 完整性元数据：只说明“本次读取是否覆盖到文件末尾”。
        end_index = params.offset + len(selected)
        metadata = {
            "total_lines": len(lines),
            "returned_lines": len(selected),
            "offset": params.offset,
            "truncated": end_index < len(lines),
            "read_complete": end_index >= len(lines),
        }
        return ToolResult(output="\n".join(numbered), metadata=metadata)
