"""SaveMemory —— 受控的长期记忆保存入口（面向 Agent 的结构化操作）。

## 为什么需要它

以前"记住我的偏好"走的是通用文件工具：模型被提示词要求用 ``WriteFile`` 创建
markdown 文件、再手工往索引里加一行。这会带来三个真实问题：

1. 用户级记忆目录在工作区之外，``WorkspaceResolver`` 直接拒绝写盘，
   ``PathSandbox`` 也会把它判成越界，于是每次写/读都弹一次权限确认；
2. 每个记忆文件是一条不同的路径，"允许一次"只覆盖那一个文件，下一个文件继续弹；
3. 模型把 ``.aieb/memory``、``C:\\Users\\...``、``MEMORY.md`` 这些内部
   细节当成"操作结果"讲给用户，普通用户完全不需要知道。

这个工具把"保存记忆"收敛为一次没有路径参数的结构化调用：

* 参数只有内容 / 范围 / 标题（可选）；
* 落盘位置由 :class:`~aieb.memory.auto_memory.MemoryManager` 决定，
  永远在受管记忆目录内，因此不需要路径沙箱授权，也不可能被用来读写别的文件；
* 保存结果由服务端返回，模型据此决定能否说"已经记住了"。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field

from aieb.tools.base import Tool, ToolResult

if TYPE_CHECKING:
    from aieb.memory.auto_memory import MemoryManager


class Params(BaseModel):
    content: str = Field(
        min_length=1,
        description=(
            "The memory to store, written as a self-contained statement. "
            "Do not include file paths or tool names."
        ),
    )
    scope: Literal["user", "project"] = Field(
        default="user",
        description=(
            "user = long-term preferences about the person (follows them across "
            "projects); project = facts that only apply to the current project. "
            "When unsure, use user."
        ),
    )
    title: str = Field(
        default="",
        description=(
            "Optional short label (a few words). Reusing an existing title updates "
            "that memory instead of creating another one."
        ),
    )
    memory_type: str = Field(
        default="",
        description="Optional: user | feedback | project | reference.",
    )


class SaveMemory(Tool):
    name = "SaveMemory"
    description = (
        "Save something to long-term memory so future conversations can use it. "
        "Use this whenever the user asks you to remember a preference or states a "
        "durable fact about themselves or the project. Storage, indexing and "
        "provenance are handled for you — never create memory files with "
        "WriteFile/EditFile/Bash. Returns a status indicating whether the memory "
        "was actually saved; only claim you remembered something when it succeeded."
    )
    params_model = Params
    # 归类为 write（它会改变持久状态），但权限层对受管记忆工具走专门分支：
    # 没有路径参数，写入范围由服务端限定。
    category = "write"

    def __init__(self, memory_manager: "MemoryManager | None" = None) -> None:
        self.memory_manager = memory_manager
        # 由 Agent 在每次执行前注入：记忆来源必须能归到当前会话
        self.session_id: str = ""

    def set_session_id(self, session_id: str) -> None:
        self.session_id = session_id or ""

    async def execute(self, params: Params) -> ToolResult:
        manager = self.memory_manager
        if manager is None:
            return ToolResult(
                output=(
                    "status: failed\nok: false\n"
                    "detail: 记忆功能未初始化\n"
                    "Do not tell the user the information was remembered."
                ),
                is_error=True,
            )

        result = manager.save_memory(
            params.content,
            scope=params.scope,
            title=params.title,
            memory_type=params.memory_type,
            session_id=self.session_id or None,
        )
        # disabled / failed 对模型来说是"没保存成功"，用 is_error 标记，
        # 避免模型把它当成成功并回复"已记住"。
        return ToolResult(output=result.as_tool_output(), is_error=not result.ok)


def build_save_memory_tool(memory_manager: Any) -> SaveMemory:
    """工厂函数：给注册表和测试一个稳定的构造入口。"""
    return SaveMemory(memory_manager)
