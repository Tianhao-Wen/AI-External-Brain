"""Phase 2B-2：Completion Discipline / Soft Early Stop 的测试。

两层：
1. **契约层**：system prompt 必须包含通用完成契约；结果完整性元数据必须真的送到模型可见的
   tool result 文本里（否则模型无从判断"证据已充分"）。
2. **行为层**：用脚本化 MockLLMClient 证明——
   * 证据充分后模型直接给 Final 时，loop 立刻结束（不会被额外验证拖住）；
   * 截断 / 出错 / 冲突 / 用户要求 double-check 时，loop **不会**阻止继续调用工具
     （即它不是 hard kill，而是 soft discipline）。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any, AsyncIterator

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from aieb.agent import (  # noqa: E402
    Agent,
    LoopComplete,
    ToolResultEvent,
    ToolUseEvent,
    format_tool_metadata,
)
from aieb.client import LLMClient  # noqa: E402
from aieb.conversation import ConversationManager  # noqa: E402
from aieb.prompts import build_system_prompt  # noqa: E402
from aieb.tools import create_default_registry  # noqa: E402
from aieb.tools.base import StreamEnd, StreamEvent, TextDelta, ToolCallComplete  # noqa: E402


class ScriptedClient(LLMClient):
    """按脚本逐轮回放事件；每轮代表一次 LLM 调用。"""

    def __init__(self, responses: list[list[StreamEvent]]) -> None:
        self._responses = list(responses)
        self.call_count = 0

    async def stream(
        self,
        conversation: ConversationManager,
        system: str = "",
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        self.call_count += 1
        events = self._responses.pop(0) if self._responses else [
            TextDelta(text="done"),
            StreamEnd(stop_reason="end_turn", input_tokens=1, output_tokens=1),
        ]
        for event in events:
            yield event


def turn(*parts) -> list[StreamEvent]:
    return list(parts)


def final(text: str) -> list[StreamEvent]:
    return [TextDelta(text=text), StreamEnd(stop_reason="end_turn", input_tokens=1, output_tokens=1)]


def tool_turn(tool_id: str, name: str, args: dict) -> list[StreamEvent]:
    return [ToolCallComplete(tool_id, name, args), StreamEnd(stop_reason="tool_use", input_tokens=1, output_tokens=1)]


def run_agent(workspace: Path, script: list[list[StreamEvent]]) -> tuple[list, dict]:
    client = ScriptedClient(script)
    registry = create_default_registry(work_dir=str(workspace))
    agent = Agent(client, registry, "anthropic", work_dir=str(workspace))
    conversation = ConversationManager()
    conversation.add_user_message("go")

    events: list = []

    async def drive():
        async for event in agent.run(conversation):
            events.append(event)

    asyncio.run(drive())
    summary = {
        "llm_calls": client.call_count,
        "tool_calls": sum(1 for e in events if isinstance(e, ToolUseEvent)),
        "tool_results": [e for e in events if isinstance(e, ToolResultEvent)],
        "loops": [e for e in events if isinstance(e, LoopComplete)],
        "turns": [e for e in events if isinstance(e, LoopComplete)][0].total_turns if any(
            isinstance(e, LoopComplete) for e in events
        ) else None,
        "history": "\n".join(
            [
                (m.content if isinstance(m.content, str) else str(m.content))
                for m in conversation.history
            ]
            + [tr.content for m in conversation.history for tr in getattr(m, "tool_results", [])]
        ),
    }
    return events, summary


@pytest.fixture()
def workspace(tmp_path):
    (tmp_path / "answer.txt").write_text("CODE-123\n", encoding="utf-8")
    (tmp_path / "a.py").write_text("TARGET here\n", encoding="utf-8")
    (tmp_path / "b.md").write_text("TARGET too\n", encoding="utf-8")
    (tmp_path / "big.txt").write_text("\n".join(f"line {i}" for i in range(5000)), encoding="utf-8")
    return tmp_path


# ---------------------------------------------------------------- 1. 契约层


def test_system_prompt_contains_completion_contract():
    prompt = build_system_prompt()
    assert "# When to stop using tools" in prompt
    assert "can the user's request already be fully answered" in prompt
    # 允许继续验证的例外必须是"条件式"，而不是鼓励性
    for condition in ("tool returned an error", "truncated", "missing", "conflict", "double-checking"):
        assert condition in prompt, f"缺少例外条件: {condition}"
    # 旧的“无条件要求验证”措辞必须已被收窄
    assert "Before reporting a task complete, verify it works" not in prompt
    assert "changed state" in prompt


def test_contract_has_no_benchmark_specific_rules():
    prompt = build_system_prompt()
    for banned in ("BENCHMARK", "FILE_", "TOOL_0", "WENDREAM_SEARCH", "case_id"):
        assert banned not in prompt, f"契约里不应出现用例特化内容: {banned}"


def test_metadata_rendering_is_compact_and_boolean_lowercase():
    text = format_tool_metadata({"matches_count": 2, "truncated": False, "search_complete": True, "elapsed_seconds": 0.1})
    assert text.startswith("\n\n[result_meta: ")
    assert "matches_count=2" in text
    assert "truncated=false" in text
    assert "search_complete=true" in text
    assert "elapsed_seconds" not in text


def test_metadata_absent_is_empty():
    assert format_tool_metadata(None) == ""
    assert format_tool_metadata({}) == ""


# ---------------------------------------------------------------- 2. 结果完整性元数据


def test_grep_reports_search_complete(workspace):
    tool = create_default_registry(work_dir=str(workspace)).get("Grep")
    from aieb.tools.grep import Params

    result = asyncio.run(tool.execute(Params(pattern="TARGET", path=".")))
    assert result.metadata["search_complete"] is True
    assert result.metadata["truncated"] is False
    assert result.metadata["matches_count"] == 2


def test_glob_reports_search_complete(workspace):
    tool = create_default_registry(work_dir=str(workspace)).get("Glob")
    from aieb.tools.glob import Params

    result = asyncio.run(tool.execute(Params(pattern="**/*.py", path=".")))
    assert result.metadata["search_complete"] is True
    assert result.metadata["matches_count"] == 1


def test_readfile_reports_read_complete(workspace):
    tool = create_default_registry(work_dir=str(workspace)).get("ReadFile")
    from aieb.tools.read_file import Params

    result = asyncio.run(tool.execute(Params(file_path="answer.txt")))
    assert result.metadata["read_complete"] is True
    assert result.metadata["truncated"] is False

    partial = asyncio.run(tool.execute(Params(file_path="big.txt", offset=0, limit=10)))
    assert partial.metadata["read_complete"] is False
    assert partial.metadata["truncated"] is True


def test_metadata_reaches_model_visible_tool_result(workspace):
    """元数据必须出现在送给模型的历史里，否则模型无从判断"证据已充分"。"""
    script = [
        tool_turn("t1", "ReadFile", {"file_path": "answer.txt"}),
        final("The code is CODE-123."),
    ]
    _, summary = run_agent(workspace, script)
    assert "[result_meta:" in summary["history"]
    assert "read_complete=true" in summary["history"]


# ---------------------------------------------------------------- 3. 行为层：Soft Early Stop


def test_scenario_a_read_once_then_final(workspace):
    """A：ReadFile 完整返回后直接 Final，不应再有任何工具调用。"""
    script = [
        tool_turn("t1", "ReadFile", {"file_path": "answer.txt"}),
        final("The number inside answer.txt is CODE-123."),
    ]
    _, summary = run_agent(workspace, script)
    assert summary["tool_calls"] == 1
    assert summary["llm_calls"] == 2
    assert summary["turns"] == 2


def test_scenario_b_grep_once_then_final(workspace):
    """B：Grep 完整返回后直接 Final。"""
    script = [
        tool_turn("t1", "Grep", {"pattern": "TARGET", "path": "."}),
        final("a.py and b.md contain TARGET."),
    ]
    _, summary = run_agent(workspace, script)
    assert summary["tool_calls"] == 1
    assert summary["llm_calls"] == 2


def test_scenario_c_truncated_result_allows_more_tools(workspace):
    """C：结果被截断时，允许继续调用工具（loop 不得强行停止）。"""
    script = [
        tool_turn("t1", "ReadFile", {"file_path": "big.txt", "offset": 0, "limit": 10}),
        tool_turn("t2", "ReadFile", {"file_path": "big.txt", "offset": 10, "limit": 10}),
        final("Read the rest."),
    ]
    _, summary = run_agent(workspace, script)
    assert summary["tool_calls"] == 2
    assert summary["llm_calls"] == 3
    # 第一次结果确实标记为未读完
    assert "read_complete=false" in summary["history"]


def test_scenario_d_error_result_allows_recovery(workspace):
    """D：工具失败后允许 recovery。"""
    script = [
        tool_turn("t1", "ReadFile", {"file_path": "does_not_exist.txt"}),
        tool_turn("t2", "Glob", {"pattern": "*.txt", "path": "."}),
        final("That file does not exist; available txt files listed."),
    ]
    _, summary = run_agent(workspace, script)
    assert summary["tool_calls"] == 2
    assert summary["llm_calls"] == 3


def test_scenario_e_conflicting_evidence_allows_verification(workspace):
    """E：证据可能冲突时允许交叉验证。"""
    script = [
        tool_turn("t1", "ReadFile", {"file_path": "answer.txt"}),
        tool_turn("t2", "Grep", {"pattern": "CODE", "path": "."}),
        final("Cross-checked: CODE-123."),
    ]
    _, summary = run_agent(workspace, script)
    assert summary["tool_calls"] == 2


def test_scenario_f_user_asked_double_check_allows_extra_verification(workspace):
    """F：用户明确要求 double-check 时允许额外验证。"""
    script = [
        tool_turn("t1", "ReadFile", {"file_path": "answer.txt"}),
        tool_turn("t2", "Bash", {"command": 'python -c "print(open(\'answer.txt\').read())"'}),
        final("Double-checked twice; CODE-123."),
    ]
    _, summary = run_agent(workspace, script)
    assert summary["tool_calls"] == 2


def test_no_hard_iteration_cap_by_default(workspace):
    """Early Stop 是软约束：默认不应引入人为迭代上限。"""
    registry = create_default_registry(work_dir=str(workspace))
    agent = Agent(ScriptedClient([]), registry, "anthropic", work_dir=str(workspace))
    assert agent.max_iterations == 0, "不应通过降低 max_iterations 实现 early stop"


def test_many_tool_rounds_are_still_permitted(workspace):
    """连续多轮工具调用必须仍然可行（复杂任务不能被提前掐断）。"""
    script = [tool_turn(f"t{i}", "Glob", {"pattern": f"*{i}*", "path": "."}) for i in range(5)]
    script.append(final("done"))
    _, summary = run_agent(workspace, script)
    assert summary["tool_calls"] == 5
    assert summary["llm_calls"] == 6
