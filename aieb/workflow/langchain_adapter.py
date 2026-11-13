"""LangChain 适配层：把现有 LLM 客户端与工具**标准化**，而不是重写一套。

设计原则（避免"为了框架而重复造轮子"）：

* 底层仍然走项目自己的 `aieb.client.create_client`（DeepSeek/openai-compat 的流式、
  token 计量、错误翻译都已经过测试），**不引入第二套 HTTP 栈**；
* 对外暴露 LangChain 的 `BaseChatModel`、`StructuredTool`、`Pydantic` 结构化输出，
  让 LangGraph / LangChain 生态（Retriever、Evaluator、LangSmith）可以直接复用；
* 默认**不启用**：工作流节点默认用现有 `aieb.brain.llm`；把
  `AIEB_WORKFLOW_LLM=langchain` 打开时才走本适配层，两条路径行为一致（同一 prompt、
  同一 provider、同样记录 token）。

本模块的每个能力都有对应测试；真实 API 连通性测试需要 `AIEB_TEST_LIVE_LLM=1`。
"""

from __future__ import annotations

import json
from typing import Any, Iterator, Sequence

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage, HumanMessage, SystemMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_core.tools import StructuredTool
from pydantic import BaseModel

from aieb.brain.llm import extract_json
from aieb.client import create_client
from aieb.config import ProviderConfig
from aieb.conversation import ConversationManager, Message
from aieb.tools.base import TextDelta as AiebTextDelta


def _to_prompt(messages: Sequence[BaseMessage]) -> tuple[str, str]:
    """LangChain 消息 → 现有客户端的 (system, user prompt)。"""
    system_parts: list[str] = []
    user_parts: list[str] = []
    for message in messages:
        content = message.content if isinstance(message.content, str) else json.dumps(
            message.content, ensure_ascii=False
        )
        if isinstance(message, SystemMessage):
            system_parts.append(content)
        elif isinstance(message, HumanMessage):
            user_parts.append(content)
        else:
            role = getattr(message, "type", "user")
            user_parts.append(f"[{role}] {content}")
    return "\n".join(system_parts), "\n".join(user_parts)


class DeepSeekChatModel(BaseChatModel):
    """把 `ProviderConfig` 包成 LangChain ChatModel（复用现有流式客户端）。"""

    provider: Any
    temperature: float = 0.0
    timeout: float = 120.0
    last_usage: dict[str, int] = {}

    model_config = {"arbitrary_types_allowed": True}

    @property
    def _llm_type(self) -> str:
        return "aieb-deepseek"

    @property
    def _identifying_params(self) -> dict[str, Any]:
        return {"model": getattr(self.provider, "model", ""), "base_url": getattr(self.provider, "base_url", "")}

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        system, prompt = _to_prompt(messages)
        chunks = list(self._stream_sync(system, prompt, run_manager))
        text = "".join(chunks)
        return ChatResult(
            generations=[ChatGeneration(message=AIMessage(content=text))],
            llm_output={"usage": dict(self.last_usage)},
        )

    def _stream_sync(
        self,
        system: str,
        prompt: str,
        run_manager: CallbackManagerForLLMRun | None,
    ) -> Iterator[str]:
        import asyncio

        async def _run() -> str:
            client = create_client(self.provider)
            conv = ConversationManager()
            conv.history = [Message(role="user", content=prompt)]
            parts: list[str] = []
            async for event in client.stream(conv, system=system):
                if isinstance(event, AiebTextDelta):
                    parts.append(event.text)
                    if run_manager is not None:
                        run_manager.on_llm_new_token(event.text)
                elif hasattr(event, "input_tokens") and hasattr(event, "output_tokens"):
                    self.last_usage = {
                        "input_tokens": int(getattr(event, "input_tokens", 0) or 0),
                        "output_tokens": int(getattr(event, "output_tokens", 0) or 0),
                    }
            return "".join(parts)

        text = asyncio.run(asyncio.wait_for(_run(), timeout=self.timeout))
        yield text

    def _stream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> Iterator[ChatGenerationChunk]:
        system, prompt = _to_prompt(messages)
        for piece in self._stream_sync(system, prompt, run_manager):
            yield ChatGenerationChunk(message=AIMessageChunk(content=piece))


def build_chat_model(provider: ProviderConfig, **kwargs: Any) -> DeepSeekChatModel:
    return DeepSeekChatModel(provider=provider, **kwargs)


def to_langchain_tool(tool: Any) -> StructuredTool:
    """把项目内的 `Tool` 包成 LangChain `StructuredTool`（参数 schema 原样复用）。

    同时提供 `func` 与 `coroutine`：同步 `invoke` 与异步 `ainvoke` 都能用。
    """

    async def _run(**kwargs: Any) -> str:
        params = tool.params_model(**kwargs)
        result = await tool.execute(params)
        return result.output

    def _run_sync(**kwargs: Any) -> str:
        import asyncio

        return asyncio.run(_run(**kwargs))

    return StructuredTool(
        name=tool.name,
        description=tool.description,
        args_schema=tool.params_model,
        func=_run_sync,
        coroutine=_run,
    )


async def structured_output(
    model: BaseChatModel,
    *,
    schema: type[BaseModel],
    system: str,
    prompt: str,
) -> BaseModel:
    """Pydantic 结构化输出：模型必须只回 JSON，解析失败直接抛错（不塞半成品）。"""
    instruction = (
        f"{system}\n\n只输出一个 JSON 对象，字段严格符合如下 JSON Schema：\n"
        f"{json.dumps(schema.model_json_schema(), ensure_ascii=False)}"
    )
    message = await model.ainvoke([SystemMessage(content=instruction), HumanMessage(content=prompt)])
    data = extract_json(message.content if isinstance(message.content, str) else str(message.content))
    return schema.model_validate(data)


def adapters_summary(provider: ProviderConfig, tools: Sequence[Any]) -> dict[str, Any]:
    """给诊断/文档用的自检信息（不调用网络）。"""
    model = build_chat_model(provider)
    return {
        "chat_model": model._llm_type,
        "model_name": getattr(provider, "model", ""),
        "tools": [to_langchain_tool(t).name for t in tools],
    }
