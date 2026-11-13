"""LangChain 适配层测试：ChatModel / Tool / Structured Output 必须真的能用。

默认全部用 Fake 客户端（不打网络）。需要真实 API 的用例用
`AIEB_TEST_LIVE_LLM=1` 显式开启（会消耗 1 次 DeepSeek 调用）。
"""

from __future__ import annotations

import os

import pytest

pytest.importorskip("langchain_core")
pytest.importorskip("langgraph")

from langchain_core.messages import HumanMessage, SystemMessage  # noqa: E402
from pydantic import BaseModel  # noqa: E402

from aieb.brain.meetings import MeetingAnalysis, RiskIn, TodoIn  # noqa: E402
from aieb.config import ProviderConfig  # noqa: E402
from aieb.tools.base import StreamEnd, TextDelta, Tool, ToolResult  # noqa: E402
from aieb.workflow import langchain_adapter as adapter  # noqa: E402


def provider() -> ProviderConfig:
    return ProviderConfig(
        name="offline",
        protocol="openai-compat",
        base_url="http://127.0.0.1:9/x",
        model="deepseek-chat",
        api_key="offline",
    )


class FakeClient:
    def __init__(self, text: str, usage: tuple[int, int] = (11, 7)) -> None:
        self.text = text
        self.usage = usage

    async def stream(self, conv, **kwargs):  # noqa: ANN001
        yield TextDelta(self.text)
        yield StreamEnd(stop_reason="stop", input_tokens=self.usage[0], output_tokens=self.usage[1])


# ---------------------------------------------------------------------------
# ChatModel
# ---------------------------------------------------------------------------


def test_chat_model_generates_text_and_records_usage(monkeypatch) -> None:
    monkeypatch.setattr(
        adapter, "create_client", lambda cfg: FakeClient('{"ok": true}', usage=(21, 13))
    )
    model = adapter.build_chat_model(provider())
    result = model.invoke(
        [SystemMessage(content="你是助手"), HumanMessage(content="说一句话")]
    )
    assert result.content == '{"ok": true}'
    assert model.last_usage == {"input_tokens": 21, "output_tokens": 13}
    assert model._llm_type == "aieb-deepseek"


def test_chat_model_streams_chunks(monkeypatch) -> None:
    monkeypatch.setattr(adapter, "create_client", lambda cfg: FakeClient("流式输出"))
    model = adapter.build_chat_model(provider())
    chunks = list(model.stream([HumanMessage(content="hi")]))
    assert "".join(chunk.content for chunk in chunks) == "流式输出"


def test_chat_model_maps_roles(monkeypatch) -> None:
    captured: dict[str, str] = {}

    class Echo(FakeClient):
        async def stream(self, conv, *, system=""):  # noqa: ANN001
            captured["system"] = system
            captured["prompt"] = conv.history[-1].content
            yield TextDelta("ok")

    monkeypatch.setattr(adapter, "create_client", lambda cfg: Echo("ok"))
    model = adapter.build_chat_model(provider())
    model.invoke([SystemMessage(content="系统指令"), HumanMessage(content="用户问题")])
    assert captured["system"] == "系统指令"
    assert captured["prompt"] == "用户问题"


# ---------------------------------------------------------------------------
# Tool 适配
# ---------------------------------------------------------------------------


class _EchoParams(BaseModel):
    text: str = ""


class _EchoTool(Tool):
    name = "Echo"
    description = "原样返回输入"
    params_model = _EchoParams

    async def execute(self, params):  # noqa: ANN001
        return ToolResult(output=f"echo:{params.text}")


def test_tool_adapter_preserves_schema_and_runs() -> None:
    tool = adapter.to_langchain_tool(_EchoTool())
    assert tool.name == "Echo"
    assert tool.description == "原样返回输入"
    assert tool.args_schema is _EchoParams
    assert tool.invoke({"text": "hi"}) == "echo:hi"


# ---------------------------------------------------------------------------
# 结构化输出
# ---------------------------------------------------------------------------


