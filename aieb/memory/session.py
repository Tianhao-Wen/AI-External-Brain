from __future__ import annotations

import json
import os
import random
import re
import string
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import IO, Any

from aieb.conversation import (
    ConversationManager,
    DocumentBlock,
    ImageBlock,
    Message,
    ToolResultBlock,
    ToolUseBlock,
)

SESSIONS_DIR = ".aieb/sessions"
DEFAULT_MAX_AGE_DAYS = 30
TITLE_MAX_LENGTH = 50
#: 侧边栏标题的展示上限（超过就截断加省略号）
TITLE_PREVIEW_LENGTH = 24


class SessionDeleteError(RuntimeError):
    """会话文件删不掉（通常是 Windows 上被其它进程占用）。"""


def derive_title(text: str) -> str:
    """从首条用户消息派生一个**简短可读**的会话标题。

    以前直接用 ``message.content[:50]``，侧边栏里会出现
    "上一次供应商评审会的时间、参与人和主要决策是什么…" 这种长句。
    这里改成：取第一行 → 去掉 markdown/列表前缀与表情 → 截到第一个
    句末标点（不超过预览长度）→ 仍然超长就加省略号。

    标题只用于列表索引，不参与检索，所以不需要保留完整问题。
    """
    raw = (text or "").strip()
    if not raw:
        return ""
    first = raw.splitlines()[0].strip()
    first = re.sub(r"^[#>*\-\s]+", "", first)          # 标题/列表/引用前缀
    first = re.sub(r"`+|\*\*|__", "", first)             # 行内 markdown 强调
    first = re.sub(r"[\U0001F300-\U0001FAFF\u2600-\u27BF]", "", first)  # 表情
    first = first.strip()
    for sep in ("。", "？", "！", "?", "!", "；", ";"):
        idx = first.find(sep)
        if 0 < idx <= TITLE_PREVIEW_LENGTH:
            first = first[:idx]
            break
    if len(first) > TITLE_PREVIEW_LENGTH:
        first = first[:TITLE_PREVIEW_LENGTH].rstrip() + "…"
    return (first or raw.splitlines()[0])[:TITLE_MAX_LENGTH]

SESSION_SUMMARY_PROMPT = (
    "你是一个对话摘要助手。请根据下面的对话内容，用一句话总结这个会话的主要内容。"
    "只输出摘要文本，不要加任何前缀或标点符号外的修饰。不要调用任何工具。"
)


# ---------------------------------------------------------------------------
# RecordType & SessionRecord
# ---------------------------------------------------------------------------


class RecordType(str, Enum):
    SYSTEM_PROMPT = "system_prompt"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL_RESULT = "tool_result"
    COMPRESSION = "compression"
    # Layer-2 compact 标记。auto_compact 压缩对话记录时写入。
    # 内容为结构化载荷（参见 make_compact_boundary / parse_compact_boundary），
    # 包含摘要文本和原样保留的 keep 尾部（以序列化 record 形式内联），
    # 使 resume 可以仅凭此标记重建压缩后的状态，无需重放标记之前的原始前缀。
    COMPACT_BOUNDARY = "compact_boundary"


