"""openai-compat 客户端的 token 用量回传回归测试。

回归的 bug：DeepSeek 等兼容 provider 把 ``usage`` 挂在**最后一个带 choices
的分块**上，而不像 OpenAI 那样额外下发一个"只有 usage"的收尾分块。旧实现只在
``not chunk.choices`` 分支里读 usage，于是这类 provider 永远不会产出
``StreamEnd``，上游（Agent 会话统计、Benchmark 成本核算）拿到的 token 恒为 0。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from aieb.client import OpenAICompatClient
from aieb.conversation import ConversationManager, Message
from aieb.tools.base import StreamEnd, TextDelta


def _chunk(content: str | None = None, finish: str | None = None, usage=None):
    delta = SimpleNamespace(content=content, reasoning_content=None, tool_calls=None)
    choice = SimpleNamespace(delta=delta, finish_reason=finish)
    return SimpleNamespace(choices=[choice] if content is not None or finish else [], usage=usage)


def _usage(prompt: int, completion: int):
    return SimpleNamespace(
        prompt_tokens=prompt,
        completion_tokens=completion,
        prompt_tokens_details=None,
    )


class _FakeCompletions:
    def __init__(self, chunks):
        self._chunks = chunks

    async def create(self, **_kwargs):
        async def gen():
            for chunk in self._chunks:
                yield chunk

        return gen()


def _client(chunks) -> OpenAICompatClient:
    client = object.__new__(OpenAICompatClient)
    client.model = "fake-model"
    client.provider_name = "fake"
    client.max_output_tokens = 16
    client._client = SimpleNamespace(
        chat=SimpleNamespace(completions=_FakeCompletions(chunks))
    )
    return client


def _collect(client: OpenAICompatClient) -> list:
    conv = ConversationManager()
    conv.history = [Message(role="user", content="你好")]

    async def run():
        return [event async for event in client.stream(conv, system="sys")]

    return asyncio.run(run())


def test_stream_end_emitted_when_usage_shares_final_choice_chunk():
    """DeepSeek 风格：usage 与最后一个 finish_reason=stop 的分块同一块。"""
    events = _collect(
        _client(
            [
                _chunk("收", None),
                _chunk("到", "stop", usage=_usage(120, 7)),
            ]
        )
    )
    text = "".join(e.text for e in events if isinstance(e, TextDelta))
    ends = [e for e in events if isinstance(e, StreamEnd)]
    assert text == "收到"
    assert len(ends) == 1
    assert ends[0].input_tokens == 120
    assert ends[0].output_tokens == 7
    assert ends[0].stop_reason == "end_turn"


def test_stream_end_emitted_for_openai_style_trailing_usage_chunk():
    """OpenAI 风格：usage 在独立的无 choices 收尾块里，且只发一次 StreamEnd。"""
    events = _collect(
        _client(
            [
                _chunk("好", None),
                _chunk(None, "stop"),
                SimpleNamespace(choices=[], usage=_usage(50, 3)),
            ]
        )
    )
    ends = [e for e in events if isinstance(e, StreamEnd)]
    assert len(ends) == 1
    assert (ends[0].input_tokens, ends[0].output_tokens) == (50, 3)


def test_cache_read_is_subtracted_from_prompt_tokens():
    """prompt_tokens 含缓存命中数时保持 input + cache_read 可加性。"""
    usage = SimpleNamespace(
        prompt_tokens=100,
        completion_tokens=5,
        prompt_tokens_details=SimpleNamespace(cached_tokens=40),
    )
    events = _collect(_client([_chunk("ok", "stop", usage=usage)]))
    end = next(e for e in events if isinstance(e, StreamEnd))
    assert end.input_tokens == 60
    assert end.cache_read == 40


def test_no_stream_end_without_usage():
    """provider 完全不回传 usage 时不能凭空编造 token 数。"""
    events = _collect(_client([_chunk("ok", "stop")]))
    assert not [e for e in events if isinstance(e, StreamEnd)]
