
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from aieb.tools.base import Tool, ToolResult
from aieb.tools.workspace import WorkspaceResolver

if TYPE_CHECKING:
    from aieb.cache import FileCache
    from aieb.tools.file_state_cache import FileStateCache


class Params(BaseModel):
    file_path: str = Field(description="Path to the file to write")
    content: str = Field(description="Content to write to the file")


class WriteFile(Tool):
    name = "WriteFile"
    description = (
        "Write content to a file, creating parent directories if needed. Overwrites existing files.\n"
        "You MUST read existing files with ReadFile before overwriting them. This tool will fail otherwise."
    )
    params_model = Params
    category = "write"


    def __init__(
        self,
        file_cache: FileCache | None = None,
        file_history: Any = None,
        file_state_cache: FileStateCache | None = None,
        work_dir: str | None = None,
    ) -> None:
        self._cache = file_cache
        self.file_history = file_history
        self._state_cache = file_state_cache
        # work_dir 为 None 时保持旧行为（相对进程 cwd）
        self._resolver = WorkspaceResolver(work_dir) if work_dir else None


    def _resolve(self, raw: str) -> tuple[Path | None, str]:
        if self._resolver is None:
            return Path(raw), ""
        return self._resolver.try_resolve(raw)


    async def execute(self, params: Params) -> ToolResult:
        path, error = self._resolve(params.file_path)
        if path is None:
            return ToolResult(output=error, is_error=True)

        # 备份必须针对**实际被写入的**路径，否则 undo 会指向另一个文件
        if self.file_history is not None:
            self.file_history.track_edit(str(path))

        if self._state_cache and path.exists():
            resolved = str(path.resolve())
            ok, err_msg = self._state_cache.check(resolved)
            if not ok:
                return ToolResult(output=err_msg, is_error=True)

        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(params.content, encoding="utf-8")
            if self._cache:
                self._cache.invalidate(str(path.resolve()))
            if self._state_cache:
                self._state_cache.update(str(path.resolve()))
        except Exception as e:
            return ToolResult(output=f"Error writing file: {e}", is_error=True)
        return ToolResult(output=f"Successfully wrote to {params.file_path}")