class _StubModel:
    """只实现 `ainvoke` 的桩模型（pydantic 模型不允许随便挂属性）。"""

    def __init__(self, content: str) -> None:
        self.content = content
        self.last_usage: dict[str, int] = {}

    async def ainvoke(self, messages, **kwargs):  # noqa: ANN001
        class _Msg:
            content = self.content

        return _Msg()


def test_structured_output_parses_pydantic() -> None:
    payload = MeetingAnalysis(
        title="周会",
        summary="延期",
        risks=[RiskIn(title="延期", level="high", evidence="延期两周")],
        todos=[TodoIn(content="补测试", owner="李娜")],
    ).model_dump_json()
    model = _StubModel(payload)
    import asyncio

    result = asyncio.run(
        adapter.structured_output(
            model, schema=MeetingAnalysis, system="analyze", prompt="会议原文"
        )
    )
    assert isinstance(result, MeetingAnalysis)
    assert result.risks[0].title == "延期"


def test_structured_output_raises_on_bad_json() -> None:
    model = _StubModel("这不是 JSON")
    import asyncio

    with pytest.raises(Exception):
        asyncio.run(
            adapter.structured_output(
                model, schema=MeetingAnalysis, system="analyze", prompt="原文"
            )
        )


# ---------------------------------------------------------------------------
# 与工作流 deps 的接线
# ---------------------------------------------------------------------------


def test_real_deps_langchain_branch_uses_same_prompt(monkeypatch, tmp_path) -> None:
    """`AIEB_WORKFLOW_LLM=langchain` 时，analyzer 走适配层且 prompt 与直接路径一致。"""
    from aieb.brain import meetings as meetings_mod
    from aieb.brain.store import BrainStore
    from aieb.workflow.deps import build_real_deps
    from aieb.workflow.persistence import WorkflowStore

    payload = MeetingAnalysis(
        title="周会", summary="延期", risks=[], todos=[]
    ).model_dump_json()
    seen: dict[str, str] = {}

    def fake_create_client(cfg):  # noqa: ANN001
        class _C(FakeClient):
            async def stream(self, conv, *, system=""):  # noqa: ANN001
                seen["system"] = system
                seen["prompt"] = conv.history[-1].content
                yield TextDelta(payload)

        return _C(payload)

    monkeypatch.setattr(adapter, "create_client", fake_create_client)
    monkeypatch.setenv("AIEB_WORKFLOW_LLM", "langchain")

    class _Service:
        def __init__(self) -> None:
            self.provider = provider()
            self.store = BrainStore(tmp_path)
            self.store.add_activity = lambda *a, **k: None

    service = _Service()
    deps = build_real_deps(
        service=service,
        wf_store=WorkflowStore(tmp_path / ".aieb" / "workflow" / "workflow.sqlite3"),
        workspace=tmp_path,
    )
    import asyncio

    outcome = asyncio.run(deps.analyzer("周会.txt", "会议原文" * 20))
    assert isinstance(outcome.analysis, MeetingAnalysis)
    expected_prompt = meetings_mod.build_analysis_prompt("会议原文" * 20, "周会.txt")
    assert expected_prompt.strip() in seen["prompt"].strip()
    service.store.close()


# ---------------------------------------------------------------------------
# 真实 API（默认跳过）
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    os.environ.get("AIEB_TEST_LIVE_LLM", "") not in {"1", "true", "yes"},
    reason="需要真实 LLM：设置 AIEB_TEST_LIVE_LLM=1 并配置 DEEPSEEK_API_KEY",
)
def test_live_deepseek_roundtrip() -> None:
    key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not key:
        pytest.skip("缺少 DEEPSEEK_API_KEY")
    model = adapter.build_chat_model(
        ProviderConfig(
            name="live",
            protocol="openai-compat",
            base_url="https://api.deepseek.com",
            model="deepseek-chat",
            api_key=key,
        )
    )
    message = model.invoke([HumanMessage(content="只回复两个字：收到")])
    assert "收到" in str(message.content)
    assert model.last_usage.get("input_tokens", 0) > 0
