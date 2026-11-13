from __future__ import annotations

import base64
import hashlib
import json
from typing import Any

from aieb.conversation import DocumentBlock, Message

# 把 provider 无关的内部消息序列化成各家 API 的请求格式。
# 这一层属于「适配器」职责，对话层（ConversationManager）只管消息、不懂线上格式。

# ── OpenAI 兼容协议的 PDF 处理 ────────────────────────────────────
# Chat Completions 的 {"type": "file"} 内容块只有极少数 provider 真正支持。
# DeepSeek 会直接返回 400：
#   messages[5]: file must have a file_id or file_data
# 因此对 openai-compat 协议，一律在本地把 PDF 抽成「带页码的文本」注入，
# 既保证任何 model 都能读，又保留【第N页】页码便于回答时引用。
MAX_DOCUMENT_CHARS = 16000  # 单份文档注入上限（约 4.5k token）
MAX_DOCUMENTS_CHARS_TOTAL = 32000  # 一轮对话中文档文本总量上限

_PDF_TEXT_CACHE: dict[str, str] = {}
_PDF_TEXT_CACHE_LIMIT = 32


def _pdf_text_from_base64(data: str) -> str:
    """把 PDF 的 base64 抽取成按页纯文本（带【第N页】标记）。"""
    if not data:
        return ""
    key = hashlib.sha1(data.encode("ascii", "ignore")).hexdigest()
    cached = _PDF_TEXT_CACHE.get(key)
    if cached is not None:
        return cached

    text = ""
    try:
        raw = base64.b64decode(data, validate=False)
    except Exception:
        raw = b""
    if raw:
        try:
            try:
                import pymupdf as fitz  # PyMuPDF >= 1.24
            except ImportError:  # pragma: no cover - 老版本 PyMuPDF
                import fitz  # type: ignore[no-redef]

            with fitz.open(stream=raw, filetype="pdf") as doc:
                if doc.needs_pass:
                    text = "（PDF 已加密，无法读取原文）"
                else:
                    parts = []
                    for index, page in enumerate(doc, start=1):
                        page_text = (page.get_text("text") or "").strip()
                        if page_text:
                            parts.append(f"【第{index}页】\n{page_text}")
                    text = "\n".join(parts)
        except ImportError:
            text = "（服务端缺少 PDF 解析组件，无法读取原文）"
        except Exception:  # noqa: BLE001 - 解析失败不能让整轮对话失败
            text = "（PDF 解析失败，无法读取原文）"

    if len(_PDF_TEXT_CACHE) >= _PDF_TEXT_CACHE_LIMIT:
        _PDF_TEXT_CACHE.clear()
    _PDF_TEXT_CACHE[key] = text
    return text


def _document_content_blocks(
    documents: list[DocumentBlock], budget: list[int]
) -> list[dict[str, Any]]:
    """把文档转成纯文本块；budget 是剩余可用字符数（单元素列表，跨文档递减）。"""
    blocks: list[dict[str, Any]] = []
    for doc in documents:
        body = _pdf_text_from_base64(doc.data).strip()
        if not body:
            body = "（该文档没有可提取的文字，可能是扫描件；如需引用请使用知识库检索）"
        limit = min(MAX_DOCUMENT_CHARS, max(budget[0], 0))
        if limit <= 0:
            body = "（本轮对话中的文档内容过多，已省略；需要时请让我按关键词检索知识库）"
        elif len(body) > limit:
            body = (
                body[:limit]
                + "\n……（原文过长已截断；完整内容已存入知识库，可让我检索具体段落）"
            )
        budget[0] -= min(len(body), limit)
        name = doc.name or "文档"
        blocks.append({
            "type": "text",
            "text": f"以下是用户上传的文档《{name}》原文（按页标注）：\n{body}",
        })
    return blocks


def build_anthropic_messages(messages: list[Message]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for m in messages:
        if m.tool_uses or m.thinking_blocks:
            content: list[dict[str, Any]] = []
            for tb in m.thinking_blocks:
                content.append({
                    "type": "thinking",
                    "thinking": tb.thinking,
                    "signature": tb.signature,
                })
            if m.content:
                content.append({"type": "text", "text": m.content})
            for tu in m.tool_uses:
                content.append({
                    "type": "tool_use",
                    "id": tu.tool_use_id,
                    "name": tu.tool_name,
                    "input": tu.arguments,
                })
            if not content:
                content.append({"type": "text", "text": ""})
            result.append({"role": "assistant", "content": content})
        elif m.tool_results:
            content = []
            for tr in m.tool_results:
                content.append({
                    "type": "tool_result",
                    "tool_use_id": tr.tool_use_id,
                    "content": tr.content,
                    "is_error": tr.is_error,
                })
            result.append({"role": "user", "content": content})
        elif m.images or m.documents:
            content = [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": ib.media_type,
                        "data": ib.data,
                    },
                }
                for ib in m.images
            ]
            content.extend(
                {
                    "type": "document",
                    "source": {
                        "type": "base64",
                        "media_type": db.media_type,
                        "data": db.data,
                    },
                    **({"title": db.name} if db.name else {}),
                }
                for db in m.documents
            )
            if m.content:
                content.append({"type": "text", "text": m.content})
            result.append({"role": m.role, "content": content})
        else:
            # 合并连续的 user 纯文本消息（system-reminder 或普通 user 文本）。
            # 不合并到 tool_result 类型的 user 消息中（content 是 list）。
            if (
                m.role == "user"
                and result
                and result[-1]["role"] == "user"
                and isinstance(result[-1]["content"], str)
            ):
                result[-1]["content"] = result[-1]["content"] + "\n" + m.content
            else:
                result.append({"role": m.role, "content": m.content})
    return result


