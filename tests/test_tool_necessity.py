"""Phase 2B-3：Tool Necessity / 按需工具暴露 的测试。

两层：
1. **暴露结构**：探索型工具（Bash / Glob）默认不出现在首轮 schema，但可通过 ToolSearch 加载且之后保持可用；
   读/写/搜索（ReadFile / WriteFile / EditFile / Grep）保持直接可用，避免"每个文件任务多一次 ToolSearch"。
2. **行为层**：
   * 纯回忆（对话内 / session 恢复历史内已有答案）→ 0 工具调用即可给出 Final；
   * 真正需要工具的请求（搜索/写文件/读文件/执行命令）能力没有被关掉。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any, AsyncIterator

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from aieb.agent import Agent, LoopComplete, ToolUseEvent  # noqa: E402
from aieb.client import LLMClient  # noqa: E402
from aieb.conversation import ConversationManager, Message  # noqa: E402
from aieb.prompts import build_system_prompt  # noqa: E402
from aieb.tools import WORKSPACE_EXPLORE_TOOLS, create_default_registry  # noqa: E402
from aieb.tools.base import StreamEnd, StreamEvent, TextDelta, ToolCallComplete  # noqa: E402


class ScriptedClient(LLMClient):
    def __init__(self, responses: list[list[StreamEvent]]) -> None:
        self._responses = list(responses)
        self.call_count = 0
        # 记录每次 LLM 调用时看到的工具列表，用于验证暴露结构
        self.seen_tools: list[list[str]] = []

    async def stream(
        self,
        conversation: ConversationManager,
        system: str = "",
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        self.call_count += 1
        self.seen_tools.append(sorted(t.get("name", "") for t in (tools or [])))
        events = self._responses.pop(0) if self._responses else [
            TextDelta(text="done"),
            StreamEnd(stop_reason="end_turn", input_tokens=1, output_tokens=1),
        ]
        for event in events:
            yield event


def final(text: str) -> list[StreamEvent]:
    return [TextDelta(text=text), StreamEnd(stop_reason="end_turn", input_tokens=1, output_tokens=1)]


def tool_turn(tool_id: str, name: str, args: dict) -> list[StreamEvent]:
    return [ToolCallComplete(tool_id, name, args), StreamEnd(stop_reason="tool_use", input_tokens=1, output_tokens=1)]


def run(workspace: Path, script, history: list[Message] | None = None):
    client = ScriptedClient(script)
    registry = create_default_registry(work_dir=str(workspace))
    agent = Agent(client, registry, "anthropic", work_dir=str(workspace))
    conversation = ConversationManager()
    if history:
        conversation.history = list(history)
    conversation.add_user_message("go")

    events: list = []

    async def drive():
        async for event in agent.run(conversation):
            events.append(event)

    asyncio.run(drive())
    return {
        "client": client,
        "registry": registry,
        "tool_calls": [e for e in events if isinstance(e, ToolUseEvent)],
        "loops": [e for e in events if isinstance(e, LoopComplete)],
    }


@pytest.fixture()
def workspace(tmp_path):
    (tmp_path / "config.json").write_text('{"timeout": 42}', encoding="utf-8")
    (tmp_path / "a.py").write_text("TARGET\n", encoding="utf-8")
    return tmp_path


# ---------------------------------------------------------------- 1. 暴露结构


def test_explore_tools_are_deferred_by_default(workspace):
    registry = create_default_registry(work_dir=str(workspace))
    exposed = {s["name"] for s in registry.get_all_schemas()}
    assert set(WORKSPACE_EXPLORE_TOOLS) == {"Bash", "Glob"}
    for name in WORKSPACE_EXPLORE_TOOLS:
        assert name not in exposed, f"{name} 不应在首轮暴露"
        assert name in registry.get_deferred_tool_names()
    # 读/写/搜索保持直接可用
    for name in ("ReadFile", "WriteFile", "EditFile", "Grep"):
        assert name in exposed
    # 发现入口本身必须始终可见
    assert "ToolSearch" in exposed


def test_eager_mode_restores_old_behaviour(workspace):
    registry = create_default_registry(work_dir=str(workspace), lazy_explore_tools=False)
    exposed = {s["name"] for s in registry.get_all_schemas()}
    assert {"Bash", "Glob", "ReadFile", "Grep"} <= exposed


def test_toolsearch_loads_explore_tools_and_caches(workspace):
    registry = create_default_registry(work_dir=str(workspace))
    search = registry.get("ToolSearch")
    from aieb.tools.impl.tool_search import ToolSearchParams

    result = asyncio.run(search.execute(ToolSearchParams(query="select:Bash,Glob")))
    assert "Bash" in result.output and "Glob" in result.output
    exposed = {s["name"] for s in registry.get_all_schemas()}
    assert {"Bash", "Glob"} <= exposed, "加载后应持续可见（缓存）"


def test_conversation_first_contract_present():
    prompt = build_system_prompt()
    assert "# Answer from the conversation first" in prompt
    assert "restored session memory" in prompt
    assert "answer directly" in prompt
    # 陈述句 vs 请求句的分类原则（Phase 2B-3）
    assert "is it a request" in prompt and "a statement gets a short acknowledgement" in prompt.lower()
    for banned in ("BENCHMARK", "FILE_0", "TOOL_0", "case_id", "CTX_"):
        assert banned not in prompt


def test_deferred_tools_section_renders_only_when_needed():
    none = build_system_prompt()
    assert "# Tools that are not loaded yet" not in none

    with_deferred = build_system_prompt(deferred_tools=["Bash", "Glob"])
    assert "# Tools that are not loaded yet" in with_deferred
    assert "Bash, Glob" in with_deferred
    assert "ToolSearch" in with_deferred
    # 提示必须明确"不要为了看看工作区而加载"
    assert "do not load them to check the workspace" in with_deferred.lower()


def test_agent_prompt_lists_deferred_tools(workspace):
    """真实 Agent 运行时，系统提示应带上当前未加载的工具清单。"""
    client = ScriptedClient([final("ok")])
    registry = create_default_registry(work_dir=str(workspace))
    agent = Agent(client, registry, "anthropic", work_dir=str(workspace))
    conversation = ConversationManager()
    conversation.add_user_message("hi")

    prompts: list[str] = []
    original = client.stream

    async def spy(conv, system="", tools=None):
        prompts.append(system)
        async for event in original(conv, system=system, tools=tools):
            yield event

    async def drive():
        client.stream = spy  # type: ignore[assignment]
        async for _ in agent.run(conversation):
            pass

    asyncio.run(drive())
    assert prompts and "Bash" in prompts[0] and "# Tools that are not loaded yet" in prompts[0]


# ---------------------------------------------------------------- 2. 纯回忆：0 工具


def test_conversation_recall_needs_no_tools(workspace):
    """场景 A：答案已在对话里，模型直接回答 → 0 工具调用。"""
    history = [
        Message(role="user", content="My project code is APOLLO-42."),
        Message(role="assistant", content="Noted: APOLLO-42."),
        Message(role="user", content="What is the difference between a list and a tuple?"),
        Message(role="assistant", content="Mutability."),
    ]
    out = run(workspace, [final("Your project code is APOLLO-42.")], history=history)
    assert len(out["tool_calls"]) == 0
    assert out["client"].call_count == 1
    # 首轮就不应该把探索型工具摆在模型面前
    assert out["client"].seen_tools[0] == ["EditFile", "Grep", "ReadFile", "ToolSearch", "WriteFile"]


def test_constraints_recall_needs_no_tools(workspace):
    """场景 B：复述约束 → 0 工具调用。"""
    history = [
        Message(role="user", content="Remember: language=Python, third_party=false, output=JSON"),
        Message(role="assistant", content="Got it."),
    ]
    out = run(workspace, [final("1. Python 2. no third-party libs 3. JSON")], history=history)
    assert len(out["tool_calls"]) == 0


def test_restored_session_history_counts_as_evidence(workspace):
    """场景 C：resume 出来的历史就是证据，新 Agent 不应因此去搜 workspace。"""
    restored = [
        Message(role="user", content="SECRET = ALPHA-7788，请记住。"),
        Message(role="assistant", content="记住了：ALPHA-7788。"),
        Message(role="user", content="What is the secret?"),
        Message(role="assistant", content="The secret is ALPHA-7788."),
    ]
    out = run(workspace, [final("ALPHA-7788")], history=restored)
    assert len(out["tool_calls"]) == 0


# ---------------------------------------------------------------- 3. 真需要工具：能力不能被关掉


def test_grep_available_without_activation(workspace):
    """场景 D：需要在文件里搜索 → Grep 直接可用（无需 ToolSearch）。"""
    out = run(workspace, [
        tool_turn("t1", "Grep", {"pattern": "TARGET", "path": "."}),
        final("a.py contains TARGET."),
    ])
    assert len(out["tool_calls"]) == 1
    assert "Grep" in out["client"].seen_tools[0]


def test_writefile_available_without_activation(workspace):
    """场景 E：需要创建文件 → WriteFile 直接可用。"""
    out = run(workspace, [
        tool_turn("t1", "WriteFile", {"file_path": "result.json", "content": '{"ok": true}'}),
        final("Created result.json."),
    ])
    assert len(out["tool_calls"]) == 1
    assert (workspace / "result.json").exists()
    assert "WriteFile" in out["client"].seen_tools[0]


def test_readfile_available_without_activation(workspace):
    """场景 F：需要读配置 → ReadFile 直接可用。"""
    out = run(workspace, [
        tool_turn("t1", "ReadFile", {"file_path": "config.json"}),
        final("timeout is 42."),
    ])
    assert len(out["tool_calls"]) == 1
    assert "ReadFile" in out["client"].seen_tools[0]


def test_bash_requires_activation_but_then_works(workspace):
    """执行命令类任务：先 ToolSearch 激活，之后可正常执行（能力未被永久关闭）。"""
    out = run(workspace, [
        tool_turn("t1", "ToolSearch", {"query": "select:Bash"}),
        tool_turn("t2", "Bash", {"command": 'python -c "print(1+1)"'}),
        final("2"),
    ])
    assert out["client"].seen_tools[0] and "Bash" not in out["client"].seen_tools[0]
    assert any("Bash" in tools for tools in out["client"].seen_tools[1:]), "激活后应可见"
    assert [e.tool_name for e in out["tool_calls"]] == ["ToolSearch", "Bash"]


def test_attachments_do_not_require_explore_tools(workspace):
    """附件类请求同样不需要 Bash/Glob：暴露结构一致。"""
    registry = create_default_registry(work_dir=str(workspace))
    exposed = {s["name"] for s in registry.get_all_schemas()}
    assert "Bash" not in exposed
    assert "ReadFile" in exposed