@dataclass
class SessionRecord:
    type: RecordType
    content: Any
    timestamp: datetime
    tool_use_id: str | None = None
    is_error: bool = False
    # 分支标识：同一条 Message 的所有 record（例如多个 tool_result）共享同一对 ID。
    # 旧会话文件没有这两个字段，读取时留空，由 resume() 补上确定性 ID。
    message_id: str = ""
    parent_id: str | None = None

    def to_jsonl(self) -> str:
        data: dict[str, Any] = {
            "type": self.type.value,
            "content": self.content,
            "timestamp": self.timestamp.isoformat(),
        }
        if self.tool_use_id is not None:
            data["tool_use_id"] = self.tool_use_id
        if self.type == RecordType.TOOL_RESULT:
            data["is_error"] = self.is_error
        if self.message_id:
            data["message_id"] = self.message_id
        if self.parent_id is not None:
            data["parent_id"] = self.parent_id
        return json.dumps(data, ensure_ascii=False)


    @classmethod
    def from_jsonl(cls, line: str) -> SessionRecord | None:
        try:
            data = json.loads(line)
            return cls(
                type=RecordType(data["type"]),
                content=data["content"],
                timestamp=datetime.fromisoformat(data["timestamp"]),
                tool_use_id=data.get("tool_use_id"),
                is_error=data.get("is_error", False),
                message_id=data.get("message_id") or "",
                parent_id=data.get("parent_id"),
            )
        except (json.JSONDecodeError, KeyError, ValueError):
            return None

    @classmethod
    def from_message(cls, message: Message) -> list[SessionRecord]:
        now = datetime.now(timezone.utc)
        records: list[SessionRecord] = []

        if message.tool_results:
            for tr in message.tool_results:
                records.append(
                    cls(
                        type=RecordType.TOOL_RESULT,
                        content=tr.content,
                        timestamp=now,
                        tool_use_id=tr.tool_use_id,
                        is_error=tr.is_error,
                    )
                )
        elif message.role == "assistant":
            if message.tool_uses:
                content_blocks: list[dict[str, Any]] = []
                if message.content:
                    content_blocks.append({"type": "text", "text": message.content})
                for tu in message.tool_uses:
                    content_blocks.append(
                        {
                            "type": "tool_use",
                            "id": tu.tool_use_id,
                            "name": tu.tool_name,
                            "input": tu.arguments,
                        }
                    )
                records.append(
                    cls(type=RecordType.ASSISTANT, content=content_blocks, timestamp=now)
                )
            else:
                records.append(
                    cls(type=RecordType.ASSISTANT, content=message.content, timestamp=now)
                )
        elif message.images or message.documents:
            content_blocks: list[dict[str, Any]] = [
                {"type": "image", "media_type": ib.media_type, "data": ib.data}
                for ib in message.images
            ]
            content_blocks.extend(
                {
                    "type": "document",
                    "media_type": db.media_type,
                    "data": db.data,
                    "name": db.name,
                }
                for db in message.documents
            )
            if message.content:
                content_blocks.append({"type": "text", "text": message.content})
            records.append(
                cls(type=RecordType.USER, content=content_blocks, timestamp=now)
            )
        else:
            records.append(
                cls(type=RecordType.USER, content=message.content, timestamp=now)
            )

        for record in records:
            record.message_id = message.message_id
            record.parent_id = message.parent_id
        return records


# ---------------------------------------------------------------------------
# Compact boundary 载荷（摘要 + 内联的 keep 尾部）
# ---------------------------------------------------------------------------


def _message_to_record_dicts(message: Message) -> list[dict[str, Any]]:
    """将单条 Message 序列化为与磁盘存储格式一致的 record-dict 列表。

    复用 SessionRecord.from_message，使内联的 keep 尾部与正常追加消息的持久化
    结果逐字节一致（assistant 的 tool_uses 变为 content-blocks 列表，每个
    tool_result 独立成一条 record）。这保证了 tool_use↔tool_result 配对的
    无损往返——不像纯 role+content 文本导出那样会丢失 tool call 的关联关系。
    """
    dicts: list[dict[str, Any]] = []
    for rec in SessionRecord.from_message(message):
        data: dict[str, Any] = {"type": rec.type.value, "content": rec.content}
        if rec.tool_use_id is not None:
            data["tool_use_id"] = rec.tool_use_id
        if rec.type == RecordType.TOOL_RESULT:
            data["is_error"] = rec.is_error
        if rec.message_id:
            data["message_id"] = rec.message_id
        if rec.parent_id is not None:
            data["parent_id"] = rec.parent_id
        dicts.append(data)
    return dicts


def make_compact_boundary(summary: str, keep: list[Message]) -> SessionRecord:
    """构建一条 COMPACT_BOUNDARY record，内联摘要和原样保留的 keep 尾部。

    `keep` 是 auto_compact 原样保留的近期尾部消息。将其存储在 boundary record
    内部（而不是依赖它在文件中的物理位置），意味着 resume 可以仅凭 boundary
    重建压缩后的状态——boundary 之前的原始前缀保留在磁盘上但不会被重放。
    """
    keep_dicts: list[dict[str, Any]] = []
    for msg in keep:
        keep_dicts.extend(_message_to_record_dicts(msg))
    payload = {"summary": summary, "keep": keep_dicts}
    return SessionRecord(
        type=RecordType.COMPACT_BOUNDARY,
        content=payload,
        timestamp=datetime.now(timezone.utc),
    )