def build_openai_input(messages: list[Message]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for m in messages:
        if m.tool_uses:
            # Responses API: thinking blocks 作为 reasoning item 回传。
            for tb in (m.thinking_blocks or []):
                result.append({
                    "type": "reasoning",
                    "id": tb.signature,
                    "summary": [{"type": "summary_text", "text": tb.thinking}],
                })
            if m.content:
                result.append({"role": "assistant", "content": m.content})
            for tu in m.tool_uses:
                result.append({
                    "type": "function_call",
                    "name": tu.tool_name,
                    "call_id": tu.tool_use_id,
                    "arguments": json.dumps(tu.arguments),
                })
        elif m.tool_results:
            for tr in m.tool_results:
                result.append({
                    "type": "function_call_output",
                    "call_id": tr.tool_use_id,
                    "output": tr.content,
                })
        else:
            # 非 tool 的 assistant 消息也回传 reasoning。
            for tb in (m.thinking_blocks or []):
                result.append({
                    "type": "reasoning",
                    "id": tb.signature,
                    "summary": [{"type": "summary_text", "text": tb.thinking}],
                })
            result.append({"role": m.role, "content": m.content})
    return result


def build_chat_completion_messages(messages: list[Message]) -> list[dict[str, Any]]:
    """OpenAI Chat Completions 格式。

    - 用户消息：{"role": "user", "content": "..."}
    - 用户图片：{"role": "user", "content": [{"type": "text", "text": "..."}, {"type": "image_url", "image_url": {"url": "data:..."}}]}
    - 助手文本+工具调用：{"role": "assistant", "content": "...", "tool_calls": [...]} 
    - 工具结果：{"role": "tool", "tool_call_id": "...", "content": "..."}
    - thinking 块作为 reasoning_content 回传（DeepSeek/小米等 provider 要求）。
    """
    result: list[dict[str, Any]] = []
    for m in messages:
        reasoning = "".join(tb.thinking for tb in m.thinking_blocks) if m.thinking_blocks else ""

        if m.tool_uses:
            tool_calls = []
            for tu in m.tool_uses:
                tool_calls.append({
                    "id": tu.tool_use_id,
                    "type": "function",
                    "function": {
                        "name": tu.tool_name,
                        "arguments": json.dumps(tu.arguments),
                    },
                })
            msg: dict[str, Any] = {
                "role": "assistant",
                "content": m.content or None,
                "tool_calls": tool_calls,
            }
            if reasoning:
                msg["reasoning_content"] = reasoning
            result.append(msg)
        elif m.tool_results:
            for tr in m.tool_results:
                result.append({
                    "role": "tool",
                    "tool_call_id": tr.tool_use_id,
                    "content": tr.content,
                })
        else:
            content = m.content
            if m.images or m.documents:
                content_blocks: list[dict[str, Any]] = []
                if m.content:
                    content_blocks.append({"type": "text", "text": m.content})
                for image in m.images:
                    content_blocks.append({
                        "type": "image_url",
                        "image_url": {"url": f"data:{image.media_type};base64,{image.data}"},
                    })
                # 文档不使用 {"type": "file"}：DeepSeek 等 openai-compat provider
                # 会以 400 拒绝（file must have a file_id or file_data）。
                # 统一在本地抽文本后注入，保证任何模型都能读到原文与页码。
                content_blocks.extend(
                    _document_content_blocks(
                        m.documents, [MAX_DOCUMENTS_CHARS_TOTAL]
                    )
                )
                content = content_blocks
            msg = {"role": m.role, "content": content}
            if reasoning:
                msg["reasoning_content"] = reasoning
            result.append(msg)
    return result


def build_messages(messages: list[Message], protocol: str = "anthropic") -> list[dict[str, Any]]:
    if protocol == "openai":
        return build_openai_input(messages)
    if protocol == "openai-compat":
        return build_chat_completion_messages(messages)
    return build_anthropic_messages(messages)
