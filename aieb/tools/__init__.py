from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

from aieb.tools.base import Tool
from aieb import env_compat

if TYPE_CHECKING:
    from aieb.cache import FileCache


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}
        self._disabled: set[str] = set()
        self._discovered: set[str] = set()

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def set_work_dir(self, work_dir: str | os.PathLike[str] | None) -> None:
        """把注册表里所有工具的 workspace 根指向同一个 canonical 路径。

        工具的根分散在三种形态上：
          * Grep / Glob：``root``
          * Bash：``work_dir``
          * ReadFile / WriteFile / EditFile：``_resolver``（WorkspaceResolver）
        切换 worktree 时必须三者一起改，否则会出现
        Agent.work_dir 指向 worktree、但工具仍按主仓库解路径的分裂状态。
        """
        from aieb.tools.workspace import WorkspaceResolver

        target = str(work_dir) if work_dir is not None else None
        for tool in self._tools.values():
            if hasattr(tool, "root"):
                tool.root = target  # type: ignore[attr-defined]
            if hasattr(tool, "work_dir"):
                tool.work_dir = target  # type: ignore[attr-defined]
            if hasattr(tool, "_resolver"):
                tool._resolver = WorkspaceResolver(target) if target else None  # type: ignore[attr-defined]

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)


    def is_enabled(self, name: str) -> bool:
        return name in self._tools and name not in self._disabled

    def enable(self, name: str) -> None:
        self._disabled.discard(name)


    def disable(self, name: str) -> None:
        if name in self._tools:
            self._disabled.add(name)

    def enable_all(self) -> None:
        self._disabled.clear()


    def mark_discovered(self, name: str) -> None:
        self._discovered.add(name)

    def is_discovered(self, name: str) -> bool:
        return name in self._discovered


    def get_deferred_tool_names(self) -> list[str]:
        return [
            name
            for name, tool in self._tools.items()
            if getattr(tool, "should_defer", False)
            and name not in self._discovered
            and name not in self._disabled
        ]

    def search_deferred(
        self, query: str, max_results: int, protocol: str = "anthropic"
    ) -> list[dict[str, Any]]:
        query_lower = query.lower()
        scored: list[tuple[int, str, Tool]] = []
        for name, tool in self._tools.items():
            if not getattr(tool, "should_defer", False):
                continue
            if name in self._disabled:
                continue
            score = 0
            name_lower = name.lower()
            desc_lower = (tool.description or "").lower()
            if query_lower in name_lower:
                score += 10
            if query_lower in desc_lower:
                score += 5
            for word in query_lower.split():
                if word in name_lower:
                    score += 3
                if word in desc_lower:
                    score += 1
            if score > 0:
                scored.append((score, name, tool))
        scored.sort(key=lambda x: x[0], reverse=True)
        results: list[dict[str, Any]] = []
        for _, _name, tool in scored[:max_results]:
            base = tool.get_schema()
            if protocol in ("openai", "openai-compat"):
                results.append({
                    "type": "function",
                    "name": base["name"],
                    "description": base["description"],
                    "parameters": base["input_schema"],
                })
            else:
                results.append(base)
        return results

    def find_deferred_by_names(
        self, names: list[str], protocol: str = "anthropic"
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for name in names:
            tool = self._tools.get(name)
            if tool is None:
                continue
            if not getattr(tool, "should_defer", False):
                continue
            base = tool.get_schema()
            if protocol in ("openai", "openai-compat"):
                results.append({
                    "type": "function",
                    "name": base["name"],
                    "description": base["description"],
                    "parameters": base["input_schema"],
                })
            else:
                results.append(base)
        return results

    def list_tools(self) -> list[Tool]:
        return list(self._tools.values())


    def get_all_schemas(self, protocol: str = "anthropic") -> list[dict[str, Any]]:
        schemas: list[dict[str, Any]] = []
        for name, tool in self._tools.items():
            if name in self._disabled:
                continue
            if getattr(tool, "should_defer", False) and name not in self._discovered:
                continue
            base = tool.get_schema()
            if protocol in ("openai", "openai-compat"):
                schemas.append({
                    "type": "function",
                    "name": base["name"],
                    "description": base["description"],
                    "parameters": base["input_schema"],
                })
            else:
                schemas.append(base)
        return schemas


# 探索型工具：它们最大的误用方式是“没有明确目标就先看一眼工作区”（ls / dir / **/*）。
# 默认不在首轮 schema 里暴露，模型确实需要时用 ToolSearch 加载（按 capability family）。
WORKSPACE_EXPLORE_TOOLS: tuple[str, ...] = ("Bash", "Glob")


def lazy_explore_enabled() -> bool:
    """是否启用“探索型工具按需加载”。可用 AIEB_LAZY_EXPLORE_TOOLS=0 关闭。

    历史名 WENDREAM_LAZY_EXPLORE_TOOLS 仍可用。
    """
    raw = env_compat.get("LAZY_EXPLORE_TOOLS", "1").strip().lower()
    return raw not in ("0", "false", "no", "off")


def reset_per_turn_budgets(registry: "ToolRegistry | None") -> None:
    """重置”每轮“配额：联网工具的次数预算与去重集合。

    为什么不放在 Agent 循环里：这是**工具层的成本控制**，与 Agent 的决策无关。
    在用户每次发消息时调用（Web 与 TUI 共用），不改变任何执行语义。
    """
    if registry is None:
        return
    for name in ("WebSearch", "WebFetch"):
        tool = registry.get(name)
        begin = getattr(tool, "begin_turn", None)
        if callable(begin):
            try:
                begin()
            except Exception:  # noqa: BLE001 - 预算重置失败不影响主流程
                continue


def create_default_registry(
    file_cache: FileCache | None = None,
    file_history: Any = None,
    work_dir: str | None = None,
    restrict_bash_to_workspace: bool = False,
    lazy_explore_tools: bool | None = None,
    web_search_config: Any = None,
    llm_providers: Any = None,
) -> ToolRegistry:
    from aieb.tools.bash import Bash
    from aieb.tools.edit_file import EditFile
    from aieb.tools.file_state_cache import FileStateCache
    from aieb.tools.glob import Glob
    from aieb.tools.grep import Grep
    from aieb.tools.impl.tool_search import ToolSearchTool
    from aieb.tools.read_file import ReadFile
    from aieb.tools.write_file import WriteFile

    if lazy_explore_tools is None:
        lazy_explore_tools = lazy_explore_enabled()

    file_state_cache = FileStateCache()

    registry = ToolRegistry()
    registry.register(
        ReadFile(file_cache=file_cache, file_state_cache=file_state_cache, work_dir=work_dir)
    )
    registry.register(
        WriteFile(
            file_cache=file_cache,
            file_history=file_history,
            file_state_cache=file_state_cache,
            work_dir=work_dir,
        )
    )
    registry.register(
        EditFile(
            file_cache=file_cache,
            file_history=file_history,
            file_state_cache=file_state_cache,
            work_dir=work_dir,
        )
    )
    registry.register(Bash())
    # 搜索类工具的相对路径必须相对 workspace 解析，而不是进程 cwd。
    registry.register(Glob(root=work_dir))
    registry.register(Grep(root=work_dir))
    # 命令执行同样必须落在 workspace 内，否则相对路径、以及从 cwd 派生的
    # 读取范围都会越界到宿主仓库（Benchmark 下会读到 cases/ 等评测文件）。
    if work_dir:
        bash_tool = registry.get("Bash")
        if bash_tool is not None:
            bash_tool.work_dir = str(work_dir)
            # 高约束环境（例如 Benchmark 隔离）可打开启发式越界预检。
            # 生产默认 False：不限制用户正常调用系统命令。
            bash_tool.restrict_to_workspace = bool(restrict_bash_to_workspace)

    # 按需加载：探索型工具默认不出现在首轮 schema 中，需要时通过 ToolSearch 加载。
    # 读/写/搜索（ReadFile / WriteFile / EditFile / Grep）保持直接可用，
    # 避免“每个文件任务都多一次 ToolSearch”的反效果。
    registry.register(ToolSearchTool(registry))
    if lazy_explore_tools:
        for name in WORKSPACE_EXPLORE_TOOLS:
            tool = registry.get(name)
            if tool is not None:
                tool.should_defer = True  # type: ignore[attr-defined]

    # 联网工具：真正注册进 Schema，模型才能调用（而不是只在提示词里声称能联网）。
    # 关闭时**不注册**，避免模型看到工具却每次都失败；
    # provider=tavily 但缺 Key 时仍注册，由工具返回明确的配置错误。
    if web_search_config is not None and getattr(web_search_config, "enabled", True):
        from aieb.tools.web_fetch import WebFetch
        from aieb.tools.web_search import build_web_search_tool

        registry.register(build_web_search_tool(web_search_config, llm_providers))
        registry.register(
            WebFetch(
                timeout=float(getattr(web_search_config, "timeout", 20.0) or 20.0),
                max_fetches_per_turn=int(
                    getattr(web_search_config, "max_fetches_per_turn", 6) or 6
                ),
            )
        )
    return registry
