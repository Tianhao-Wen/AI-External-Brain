
"""自动记忆管理器（对齐 Go 版 memory.Manager + memdir + paths）。

使用独立 .md 文件 + frontmatter + MEMORY.md 索引的存储格式，
替代旧版集中式 memories.md。每条记忆存为一个文件，MEMORY.md
只保存索引指针。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from aieb.conversation import ConversationManager, Message
from aieb.memory import provenance
from aieb import env_compat

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

# 记忆索引文件名
ENTRYPOINT_NAME = "MEMORY.md"

# 四种记忆类型（对齐 Go 版 MemoryType）
VALID_TYPES = {"user", "feedback", "project", "reference"}

# 记忆类型到存储目录的路由：user/feedback → 用户级，project/reference → 项目级
_USER_LEVEL_TYPES = {"user", "feedback"}
_PROJECT_LEVEL_TYPES = {"project", "reference"}

# MEMORY.md 截断限制
MAX_ENTRYPOINT_LINES = 200
MAX_ENTRYPOINT_BYTES = 25_000

# frontmatter 正则
_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)


# ---------------------------------------------------------------------------
# 路径工具函数（对齐 Go 版 paths.go）
# ---------------------------------------------------------------------------

def get_auto_mem_path(project_root: str) -> str:
    """返回项目级记忆目录路径：<projectRoot>/.aieb/memory/。

    保留尾部分隔符，确保前缀匹配不会误命中类似 memoryxyz 的路径。
    支持 AIEB_REMOTE_MEMORY_DIR 环境变量覆盖（历史名 WENDREAM_REMOTE_MEMORY_DIR 仍可用）。
    """
    override = env_compat.get("REMOTE_MEMORY_DIR", "")
    if override:
        return override.rstrip(os.sep) + os.sep
    abs_root = os.path.abspath(project_root)
    return os.path.join(abs_root, ".aieb", "memory") + os.sep


def get_user_auto_mem_path() -> str:
    """返回用户级记忆目录路径：~/.aieb/memory/。

    用于存储 type=user / type=feedback 的记忆，跨项目跟随用户。
    如果 HOME 无法解析则返回空字符串。
    """
    try:
        home = str(Path.home())
    except RuntimeError:
        return ""
    if not home:
        return ""
    return os.path.join(home, ".aieb", "memory") + os.sep


def is_auto_mem_path(absolute_path: str, project_root: str) -> bool:
    """检查路径是否在项目级或用户级记忆目录内。"""
    abs_p = os.path.normpath(absolute_path) + os.sep
    project_dir = get_auto_mem_path(project_root)
    if project_dir and abs_p.startswith(project_dir):
        return True
    user_dir = get_user_auto_mem_path()
    if user_dir and abs_p.startswith(user_dir):
        return True
    return False


def ensure_memory_dir_exists(memory_dir: str) -> None:
    """确保记忆目录存在，agent 可直接写入无需先 mkdir。"""
    if memory_dir:
        os.makedirs(memory_dir, exist_ok=True)


# ---------------------------------------------------------------------------
# Frontmatter 解析（对齐 Go 版 parseFrontmatter）
# ---------------------------------------------------------------------------

@dataclass
class MemoryFile:
    """一个记忆文件的元信息。"""
    path: str = ""
    name: str = ""
    description: str = ""
    type: str = ""


def parse_frontmatter(content: str) -> MemoryFile:
    """从 YAML-ish frontmatter 中提取 name/description/type。

    只读取三个已知字段，未知字段忽略。没有 frontmatter 的文件返回空字段。
    """
    mf = MemoryFile()
    m = _FRONTMATTER_RE.match(content)
    if not m:
        return mf
    for line in m.group(1).split("\n"):
        colon = line.find(":")
        if colon < 0:
            continue
        key = line[:colon].strip()
        val = line[colon + 1:].strip()
        # 去除引号
        if len(val) >= 2 and (
            (val.startswith('"') and val.endswith('"'))
            or (val.startswith("'") and val.endswith("'"))
        ):
            val = val[1:-1]
        if key == "name":
            mf.name = val
        elif key == "description":
            mf.description = val
        elif key == "type" and val in VALID_TYPES:
            mf.type = val
    return mf


# ---------------------------------------------------------------------------
# MEMORY.md 截断（对齐 Go 版 TruncateEntrypointContent）
# ---------------------------------------------------------------------------

def truncate_entrypoint_content(raw: str) -> str:
    """截断 MEMORY.md 内容，超过行数或字节限制时添加警告。"""
    trimmed = raw.strip()
    lines = trimmed.split("\n")
    line_count = len(lines)
    byte_count = len(trimmed.encode("utf-8"))

    over_lines = line_count > MAX_ENTRYPOINT_LINES
    over_bytes = byte_count > MAX_ENTRYPOINT_BYTES

    if not over_lines and not over_bytes:
        return trimmed

    result = trimmed
    if over_lines:
        result = "\n".join(lines[:MAX_ENTRYPOINT_LINES])

    result_bytes = result.encode("utf-8")
    if len(result_bytes) > MAX_ENTRYPOINT_BYTES:
        cut = result[:MAX_ENTRYPOINT_BYTES].rfind("\n")
        if cut > 0:
            result = result[:cut]
        else:
            result = result[:MAX_ENTRYPOINT_BYTES]

    # 构建警告信息
    if over_bytes and not over_lines:
        reason = f"{_format_size(byte_count)} (limit: {_format_size(MAX_ENTRYPOINT_BYTES)}) — index entries are too long"
    elif over_lines and not over_bytes:
        reason = f"{line_count} lines (limit: {MAX_ENTRYPOINT_LINES})"
    else:
        reason = f"{line_count} lines and {_format_size(byte_count)}"

    result += (
        f"\n\n> WARNING: {ENTRYPOINT_NAME} is {reason}. "
        "Only part of it was loaded. Keep index entries to one line "
        "under ~200 chars; move detail into topic files."
    )
    return result


def _format_size(byte_count: int) -> str:
    if byte_count < 1024:
        return f"{byte_count}B"
    elif byte_count < 1024 * 1024:
        return f"{byte_count / 1024:.1f}KB"
    else:
        return f"{byte_count / (1024 * 1024):.1f}MB"


# ---------------------------------------------------------------------------
# 构建记忆系统提示（对齐 Go 版 BuildMemoryPrompt）
# ---------------------------------------------------------------------------

def build_memory_prompt(user_mem_dir: str, project_mem_dir: str) -> str:
    """构建记忆系统提示，包含行为指令和 MEMORY.md 索引内容。

    对齐 Go 版 BuildMemoryPrompt：组合类型化记忆行为指令 + 两个 MEMORY.md
    的内容，生成完整的 '# auto memory' 系统提示段。
    """
    lines = _build_memory_lines(user_mem_dir, project_mem_dir)
    parts = [lines]

    if user_mem_dir:
        ep_path = os.path.join(user_mem_dir, ENTRYPOINT_NAME)
        parts.append("")
        parts.append(_build_entrypoint_section("User-level", ep_path))

    if project_mem_dir:
        ep_path = os.path.join(project_mem_dir, ENTRYPOINT_NAME)
        parts.append("")
        parts.append(_build_entrypoint_section("Project-level", ep_path))

    return "\n".join(parts)


def _build_entrypoint_section(scope_label: str, entrypoint_path: str) -> str:
    """读取一个 MEMORY.md 文件并格式化为系统提示段。

    标题里**故意不写绝对路径**：这段提示会进入模型的上下文，一旦带上
    ``C:\\Users\\...\\MEMORY.md`` 之类的信息，模型很容易在回答里复述给用户。
    模型只需要知道"有这么一个索引"，写盘由记忆服务自己负责。
    """
    header = f"## {scope_label} memories\n"
    try:
        data = Path(entrypoint_path).read_text(encoding="utf-8")
        if data.strip():
            return header + "\n" + truncate_entrypoint_content(data)
    except OSError:
        pass
    return header + (
        f"\nNothing has been saved at this scope yet. "
        f"The index is maintained automatically by the SaveMemory tool."
    )


def _build_memory_lines(user_mem_dir: str, project_mem_dir: str) -> str:
    """构建类型化记忆的行为指令文本（不含 MEMORY.md 内容）。

    重要：这里**不再出现任何绝对路径**，也不再要求模型"用 Write 工具创建
    markdown 文件 + 手工维护索引"。历史上的那套指令是用户级记忆被路径沙箱
    反复拦截、模型把 ``C:\\Users\\...``、``.aieb/memory``、``MEMORY.md``
    直接说给用户的根因。现在写入统一走受控的 ``SaveMemory``。
    """
    parts = ["# auto memory\n"]
    parts.append(
        "You have a persistent memory system with two scopes:\n"
    )

    if user_mem_dir:
        parts.append(
            "- **User-level** — who the user is and how they like to work: long-term "
            "preferences, taste, working style, recurring constraints. These follow the "
            "user across every project."
        )
    if project_mem_dir:
        parts.append(
            "- **Project-level** — facts that only matter for the current project/repo: "
            "architecture decisions, conventions, environment quirks."
        )

    parts.append(
        "\n## How to save a memory\n"
        "- Call the `SaveMemory` tool. It takes the memory content plus a scope "
        "(`user` or `project`). Storage, indexing and provenance are handled by the "
        "system — you do NOT create files, directories or indexes yourself.\n"
        "- Never use `WriteFile`, `EditFile`, `ReadFile` or `Bash` to touch memory "
        "storage. Those tools cannot reach it, and trying will look like an error to "
        "the user.\n"
        "- Pick `user` when the statement is about the human (preferences, taste, "
        "constraints that hold regardless of project). Pick `project` only when it is "
        "genuinely about this project. If you are unsure, prefer `user`.\n"
        "- Save when the user asks you to remember something, or when they state a "
        "durable preference. Do not save ephemeral task details, code that can be "
        "re-read from the repo, or git history.\n"
        "- Before saving, check the existing memory index below: if the same "
        "preference is already there, update it (same scope + similar title) instead "
        "of adding a near-duplicate.\n"
        "- The tool reports a status. Only claim that you remembered something when "
        "it reports success. If it reports the feature is disabled or the save "
        "failed, say so plainly and do not pretend.\n"
        "\n## How to talk about memory\n"
        "- To the user, memory is just \"remembering\". Never mention file paths, "
        "directory names, index files, tool names or session IDs unless the user "
        "explicitly asks how it works technically.\n"
        "- Good: \"Got it — I'll remember that and apply it next time.\"\n"
        "- Bad: telling the user which directory the note went into, or asking them "
        "to copy a file somewhere so it applies to other conversations.\n"
        "- Both scopes are already available in every conversation of this workspace "
        "and (for user-level) across projects. The user never needs to move anything."
    )

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# MemoryManager（对齐 Go 版 Manager）
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# 受控保存（SaveMemory）
# ---------------------------------------------------------------------------

# 单条记忆的长度上限：超长内容不是"记忆"，而是文档。
MAX_MEMORY_CHARS = 8000


@dataclass
class SaveResult:
    """一次记忆保存的结果。

    ``status`` 是**服务端事实**，模型只能依据它决定可否说"记住了"：

    * ``saved``     —— 新建成功
    * ``updated``   —— 更新了已有记忆
    * ``duplicate`` —— 内容已存在，未重复写入
    * ``disabled``  —— 用户关闭了记忆功能
    * ``failed``    —— 保存失败（含原因）
    """

    ok: bool
    status: str
    scope: str = ""
    filename: str = ""
    message: str = ""

    def as_tool_output(self) -> str:
        """给模型看的结果文本：第一行是机器可判定的状态，后面是人类说明。"""
        lines = [
            f"status: {self.status}",
            f"ok: {'true' if self.ok else 'false'}",
        ]
        if self.scope:
            lines.append(f"scope: {self.scope}")
        if self.message:
            lines.append(f"detail: {self.message}")
        if not self.ok:
            lines.append(
                "Do not tell the user the information was remembered. "
                "Explain briefly what happened instead."
            )
        return "\n".join(lines)


def slugify_memory_name(raw: str) -> str:
    """把标题压成安全的记忆文件名（不含扩展名）。

    只保留 ASCII 字母/数字与短横线；中文等非 ASCII 内容无法直接做文件名，
    这种情况返回空字符串，由调用方退化到内容哈希。
    """
    cleaned = re.sub(r"[^A-Za-z0-9]+", "-", (raw or "").strip().lower()).strip("-")
    if len(cleaned) > 60:
        cleaned = cleaned[:60].strip("-")
    return cleaned


def _normalize_body(text: str) -> str:
    """比较用正文：忽略空白差异，避免"同一句话不同换行"被当成两条记忆。"""
    return " ".join((text or "").split())


class MemoryManager:
    """管理双路径自动记忆目录（用户级 + 项目级）。

    对齐 Go 版 memory.Manager：使用独立 .md 文件 + frontmatter + MEMORY.md 索引。
    实际的写入/读取通过 agent 的 Write/Read 工具完成（参考架构），
    此类提供系统提示构建和 /memory 斜杠命令支持。
    """

    def __init__(self, project_root: str) -> None:
        abs_root = os.path.abspath(project_root)
        self._project_root = abs_root
        # 用户级：~/.aieb/memory/ — user/feedback 类型记忆
        self._user_mem_dir = get_user_auto_mem_path()
        # 项目级：<projectRoot>/.aieb/memory/ — project/reference 类型记忆
        self._mem_dir = get_auto_mem_path(abs_root)
        self._last_extraction_msg_count = 0

    @property
    def user_path(self) -> Path:
        """用户级 MEMORY.md 的路径（兼容旧接口）。"""
        if self._user_mem_dir:
            return Path(os.path.join(self._user_mem_dir, ENTRYPOINT_NAME))
        return Path.home() / ".aieb" / "memory" / ENTRYPOINT_NAME

    @property
    def project_path(self) -> Path:
        """项目级 MEMORY.md 的路径（兼容旧接口）。"""
        return Path(os.path.join(self._mem_dir, ENTRYPOINT_NAME))

    @property
    def user_mem_dir(self) -> Path:
        """用户级记忆目录（~/.aieb/memory/）。"""
        return Path(self._user_mem_dir.rstrip(os.sep)) if self._user_mem_dir else Path.home() / ".aieb" / "memory"

    @property
    def project_mem_dir(self) -> Path:
        """项目级记忆目录（<project>/.aieb/memory/）。"""
        return Path(self._mem_dir.rstrip(os.sep))

    def load(self) -> str:
        """构建完整的记忆系统提示（对齐 Go 版 BuildSystemReminder）。

        确保两个目录存在后，返回包含行为指令和 MEMORY.md 索引内容的
        '# auto memory' 段，用于注入系统提示。
        """
        if not self._mem_dir and not self._user_mem_dir:
            return ""
        # 记忆功能被关闭时，不再把旧记忆注入提示（与界面开关保持一致）
        if not self.enabled:
            return ""
        # 确保目录存在
        if self._user_mem_dir:
            ensure_memory_dir_exists(self._user_mem_dir)
        if self._mem_dir:
            ensure_memory_dir_exists(self._mem_dir)
        return build_memory_prompt(self._user_mem_dir, self._mem_dir)

    def load_all(self) -> list[MemoryFile]:
        """扫描两个目录中所有 .md 文件（排除 MEMORY.md），解析 frontmatter。

        对齐 Go 版 LoadAll：用户级文件在前，项目级在后。
        """
        result = _load_dir(self._user_mem_dir)
        result.extend(_load_dir(self._mem_dir))
        return result

    def get_memories(self) -> list[str]:
        """返回所有记忆文件的单行摘要，用于 /memory list。

        对齐 Go 版 GetMemories。
        """
        files = self.load_all()
        out: list[str] = []
        for f in files:
            type_tag = f.type if f.type else "?"
            desc = f.description if f.description else Path(f.path).name
            out.append(f"[{type_tag}] {f.name} — {desc}")
        return out

    def _scan_existing_memories(self) -> str:
        """扫描已有记忆文件，生成 manifest 给 LLM 做去重。"""
        entries: list[str] = []
        for dir_path in (self._user_mem_dir, self._mem_dir):
            if not dir_path:
                continue
            d = Path(dir_path)
            if not d.is_dir():
                continue
            for f in sorted(d.iterdir()):
                if f.name == ENTRYPOINT_NAME or not f.name.endswith(".md"):
                    continue
                try:
                    mf = parse_frontmatter(f.read_text(encoding="utf-8"))
                    type_tag = mf.type or "?"
                    desc = mf.description or f.stem
                    entries.append(f"- [{type_tag}] {f.name}: {desc}")
                except OSError:
                    continue
        return "\n".join(entries)

    async def extract(
        self,
        client: Any,
        conversation: ConversationManager,
        protocol: str,
        session_id: str | None = None,
    ) -> None:
        """触发记忆提取（参照 Go 版 extractor.go）。

        使用裸 LLM 调用 + 结构化输出解析，发送已有记忆 manifest 做去重。
        """
        from aieb.tools.base import StreamEnd, TextDelta

        # 用户关闭了记忆功能：不再从对话中提取新记忆
        if not self.enabled:
            return

        recent = conversation.history[self._last_extraction_msg_count:]
        if not recent:
            return

        conv_lines: list[str] = []
        for msg in recent:
            if msg.role == "user" and msg.content:
                conv_lines.append(f"[user]: {msg.content}")
            elif msg.role == "assistant" and msg.content:
                conv_lines.append(f"[assistant]: {msg.content}")
        if not conv_lines:
            return

        # 扫描已有记忆做去重
        manifest = self._scan_existing_memories()
        manifest_section = ""
        if manifest:
            manifest_section = (
                f"\n\n## Existing memory files\n\n{manifest}\n\n"
                "Check this list before creating — update an existing file rather than creating a duplicate."
            )

        prompt = (
            f"Analyze the conversation below and extract memories worth saving.\n\n"
            f"For each memory, output in this exact format:\n"
            f"MEMORY_NAME: <kebab-case-name>\n"
            f"MEMORY_TYPE: <user|feedback|project|reference>\n"
            f"MEMORY_DESC: <one-line description>\n"
            f"MEMORY_BODY: <content>\n"
            f"---\n\n"
            f"Types:\n"
            f"- user/feedback → save to {self._user_mem_dir}\n"
            f"- project/reference → save to {self._mem_dir}\n\n"
            f"What NOT to save:\n"
            f"- Code patterns derivable from reading the project\n"
            f"- Git history, debugging solutions\n"
            f"- Ephemeral task details\n\n"
            f"If nothing is worth saving, output NONE.{manifest_section}\n\n"
            f"Conversation:\n{''.join(conv_lines)}"
        )

        extract_conv = ConversationManager()
        extract_conv.history = [Message(role="user", content=prompt)]

        collected = ""
        try:
            async for event in client.stream(
                extract_conv, system="You are a memory extraction assistant."
            ):
                if isinstance(event, TextDelta):
                    collected += event.text
                elif isinstance(event, StreamEnd):
                    pass
        except Exception:
            return

        self._last_extraction_msg_count = len(conversation.history)

        # 解析并写入记忆文件
        if not collected or collected.strip() == "NONE" or "MEMORY_NAME:" not in collected:
            return

        # 会话已被删除（墓碑存在）→ 不再落盘，避免"删完又被写回来"
        if session_id and (
            provenance.is_purged(self._mem_dir, session_id)
            or provenance.is_purged(self._user_mem_dir, session_id)
        ):
            return

        blocks = [b for b in collected.split("---") if "MEMORY_NAME:" in b]
        for block in blocks:
            name = _extract_field(block, "MEMORY_NAME")
            mtype = _extract_field(block, "MEMORY_TYPE") or "reference"
            desc = _extract_field(block, "MEMORY_DESC")
            body = _extract_field(block, "MEMORY_BODY")
            if not name or not body:
                continue
            if mtype not in VALID_TYPES:
                mtype = "reference"

            # 走统一的保存入口：同内容去重、同标题更新、索引与来源登记一致。
            # 这样"用户让我记住"和"自动提取"不会产生两份近似的记忆。
            scope = "user" if mtype in _USER_LEVEL_TYPES else "project"
            self.save_memory(
                body,
                scope=scope,
                title=name,
                memory_type=mtype,
                session_id=session_id,
                source=provenance.SOURCE_EXTRACT,
            )

    def clear(self) -> None:
        """清除两个目录中所有 .md 文件（对齐 Go 版 Clear）。"""
        _clear_dir(self._user_mem_dir)
        _clear_dir(self._mem_dir)

    def delete(self, name: str) -> tuple[bool, str]:
        """删除单条记忆（按文件名或 name 字段），同时清理索引与来源记录。"""
        target = name if name.endswith(".md") else f"{name}.md"
        for dir_path in (self._user_mem_dir, self._mem_dir):
            if not dir_path:
                continue
            if (Path(dir_path) / target).is_file():
                ok = provenance.delete_memory_file(dir_path, target)
                scope = "用户级" if dir_path == self._user_mem_dir else "项目级"
                return ok, f"已删除{scope}记忆：{target}" if ok else f"删除{scope}记忆失败：{target}"
        return False, f"未找到记忆：{name}"

    def get_memories_with_sources(self) -> list[str]:
        """带来源信息的记忆列表（/memory list）。"""
        out: list[str] = []
        for dir_path, scope in ((self._user_mem_dir, "用户级"), (self._mem_dir, "项目级")):
            if not dir_path:
                continue
            for mf in _load_dir(dir_path):
                src = provenance.source_summary(dir_path, Path(mf.path).name)
                tag = mf.type or "?"
                out.append(f"[{scope}/{tag}] {mf.name} — {mf.description or ''}（{src}）")
        return out

    # ------------------------------------------------------------------
    # 写入时登记来源（供 Agent 工具路径调用）
    # ------------------------------------------------------------------

    def _scopes(self) -> list[tuple[str, str]]:
        return [(d, "user") for d in [self._user_mem_dir] if d] + [
            (d, "project") for d in [self._mem_dir] if d
        ]

    # ------------------------------------------------------------------
    # 受控保存：Agent 唯一应该使用的记忆写入口
    # ------------------------------------------------------------------

    def _scope_dir(self, scope: str) -> str:
        return self._user_mem_dir if scope == "user" else self._mem_dir

    @staticmethod
    def _pick_memory_name(title: str, content: str) -> str:
        base = slugify_memory_name(title) or slugify_memory_name(_first_line(content))
        if not base:
            digest = hashlib.sha1(
                _normalize_body(content).encode("utf-8", "replace")
            ).hexdigest()[:8]
            base = f"memory-{digest}"
        return base

    def save_memory(
        self,
        content: str,
        *,
        scope: str = "user",
        title: str = "",
        memory_type: str = "",
        session_id: str | None = None,
        source: str = provenance.SOURCE_SAVE,
    ) -> SaveResult:
        """把一条记忆写进受管目录，并完成索引 + 来源登记。

        这是记忆的**唯一**写入口：不接受路径参数，作用域由服务端映射到
        ``~/.aieb/memory`` 或 ``<project>/.aieb/memory``，因此既不需要
        路径沙箱授权，也不可能被用来读写其它文件。
        """
        text = (content or "").strip()
        if not text:
            return SaveResult(False, "failed", message="记忆内容为空")
        if len(text) > MAX_MEMORY_CHARS:
            return SaveResult(
                False,
                "failed",
                message=f"内容过长（{len(text)} 字），超过单条记忆上限 {MAX_MEMORY_CHARS} 字",
            )
        # 用户关闭了记忆：必须如实返回，不能继续写盘
        if not self.enabled:
            return SaveResult(False, "disabled", message="记忆功能已关闭")

        scope = scope if scope in ("user", "project") else "user"
        dir_path = self._scope_dir(scope)
        if not dir_path:
            return SaveResult(False, "failed", scope=scope, message="无法确定记忆存放位置")

        mtype = memory_type if memory_type in VALID_TYPES else ""
        if not mtype:
            mtype = "user" if scope == "user" else "project"
        # frontmatter 里的 type 必须与落盘目录一致，否则下次扫描会归类错
        if scope == "user" and mtype not in _USER_LEVEL_TYPES:
            mtype = "user"
        if scope == "project" and mtype not in _PROJECT_LEVEL_TYPES:
            mtype = "project"

        try:
            ensure_memory_dir_exists(dir_path)
        except OSError as exc:
            return SaveResult(False, "failed", scope=scope, message=f"无法创建记忆目录: {exc}")

        # 同一内容已经存在：不重复写文件（显式保存与自动提取会命中这里），
        # 但仍要把本次会话登记成这条记忆的又一个来源——否则删掉后写入的那个
        # 会话时，会把别的会话也在用的记忆一起清掉。
        wanted = _normalize_body(text)
        for existing in _load_dir(dir_path):
            existing_path = Path(existing.path)
            if existing_path.name == ENTRYPOINT_NAME:
                continue
            if _normalize_body(self._body_of(existing_path)) == wanted:
                try:
                    provenance.record_write(
                        dir_path,
                        existing_path.name,
                        session_id=session_id,
                        source=source,
                    )
                except OSError:
                    pass
                return SaveResult(
                    True,
                    "duplicate",
                    scope,
                    existing_path.name,
                    f"这条信息已经在记忆里了（{existing.name or existing_path.stem}）",
                )

        name = self._pick_memory_name(title, text)
        filename = f"{name}.md"
        target = Path(dir_path) / filename
        existed = target.is_file()

        desc = _first_line(text)[:120]
        # 展示名与文件名解耦：文件名必须是安全的 slug（中文内容会退化成哈希），
        # 但用户/模型看到的标题应该在面板里可读。
        display = (title or "").strip() or _first_line(text)[:40] or name
        payload = (
            f"---\nname: {display}\ndescription: {desc}\ntype: {mtype}\n---\n\n{text}\n"
        )
        try:
            provenance.write_text_atomic(target, payload)
        except OSError as exc:
            return SaveResult(False, "failed", scope=scope, message=f"写入失败: {exc}")

        # 来源登记（写入时刻，不依赖 mtime）
        try:
            provenance.record_write(
                dir_path,
                filename,
                session_id=session_id,
                source=source,
                digest=provenance.content_digest(payload),
            )
        except OSError:
            pass

        self._upsert_index(dir_path, filename, display, desc)

        status = "updated" if existed else "saved"
        return SaveResult(
            True,
            status,
            scope,
            filename,
            ("已更新已有记忆" if existed else "已保存"),
        )

    @staticmethod
    def _upsert_index(dir_path: str, filename: str, display: str, desc: str) -> None:
        """在 MEMORY.md 里写入/更新一行索引，不产生重复行。"""
        idx_path = Path(dir_path) / ENTRYPOINT_NAME
        line = f"- [{display}]({filename}) — {desc}"
        try:
            existing = idx_path.read_text(encoding="utf-8") if idx_path.exists() else ""
        except OSError:
            existing = ""
        lines = existing.splitlines()
        replaced = False
        for i, current in enumerate(lines):
            start = current.find("](")
            end = current.find(")", start + 2) if start >= 0 else -1
            if start >= 0 and end > 0 and current[start + 2:end].strip() == filename:
                lines[i] = line
                replaced = True
                break
        if not replaced:
            lines.append(line)
        try:
            provenance.write_text_atomic(idx_path, "\n".join(lines).rstrip("\n") + "\n")
        except OSError:
            pass

    def note_write(
        self, file_path: str | Path, *, session_id: str | None = None, source: str = "tool"
    ) -> bool:
        """登记一次对受管记忆文件的写入（不负责写文件本身）。"""
        try:
            resolved = Path(file_path).resolve()
        except (OSError, ValueError):
            return False
        if resolved.suffix != ".md":
            return False
        for dir_path, _scope in self._scopes():
            try:
                resolved.relative_to(Path(dir_path).resolve())
            except (OSError, ValueError):
                continue
            digest = None
            try:
                digest = provenance.content_digest(resolved.read_text(encoding="utf-8"))
            except OSError:
                pass
            return provenance.record_write(
                dir_path, resolved.name, session_id=session_id, source=source, digest=digest
            )
        return False

    # ------------------------------------------------------------------
    # 记忆功能开关（持久化在项目级记忆目录）
    # ------------------------------------------------------------------

    @property
    def settings_path(self) -> Path:
        return Path(self._mem_dir) / provenance.SETTINGS_FILE

    @property
    def enabled(self) -> bool:
        """记忆功能是否开启（未配置视为开启）。"""
        try:
            data = json.loads(self.settings_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return True
        return bool(data.get("enabled", True))

    def set_enabled(self, enabled: bool) -> bool:
        path = self.settings_path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = {"enabled": bool(enabled), "updated_at": datetime.now(timezone.utc).isoformat()}
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            return True
        except OSError:
            return False

    # ------------------------------------------------------------------
    # Web 记忆管理接口
    # ------------------------------------------------------------------

    @staticmethod
    def memory_id(scope: str, filename: str) -> str:
        digest = hashlib.sha1(f"{scope}:{filename}".encode("utf-8")).hexdigest()[:12]
        return f"mem_{digest}"

    def _find_by_id(self, mem_id: str) -> tuple[str, str, str] | None:
        """返回 (目录, 作用域, 文件名)。"""
        for dir_path, scope in self._scopes():
            for mf in _load_dir(dir_path):
                filename = Path(mf.path).name
                if self.memory_id(scope, filename) == mem_id:
                    return dir_path, scope, filename
        return None

    @staticmethod
    def _body_of(path: Path) -> str:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return ""
        m = re.match(r"\A---\s*\n.*?\n---\s*\n?", text, re.DOTALL)
        return (text[m.end():] if m else text).strip()

    @staticmethod
    def _source_label(
        scope: str,
        source: str,
        sessions: list[str],
        titles: dict[str, str],
        detached: list[dict] | None = None,
    ) -> str:
        if source == provenance.SOURCE_USER:
            return "你手动管理"
        if sessions:
            known = [titles.get(s) for s in sessions if titles.get(s)]
            if len(sessions) == 1:
                if known:
                    return f"来自对话「{known[0]}」"
                return "来自一个已删除的对话"
            if known:
                return f"来自 {len(sessions)} 个对话"
            return f"来自 {len(sessions)} 个已删除的对话"
        # 来源会话已被删除，但用户选择保留了这条记忆：
        # 必须说清楚是"有意保留"，不能笼统地显示成"清理漏了"。
        detached = detached or []
        if detached:
            title = next((d.get("title") or "" for d in reversed(detached)), "")
            if title:
                return f"来自已删除的对话「{title}」（你选择保留）"
            return "来自已删除的对话（你选择保留）"
        return "历史记忆，来源未知"

    def list_for_web(self, session_titles: dict[str, str] | None = None) -> list[dict[str, Any]]:
        """给 Web 记忆管理面板用的列表（不包含任何本地路径或 Session ID）。"""
        titles = session_titles or {}
        items: list[dict[str, Any]] = []
        for dir_path, scope in self._scopes():
            entries = provenance.load_entries(dir_path)
            for mf in _load_dir(dir_path):
                filename = Path(mf.path).name
                entry = entries.get(filename) or {}
                sessions = entry.get("sessions") or []
                detached = entry.get("detached") or []
                source = entry.get("source") or provenance.SOURCE_UNKNOWN
                body = self._body_of(Path(mf.path))
                preview = " ".join(body.split())[:120]
                items.append({
                    "id": self.memory_id(scope, filename),
                    "title": mf.name or Path(filename).stem,
                    "description": mf.description,
                    "preview": preview,
                    "body": body,
                    "scope": scope,
                    "scope_label": "我的长期偏好" if scope == "user" else "当前项目记忆",
                    "type": mf.type or "reference",
                    "source_label": self._source_label(
                        scope, source, sessions, titles, detached
                    ),
                })
        items.sort(key=lambda x: (x["scope"] != "user", x["title"]))
        return items

    def delete_by_id(self, mem_id: str) -> tuple[bool, str]:
        found = self._find_by_id(mem_id)
        if found is None:
            return False, "找不到这条记忆（可能已被删除）"
        dir_path, _scope, filename = found
        ok = provenance.delete_memory_file(dir_path, filename)
        return (ok, "记忆已删除" if ok else "删除失败，请稍后重试")

    def clear_scope(self, scope: str = "project") -> tuple[bool, str, int]:
        """清空指定范围的记忆。``scope`` 取 ``project`` 或 ``all``。"""
        targets = [(self._mem_dir, "项目记忆")]
        if scope == "all":
            targets.append((self._user_mem_dir, "长期偏好"))
        removed = 0
        errors: list[str] = []
        for dir_path, label in targets:
            if not dir_path:
                continue
            for entry in sorted(Path(dir_path).glob("*.md")):
                if entry.name == provenance.INDEX_NAME:
                    continue  # MEMORY.md 是索引，不算一条记忆
                try:
                    entry.unlink()
                    removed += 1
                except OSError as exc:
                    errors.append(f"{label} {entry.name}: {exc}")
            try:
                (Path(dir_path) / provenance.SOURCES_FILE).unlink()
            except (OSError, FileNotFoundError):
                pass
            try:
                (Path(dir_path) / provenance.INDEX_NAME).unlink()
            except (OSError, FileNotFoundError):
                pass
        if errors:
            return False, "部分记忆未能删除：" + "; ".join(errors), removed
        return True, "记忆已清空", removed

    def get_display_text(self) -> str:
        """返回记忆摘要文本（/memory 命令显示）。"""
        memories = self.get_memories()
        if not memories:
            return "当前没有任何自动记忆。"

        parts: list[str] = []
        parts.append(f"记忆目录：")
        parts.append(f"  用户级: {self._user_mem_dir}")
        parts.append(f"  项目级: {self._mem_dir}")
        parts.append("")
        for line in memories:
            parts.append(f"  {line}")
        return "\n".join(parts)


# ---------------------------------------------------------------------------
# 内部辅助函数
# ---------------------------------------------------------------------------

def _load_dir(dir_path: str) -> list[MemoryFile]:
    """扫描目录中的 .md 文件，解析 frontmatter 并返回 MemoryFile 列表。"""
    if not dir_path:
        return []
    d = Path(dir_path)
    if not d.is_dir():
        return []

    try:
        entries = sorted(d.iterdir(), key=lambda p: p.name)
    except OSError:
        return []

    result: list[MemoryFile] = []
    for entry in entries:
        if entry.is_dir():
            continue
        if entry.name == ENTRYPOINT_NAME or not entry.name.endswith(".md"):
            continue
        try:
            data = entry.read_text(encoding="utf-8")
        except OSError:
            continue
        mf = parse_frontmatter(data)
        mf.path = str(entry)
        if not mf.name:
            mf.name = entry.stem  # 去掉 .md 后缀
        result.append(mf)
    return result


def _first_line(text: str) -> str:
    """取第一行有内容的文字（用于自动生成描述/标题）。"""
    for line in (text or "").splitlines():
        stripped = line.strip().lstrip("-*# >").strip()
        if stripped:
            return stripped
    return ""


def _extract_field(block: str, field: str) -> str:
    """从记忆提取输出的一个 block 中提取指定字段值。"""
    m = re.search(rf"{field}:\s*(.+?)(?:\n|$)", block)
    return m.group(1).strip() if m else ""


def _clear_dir(dir_path: str) -> None:
    """删除目录中所有 .md 文件（包括 MEMORY.md）以及来源索引。"""
    if not dir_path:
        return
    d = Path(dir_path)
    if not d.is_dir():
        return
    try:
        for entry in d.iterdir():
            if entry.is_dir() or not entry.name.endswith(".md"):
                continue
            try:
                entry.unlink()
            except OSError:
                pass
        # 记忆全没了，来源索引与墓碑也一并清掉
        for extra in (provenance.SOURCES_FILE, provenance.TOMBSTONE_FILE):
            try:
                (d / extra).unlink()
            except (OSError, FileNotFoundError):
                pass
    except OSError:
        pass