def parse_compact_boundary(record: SessionRecord) -> tuple[str, list[Message]]:
    """make_compact_boundary 的逆操作：返回 (summary, keep_messages)。

    对遗留或格式异常的 payload 降级返回 ("", [])，确保单条损坏的 boundary
    不会导致 resume 崩溃。
    """
    content = record.content
    if not isinstance(content, dict):
        return "", []
    summary = content.get("summary", "")
    keep_raw = content.get("keep", [])
    keep_records: list[SessionRecord] = []
    for item in keep_raw if isinstance(keep_raw, list) else []:
        if not isinstance(item, dict) or "type" not in item:
            continue
        try:
            keep_records.append(
                SessionRecord(
                    type=RecordType(item["type"]),
                    content=item.get("content"),
                    timestamp=record.timestamp,
                    tool_use_id=item.get("tool_use_id"),
                    is_error=item.get("is_error", False),
                )
            )
        except ValueError:
            continue
    return summary, records_to_messages(keep_records)


# ---------------------------------------------------------------------------
# Record ↔ Message 转换
# ---------------------------------------------------------------------------


def records_to_messages(records: list[SessionRecord]) -> list[Message]:
    messages: list[Message] = []
    pending_tool_results: list[ToolResultBlock] = []
    pending_ids: tuple[str, str | None] = ("", None)

    for record in records:
        if record.type == RecordType.TOOL_RESULT:
            if not pending_tool_results:
                pending_ids = (record.message_id, record.parent_id)
            pending_tool_results.append(
                ToolResultBlock(
                    tool_use_id=record.tool_use_id or "",
                    content=(
                        record.content
                        if isinstance(record.content, str)
                        else json.dumps(record.content)
                    ),
                    is_error=record.is_error,
                )
            )
            continue

        if pending_tool_results:
            messages.append(
                Message(
                    role="user",
                    content="",
                    tool_results=pending_tool_results,
                    message_id=pending_ids[0],
                    parent_id=pending_ids[1],
                )
            )
            pending_tool_results = []

        if record.type == RecordType.SYSTEM_PROMPT:
            continue

        if record.type == RecordType.COMPRESSION:
            messages.append(
                Message(
                    role="user",
                    content="本次会话延续自之前的对话，因上下文空间不足进行了压缩。以下是早期对话的摘要：\n\n" + (record.content or ""),
                )
            )
            continue

        if record.type == RecordType.COMPACT_BOUNDARY:
            # 内联展开：摘要作为 user 消息，后接原样保留的 keep 尾部。
            # resume() 通常已预裁剪到最后一个 boundary，所以这里只会处理
            # 权威的那一条；但在此展开可以保证 records_to_messages 对任何
            # 直接调用者都保持自洽。
            summary, keep_messages = parse_compact_boundary(record)
            messages.append(Message(role="user", content="本次会话延续自之前的对话，因上下文空间不足进行了压缩。以下是早期对话的摘要：\n\n" + summary))
            messages.extend(keep_messages)
            continue

        if record.type == RecordType.USER:
            if isinstance(record.content, list):
                text = ""
                images: list[ImageBlock] = []
                documents: list[DocumentBlock] = []
                for block in record.content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "text":
                        text += block.get("text", "")
                    elif block.get("type") == "image":
                        images.append(
                            ImageBlock(
                                media_type=block.get("media_type", ""),
                                data=block.get("data", ""),
                            )
                        )
                    elif block.get("type") == "document":
                        documents.append(
                            DocumentBlock(
                                media_type=block.get("media_type", ""),
                                data=block.get("data", ""),
                                name=block.get("name", ""),
                            )
                        )
                messages.append(
                    Message(
                        role="user",
                        content=text,
                        images=images,
                        documents=documents,
                        message_id=record.message_id,
                        parent_id=record.parent_id,
                    )
                )
            else:
                messages.append(
                    Message(
                        role="user",
                        content=record.content or "",
                        message_id=record.message_id,
                        parent_id=record.parent_id,
                    )
                )
        elif record.type == RecordType.ASSISTANT:
            if isinstance(record.content, list):
                text = ""
                tool_uses: list[ToolUseBlock] = []
                for block in record.content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "text":
                        text += block.get("text", "")
                    elif block.get("type") == "tool_use":
                        tool_uses.append(
                            ToolUseBlock(
                                tool_use_id=block.get("id", ""),
                                tool_name=block.get("name", ""),
                                arguments=block.get("input", {}),
                            )
                        )
                messages.append(
                    Message(
                        role="assistant",
                        content=text,
                        tool_uses=tool_uses,
                        message_id=record.message_id,
                        parent_id=record.parent_id,
                    )
                )
            else:
                messages.append(
                    Message(
                        role="assistant",
                        content=record.content or "",
                        message_id=record.message_id,
                        parent_id=record.parent_id,
                    )
                )

    if pending_tool_results:
        messages.append(
            Message(
                role="user",
                content="",
                tool_results=pending_tool_results,
                message_id=pending_ids[0],
                parent_id=pending_ids[1],
            )
        )

    return messages


