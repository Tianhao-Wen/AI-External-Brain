"""单轮 LLM 调用封装（会议结构化分析、知识问答、PPT 大纲、记忆抽取都用它）。

复用产品已有的 provider/client 体系，因此换模型、换 key 的行为与对话完全一致；
这里只负责"发一次请求、收完整文本、必要时解析 JSON"。
"""

from __future__ import annotations

import json
import re
from typing import Any

from aieb.client import create_client
from aieb.config import ProviderConfig
from aieb.conversation import ConversationManager, Message
from aieb.tools.base import StreamEnd, TextDelta


class LLMError(RuntimeError):
    """模型调用失败（网络、鉴权、超时等），调用方需转成用户可读提示。"""


async def complete(
    provider: ProviderConfig,
    system: str,
    prompt: str,
    *,
    timeout: float = 120.0,
    usage_out: dict[str, int] | None = None,
) -> str:
    """单轮补全，返回完整文本。

    `usage_out`：传入字典时，会把 provider 回传的**真实** token 用量写进去
    （`input_tokens` / `output_tokens`），用于和上下文预算的估算值对照。
    """
    import asyncio

    async def _run() -> str:
        client = create_client(provider)
        conv = ConversationManager()
        conv.history = [Message(role="user", content=prompt)]
        parts: list[str] = []
        async for event in client.stream(conv, system=system):
            if isinstance(event, TextDelta):
                parts.append(event.text)
            elif isinstance(event, StreamEnd):
                if usage_out is not None:
                    usage_out["input_tokens"] = int(getattr(event, "input_tokens", 0) or 0)
                    usage_out["output_tokens"] = int(getattr(event, "output_tokens", 0) or 0)
                break
        return "".join(parts).strip()

    try:
        return await asyncio.wait_for(_run(), timeout=timeout)
    except asyncio.TimeoutError as exc:
        raise LLMError("模型响应超时，请稍后重试") from exc
    except LLMError:
        raise
    except Exception as exc:  # noqa: BLE001 - 统一转成可读错误
        message = str(exc) or exc.__class__.__name__
        raise LLMError(f"模型调用失败：{message}") from exc


def extract_json(text: str) -> Any:
    """从模型输出里取出 JSON（容忍 ```json 代码块和前后解释文字）。"""
    if not text:
        raise LLMError("模型没有返回内容")
    cleaned = text.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", cleaned, re.DOTALL)
    if fence:
        cleaned = fence.group(1).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(cleaned[start:end + 1])
        except json.JSONDecodeError as exc:
            raise LLMError("模型返回的结构化结果不是合法 JSON") from exc
    raise LLMError("模型返回的结构化结果不是合法 JSON")


async def complete_json(
    provider: ProviderConfig,
    system: str,
    prompt: str,
    *,
    timeout: float = 120.0,
    usage_out: dict[str, int] | None = None,
) -> Any:
    raw = await complete(provider, system, prompt, timeout=timeout, usage_out=usage_out)
    return extract_json(raw)
