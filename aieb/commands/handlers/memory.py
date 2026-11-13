
from __future__ import annotations

from aieb.commands.registry import Command, CommandContext, CommandType


async def handle_memory(ctx: CommandContext) -> None:
    mm = ctx.memory_manager
    if mm is None:
        ctx.ui.add_system_message("记忆管理器未初始化")
        return


    parts = ctx.args.split(None, 1)
    sub = parts[0] if parts else ""

    if sub == "":
        ctx.ui.add_system_message(mm.get_display_text())

    elif sub == "list":
        lines = mm.get_memories_with_sources()
        if not lines:
            ctx.ui.add_system_message("当前没有任何自动记忆。")
        else:
            body = "\n".join(f"  {line}" for line in lines)
            ctx.ui.add_system_message(
                f"记忆列表：\n{body}\n\n"
                f"  用户级目录: {mm.user_mem_dir}\n"
                f"  项目级目录: {mm.project_mem_dir}\n"
                f"  删除单条：/memory delete <文件名>"
            )

    elif sub == "clear":
        mm.clear()
        ctx.ui.add_system_message("所有自动记忆已清空。")

    elif sub == "delete":
        target = parts[1].strip() if len(parts) > 1 else ""
        if not target:
            ctx.ui.add_system_message("用法: /memory delete <文件名>（文件名可先通过 /memory list 查看）")
            return
        ok, message = mm.delete(target)
        ctx.ui.add_system_message(message)

    elif sub == "edit":
        ctx.ui.add_system_message(
            f"编辑记忆文件：\n"
            f"  用户级目录: {mm.user_mem_dir}\n"
            f"  项目级目录: {mm.project_mem_dir}"
        )

    else:
        ctx.ui.add_system_message(
            "用法: /memory [list | delete <文件名> | clear | edit]"
        )


MEMORY_COMMAND = Command(
    name="memory",
    description="记忆管理",
    usage="/memory [list | delete <文件名> | clear | edit]",
    type=CommandType.LOCAL,
    handler=handle_memory,
)