# ---------------------------------------------------------------------------
# 消息链校验
# ---------------------------------------------------------------------------


def validate_message_chain(records: list[SessionRecord]) -> int:
    last_valid = 0
    pending_tool_uses: set[str] = set()

    for i, record in enumerate(records):
        if record.type == RecordType.ASSISTANT and isinstance(record.content, list):
            for block in record.content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    tool_id = block.get("id", "")
                    if tool_id:
                        pending_tool_uses.add(tool_id)

        if record.type == RecordType.TOOL_RESULT and record.tool_use_id:
            pending_tool_uses.discard(record.tool_use_id)

        if not pending_tool_uses:
            last_valid = i + 1

    return last_valid


# ---------------------------------------------------------------------------
# 分支（对话树）基础工具
# ---------------------------------------------------------------------------


def new_message_id() -> str:
    """生成一个稳定、可读、无碰撞风险的消息 ID（只用于本地持久化）。"""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
    suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=6))
    return f"m_{stamp}_{suffix}"


def legacy_message_id(index: int) -> str:
    """给没有 ID 的旧记录生成**确定性** ID。

    旧版本写盘的 jsonl 没有 message_id。这里用「消息在文件中的序号」派生 ID：
    文件只追加、旧记录顺序不变，所以同一个旧会话每次加载都会得到同一批 ID，
    新建分支写下的 parent_id 才能在重启后重新对上。
    """
    return f"m{index:04d}"


def is_human_message(message: Message) -> bool:
    """是否是「用户真正打字发送」的消息。

    排除工具结果回填（tool_results 非空）、系统提醒注入和空消息——
    这些都不是可以编辑/参与版本导航的对话轮次。
    """
    if message.role != "user" or message.tool_results:
        return False
    content = message.content or ""
    if not content.strip() and not message.images and not message.documents:
        return False
    return not content.startswith("<system-reminder>") and not content.startswith(
        "Current working directory:"
    )


def trim_incomplete_tail(messages: list[Message]) -> list[Message]:
    """去掉结尾处「有 tool_use 却没有 tool_result」的残尾消息。

    用于崩溃/取消后的恢复：只有尾部可能不完整；中间的分支不该被牵连
    （这是分支模型与旧的线性 validate_message_chain 的关键区别）。
    """
    out = list(messages)
    while out:
        last = out[-1]
        if last.tool_uses:
            out.pop()
            continue
        if last.tool_results:
            known = {tu.tool_use_id for m in out[:-1] for tu in m.tool_uses}
            if any(tr.tool_use_id not in known for tr in last.tool_results):
                out.pop()
                continue
        break
    return out


# ---------------------------------------------------------------------------
# SessionMeta
# ---------------------------------------------------------------------------


@dataclass
class SessionMeta:
    id: str
    title: str = ""
    summary: str = ""
    message_count: int = 0
    total_tokens: int = 0
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_active: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def save(self, path: Path) -> None:
        data = {
            "id": self.id,
            "title": self.title,
            "summary": self.summary,
            "message_count": self.message_count,
            "total_tokens": self.total_tokens,
            "created_at": self.created_at.isoformat(),
            "last_active": self.last_active.isoformat(),
        }
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    @classmethod
    def load(cls, path: Path) -> SessionMeta | None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return cls(
                id=data["id"],
                title=data.get("title", ""),
                summary=data.get("summary", ""),
                message_count=data.get("message_count", 0),
                total_tokens=data.get("total_tokens", 0),
                created_at=datetime.fromisoformat(data["created_at"]),
                last_active=datetime.fromisoformat(data["last_active"]),
            )
        except (json.JSONDecodeError, KeyError, ValueError):
            return None


# ---------------------------------------------------------------------------
# Session（活跃会话句柄）
# ---------------------------------------------------------------------------


class Session:
    """活跃会话句柄：追加消息 + 维护「对话树」（分支）索引。

    ## 为什么是树，不是列表

    编辑历史消息不能直接截断并丢弃后面的内容——那样旧分支就永久消失了。
    这里把会话消息组织成一棵树：每条消息记录自己的 ``message_id`` 和
    ``parent_id``，编辑某个问题时新消息挂到它的父节点下，于是旧分支作为
    兄弟节点被完整保留在同一个 jsonl 文件里（只追加，从不删除）。

    三个指针：
      * ``_nodes / _children`` —— 树结构索引（由磁盘记录重建）；
      * ``_current`` —— 当前正在显示的那条路径的叶子消息；
      * ``_branch_parent`` —— 下一条新消息要挂到哪个父节点（编辑时指向截取
        后的前缀末尾，正常对话时等于上一条消息）。

    ``<sid>.branches.json`` 只保存「当前叶子 + 每个父节点最近选中的子节点」
    这一小块状态，用临时文件 + ``os.replace`` 原子写入；消息本体仍然只存在
    于原来的 jsonl 里，因此旧会话不需要迁移。
    """

    def __init__(
        self,
        session_id: str,
        file: IO[str],
        meta: SessionMeta,
        sessions_dir: Path,
    ) -> None:
        self.session_id = session_id
        self._file = file
        self.meta = meta
        self._sessions_dir = sessions_dir
        # 对话树索引（只覆盖已落盘的消息）
        self._nodes: dict[str, Message] = {}
        self._children: dict[str | None, list[str]] = {}
        self._current: str | None = None
        self._branch_parent: str | None = None
        self._preferred: dict[str, str] = {}
        self._state_path = sessions_dir / f"{session_id}.branches.json"
        self._load_state()

    # -- 索引 -------------------------------------------------------------
    @property
    def nodes(self) -> dict[str, Message]:
        return self._nodes

    @property
    def current(self) -> str | None:
        return self._current

    def get(self, message_id: str) -> Message | None:
        return self._nodes.get(message_id)

    def load_tree(self, messages: list[Message]) -> None:
        """用（已按文件顺序解析出来的）消息列表重建树索引。"""
        self._nodes.clear()
        self._children.clear()
        for message in messages:
            self._register(message)
        if self._current not in self._nodes:
            self._current = messages[-1].message_id if messages else None
        self._branch_parent = self._current

    def _register(self, message: Message) -> None:
        if not message.message_id:
            return
        self._nodes[message.message_id] = message
        kids = self._children.setdefault(message.parent_id, [])
        if message.message_id not in kids:
            kids.append(message.message_id)

    def _load_state(self) -> None:
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError):
            return
        if not isinstance(data, dict):
            return
        current = data.get("current")
        if isinstance(current, str) and current:
            self._current = current
        preferred = data.get("preferred")
        if isinstance(preferred, dict):
            for key, value in preferred.items():
                if isinstance(key, str) and isinstance(value, str) and value:
                    self._preferred[key] = value

    def _save_state(self) -> None:
        payload = {
            "version": 1,
            "current": self._current,
            "preferred": self._preferred,
        }
        tmp = self._state_path.with_suffix(".json.tmp")
        try:
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self._state_path)
        except OSError:
            # 分支状态写失败不能影响对话本身：内存索引仍然可用
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    # -- 追加 -------------------------------------------------------------
    def append(self, message: Message) -> None:
        if not message.message_id:
            message.message_id = new_message_id()
            if message.parent_id is None:
                message.parent_id = self._branch_parent
        records = SessionRecord.from_message(message)
        for record in records:
            self._file.write(record.to_jsonl() + "\n")
        self._file.flush()

        self._register(message)
        self._current = message.message_id
        self._branch_parent = message.message_id
        self._preferred[message.parent_id or ""] = message.message_id
        self._save_state()

        self.meta.message_count += 1
        self.meta.last_active = datetime.now(timezone.utc)

        if not self.meta.title and message.role == "user":
            if message.content:
                self.meta.title = derive_title(message.content)
            elif message.images:
                self.meta.title = "[图片]"
            elif message.documents:
                self.meta.title = "[文件]"

        self.meta.save(self._sessions_dir / f"{self.session_id}.meta")

    def append_record(self, record: SessionRecord) -> None:
        """追加一条原始 SessionRecord（例如 compact_boundary 标记）。

        与 append() 不同，此方法不会更新 message_count/title——boundary 是
        结构性标记而非对话轮次。last_active 仍会更新，以保证 session 按最近
        使用排序。
        """
        self._file.write(record.to_jsonl() + "\n")
        self._file.flush()
        self.meta.last_active = datetime.now(timezone.utc)
        self.meta.save(self._sessions_dir / f"{self.session_id}.meta")


    def close(self) -> None:
        if self._file and not self._file.closed:
            self._file.flush()
            self._file.close()

    def rewrite(self, messages: list[Message]) -> None:
        """把整段会话历史用给定的消息列表重写回磁盘。

        仅用于「把当前路径重新落盘」这类整体操作（compact 之后等）。编辑历史
        消息、切换分支**不再**走这里——它们通过 append 追加新分支，旧分支的
        记录原样保留，不能用重写把它们抹掉。

        写入走「临时文件 + os.replace」，避免写到一半崩溃把历史截断成半截。
        """
        self.close()
        path = self._sessions_dir / f"{self.session_id}.jsonl"
        tmp = path.with_suffix(".jsonl.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            for message in messages:
                for record in SessionRecord.from_message(message):
                    f.write(record.to_jsonl() + "\n")
        os.replace(tmp, path)
        self._file = open(path, "a", encoding="utf-8")  # noqa: SIM115

        self._nodes.clear()
        self._children.clear()
        for message in messages:
            self._register(message)
        self._current = messages[-1].message_id if messages else None
        self._branch_parent = self._current
        self._preferred = (
            {self._nodes[self._current].parent_id or "": self._current}
            if self._current
            else {}
        )
        self._save_state()

        self.meta.message_count = len(messages)
        self.meta.last_active = datetime.now(timezone.utc)
        self.meta.save(self._sessions_dir / f"{self.session_id}.meta")

    # -- 分支操作 ---------------------------------------------------------
    def path_messages(self, leaf_id: str | None) -> list[Message]:
        """从叶子沿 parent_id 回溯到根，返回该分支的完整消息列表。"""
        out: list[Message] = []
        seen: set[str] = set()
        cur = leaf_id
        while cur and cur in self._nodes and cur not in seen:
            seen.add(cur)
            message = self._nodes[cur]
            out.append(message)
            cur = message.parent_id
        out.reverse()
        return out

    def current_path(self) -> list[Message]:
        return self.path_messages(self._current)

    def human_ancestor(self, message_id: str) -> str | None:
        """上一条「人类消息」的 ID（没有则 None）。

        版本导航按「上一条人类消息」分组，而不是严格按 parent_id：agent 会在
        用户消息前后插入 system-reminder，严格父子关系会把同一位置的两版问题
        分到不同组里。
        """
        message = self._nodes.get(message_id)
        cur = message.parent_id if message else None
        seen: set[str] = set()
        while cur and cur in self._nodes and cur not in seen:
            seen.add(cur)
            node = self._nodes[cur]
            if is_human_message(node):
                return cur
            cur = node.parent_id
        return None

    def siblings(self, message_id: str) -> list[str]:
        """同一位置上的所有人类消息版本（按创建顺序）。"""
        message = self._nodes.get(message_id)
        if message is None or not is_human_message(message):
            return [message_id] if message_id in self._nodes else []
        key = self.human_ancestor(message_id)
        out: list[str] = []
        for mid, node in self._nodes.items():
            if not is_human_message(node):
                continue
            if self.human_ancestor(mid) == key:
                out.append(mid)
        return out

    def version_info(self, message_id: str) -> dict[str, Any]:
        sibs = self.siblings(message_id)
        if not sibs or message_id not in sibs:
            return {"index": 0, "total": 1}
        return {"index": sibs.index(message_id), "total": len(sibs)}

    def leaf_under(self, node_id: str) -> str:
        """某个节点下「最近使用」的叶子（沿 _preferred 走，缺省取最后一个子节点）。"""
        cur = node_id
        seen: set[str] = set()
        while cur in self._nodes and cur not in seen:
            seen.add(cur)
            kids = self._children.get(cur) or []
            if not kids:
                return cur
            nxt = self._preferred.get(cur)
            if nxt not in kids:
                nxt = kids[-1]
            cur = nxt
        return node_id

    def branch_to(self, parent_id: str | None) -> None:
        """下一次 append 挂到 parent_id 之下（编辑/重新生成时用来开新分支）。"""
        self._branch_parent = parent_id
        self._current = parent_id
        self._save_state()

    def switch_to(self, node_id: str) -> list[Message]:
        """切换到某个版本并返回它对应的完整对话（不重新请求模型）。"""
        leaf = self.leaf_under(node_id)
        path = self.path_messages(leaf)
        if not path:
            return []
        self._current = leaf
        self._branch_parent = leaf
        # 记下这次选择，下次回溯到同一父节点时优先回到它
        for message in path:
            self._preferred[message.parent_id or ""] = message.message_id
        self._save_state()
        return path

    def set_current(self, leaf_id: str | None) -> None:
        self._current = leaf_id
        self._branch_parent = leaf_id
        self._save_state()

    @staticmethod
    def branch_parent_for(prefix: list[Message]) -> str | None:
        """为新分支挑一个正确的父节点（被编辑消息之前的那条「真实」消息）。

        agent 会在用户消息前后插入 ``<system-reminder>`` / 环境说明，这些注入
        消息不算对话轮次。如果直接把新问题挂到「前缀最后一条」上，编辑第一轮时
        父节点就会变成一个注入的提醒，于是同一个位置的两个版本落进不同子树，
        ‹ 1/2 › 再也找不到对方。所以从后往前跳过这些注入消息。
        """
        for message in reversed(prefix):
            if is_human_message(message) or message.role == "assistant" or message.tool_results:
                return message.message_id or None
        return None


# ---------------------------------------------------------------------------
# ResumeResult
# ---------------------------------------------------------------------------


@dataclass
class ResumeResult:
    session: Session
    messages: list[Message]
    last_active: datetime


# ---------------------------------------------------------------------------
# Session 摘要生成
# ---------------------------------------------------------------------------


async def generate_session_summary(
    client: Any, conversation: ConversationManager, protocol: str
) -> str:
    from aieb.tools.base import StreamEnd, TextDelta

    recent = conversation.history[-10:]
    if not recent:
        return ""

    summary_conv = ConversationManager()
    summary_conv.history = [Message(role="user", content=SESSION_SUMMARY_PROMPT)]
    for msg in recent:
        summary_conv.history.append(msg)
    summary_conv.history.append(
        Message(role="user", content="请用一句话总结上面的对话内容。不要调用工具。")
    )

    collected = ""
    try:
        async for event in client.stream(
            summary_conv, system=SESSION_SUMMARY_PROMPT
        ):
            if isinstance(event, TextDelta):
                collected += event.text
            elif isinstance(event, StreamEnd):
                pass
    except Exception:
        return ""

    return collected.strip()


# ---------------------------------------------------------------------------
# SessionManager
# ---------------------------------------------------------------------------


def _generate_session_id() -> str:
    now = datetime.now()
    suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=4))
    return f"session_{now.strftime('%Y%m%d_%H%M%S')}_{suffix}"


class SessionManager:
    def __init__(self, work_dir: str) -> None:
        self._sessions_dir = Path(work_dir) / SESSIONS_DIR
        self._sessions_dir.mkdir(parents=True, exist_ok=True)

    @property
    def sessions_dir(self) -> Path:
        """会话文件所在目录（删除清理时需要枚举具体文件）。"""
        return self._sessions_dir


    def create(self) -> Session:
        session_id = _generate_session_id()
        jsonl_path = self._sessions_dir / f"{session_id}.jsonl"
        meta = SessionMeta(id=session_id)
        meta.save(self._sessions_dir / f"{session_id}.meta")

        file = open(jsonl_path, "a", encoding="utf-8")  # noqa: SIM115
        return Session(
            session_id=session_id,
            file=file,
            meta=meta,
            sessions_dir=self._sessions_dir,
        )


    def list(self) -> list[SessionMeta]:
        metas: list[SessionMeta] = []
        for meta_path in self._sessions_dir.glob("*.meta"):
            meta = SessionMeta.load(meta_path)
            if meta is not None:
                metas.append(meta)
        metas.sort(key=lambda m: m.last_active, reverse=True)
        return metas

    def resume(self, session_id: str) -> ResumeResult | None:
        jsonl_path = self._sessions_dir / f"{session_id}.jsonl"
        meta_path = self._sessions_dir / f"{session_id}.meta"

        if not jsonl_path.exists():
            return None

        meta = SessionMeta.load(meta_path)
        if meta is None:
            return None

        records: list[SessionRecord] = []
        with open(jsonl_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                record = SessionRecord.from_jsonl(line)
                if record is not None:
                    records.append(record)

        # 重建压缩后的状态：仅从最后一个 compact_boundary 开始重放。
        # 该标记之前的 record 是已被摘要过的原始前缀——保留在磁盘上供审计，
        # 但不再重放。标记本身内联了摘要 + 原样 keep 尾部，标记之后追加的
        # 普通消息（续写）照常重放。没有 boundary 则全量重放（兼容旧 session）。
        last_boundary = -1
        for i, rec in enumerate(records):
            if rec.type == RecordType.COMPACT_BOUNDARY:
                last_boundary = i
        if last_boundary >= 0:
            records = records[last_boundary:]

        valid_count = validate_message_chain(records)
        records = records[:valid_count]
        messages = records_to_messages(records)

        # 给旧记录（没有 message_id 的历史会话）补上确定性 ID 并串成链。
        # jsonl 只追加、旧记录顺序不变，所以同一批旧记录每次加载都会得到同一
        # 批 ID —— 新分支写下的 parent_id 在重启后仍然对得上，无需迁移文件。
        previous: str | None = None
        for index, message in enumerate(messages):
            if not message.message_id:
                message.message_id = legacy_message_id(index + 1)
                message.parent_id = previous
            previous = message.message_id
        known = {m.message_id for m in messages}
        previous = None
        for message in messages:
            # 压缩边界会把前缀连同它们的 ID 一起裁掉：父节点不存在时把链条接回
            # 文件顺序里的上一条，避免恢复出来的历史被截成一两轮。
            if message.parent_id is not None and message.parent_id not in known:
                message.parent_id = previous
            previous = message.message_id

        file = open(jsonl_path, "a", encoding="utf-8")  # noqa: SIM115
        session = Session(
            session_id=session_id,
            file=file,
            meta=meta,
            sessions_dir=self._sessions_dir,
        )
        session.load_tree(messages)

        # 当前显示的是「当前叶子」所在的那条分支（可能是编辑后新建的分支）。
        leaf = session.current
        path = session.path_messages(leaf) if leaf else []
        if not path:
            path = list(messages)
        # 崩溃/取消可能留下「有 tool_use 却无 tool_result」的尾巴：只裁剪尾部，
        # 不牵动中间其它分支（那正是分支模型与线性截断的区别）。
        trimmed = trim_incomplete_tail(path)
        if len(trimmed) != len(path):
            path = trimmed
        session.set_current(path[-1].message_id if path else None)

        return ResumeResult(
            session=session,
            messages=path,
            last_active=meta.last_active,
        )

    def rename(self, session_id: str, title: str) -> bool:
        meta_path = self._sessions_dir / f"{session_id}.meta"
        meta = SessionMeta.load(meta_path)
        if meta is None:
            return False
        meta.title = title
        meta.save(meta_path)
        return True

    def delete(self, session_id: str) -> bool:
        """删除一个会话在磁盘上的全部文件。

        顺序很关键：**先删 ``.meta``**。侧边栏是靠枚举 ``*.meta`` 列出会话的，
        所以只要元数据没了它就立刻从列表消失 —— 即使正文文件因被占用而暂时
        删不掉，用户也不会看到"点了删除却还在列表里"。

        Windows 上如果另一个进程（典型场景：同一工作区跑了第二个服务实例）
        仍打开着 ``.jsonl``，``unlink`` 会抛 ``WinError 32``。此时元数据与
        分支状态已经清掉，只把正文文件留下并抛出 ``SessionDeleteError``，
        让上层把"文件被占用"如实告诉用户，而不是假装删除成功。
        """
        meta_path = self._sessions_dir / f"{session_id}.meta"
        state_path = self._sessions_dir / f"{session_id}.branches.json"
        tmp_path = self._sessions_dir / f"{session_id}.branches.json.tmp"
        jsonl_path = self._sessions_dir / f"{session_id}.jsonl"

        deleted = False
        locked: list[str] = []
        # 元数据 / 分支状态先后删，正文最后删
        for path in (meta_path, state_path, tmp_path, jsonl_path):
            if not path.exists():
                continue
            for attempt in range(3):
                try:
                    path.unlink()
                    deleted = True
                    break
                except FileNotFoundError:
                    deleted = True
                    break
                except PermissionError:
                    # Windows 文件被占用：短暂重试，杀毒/索引抖动常常一次就过
                    time.sleep(0.15 * (attempt + 1))
                except OSError:
                    break
            else:
                locked.append(path.name)
                continue
            if path.exists():
                locked.append(path.name)

        if locked:
            raise SessionDeleteError(
                "会话正文文件被其它进程占用，无法彻底删除："
                + "、".join(locked)
                + "（常见原因：同一工作区还开着另一个 AI 外脑服务实例，"
                "请关闭多余实例后重试；会话已从列表移除，不影响其它对话）"
            )
        return deleted

    def cleanup(self, max_age_days: int = DEFAULT_MAX_AGE_DAYS) -> int:
        cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
        removed = 0

        for meta_path in list(self._sessions_dir.glob("*.meta")):
            meta = SessionMeta.load(meta_path)
            if meta is not None and meta.last_active < cutoff:
                try:
                    self.delete(meta.id)
                except (OSError, SessionDeleteError):
                    # 某个会话的文件被占用不应该让整轮清理中断
                    continue
                removed += 1

        return removed
