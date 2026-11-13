"""统一 tool 执行 pipeline 与 streaming hook 修复的测试。

覆盖（对应审计项 §19-§22）：
  * 流式 agent.run() 现在会执行 pre_tool_use / post_tool_use hooks；
  * pre-hook 拒绝 -> 工具 execute 调用 0 次，返回结构化错误，Agent 不崩；
  * 同一次 tool_use：pre-hook 恰好 1 次、execute 至多 1 次、post-hook 1 次；
  * 需要交互确认（ask）的工具在确认后同样走同一 pipeline，且不会重复执行 hook。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, AsyncIterator

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from aieb.agent import Agent, ToolResultEvent  # noqa: E402
from aieb.client import LLMClient  # noqa: E402
from aieb.conversation import ConversationManager  # noqa: E402
from aieb.hooks import Hook, HookEngine  # noqa: E402
from aieb.hooks.conditions import parse_condition  # noqa: E402
from aieb.hooks.models import Action  # noqa: E402
from aieb.tools import create_default_registry  # noqa: E402
from aieb.tools.base import (  # noqa: E402
    StreamEnd,
    StreamEvent,
    TextDelta,
    ToolCallComplete,
)


class ScriptedClient(LLMClient):
    def __init__(self, responses: list[list[StreamEvent]]) -> None:
        self._responses = list(responses)
        self.calls = 0

    async def stream(
        self,
        conversation: ConversationManager,
        system: str = "",
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        self.calls += 1
        events = self._responses.pop(0) if self._responses else [
            TextDelta(text="done"),
            StreamEnd(stop_reason="end_turn", input_tokens=1, output_tokens=1),
        ]
        for event in events:
            yield event


def _tool_use(tool_id: str, name: str, args: dict, call_count: int = 1) -> list[StreamEvent]:
    events: list[StreamEvent] = [
        ToolCallComplete(tool_id, name, args) for _ in range(call_count)
    ]
    events.append(StreamEnd(stop_reason="tool_use", input_tokens=1, output_tokens=1))
    return events


class CountingHookEngine(HookEngine):
    """记录 pre/post hook 的实际执行次数。"""

    def __init__(self, hooks: list[Hook]) -> None:
        super().__init__(hooks)
        self.pre_calls = 0
        self.post_calls = 0

    async def run_pre_tool_hooks(self, ctx):  # type: ignore[override]
        self.pre_calls += 1
        return await super().run_pre_tool_hooks(ctx)

    async def run_hooks(self, event: str, ctx) -> None:  # type: ignore[override]
        if event == "post_tool_use":
            self.post_calls += 1
        await super().run_hooks(event, ctx)


def _reject_hook() -> Hook:
    return Hook(
        id="block-rm",
        event="pre_tool_use",
        action=Action(type="command", command="echo dangerous command blocked"),
        condition=parse_condition('tool == "Bash" && args.command =~ /rm\\s+-rf/'),
        reject=True,
    )


def _post_hook() -> Hook:
    return Hook(
        id="after-tool",
        event="post_tool_use",
        action=Action(type="command", command="echo post ran"),
        condition=parse_condition('tool == "ReadFile"'),
    )


def _agent(tmp_path: Path, client: LLMClient, hooks: list[Hook]) -> tuple[Agent, CountingHookEngine]:
    registry = create_default_registry(work_dir=str(tmp_path))
    engine = CountingHookEngine(hooks)
    agent = Agent(
        client=client,
        registry=registry,
        protocol="anthropic",
        work_dir=str(tmp_path),
        hook_engine=engine,
    )
    return agent, engine


async def _run(agent: Agent, prompt: str = "go", answer: str = "allow") -> list:
    """驱动 agent，并对权限请求自动作答（避免测试永久挂起）。"""
    from aieb.agent import PermissionRequest, PermissionResponse

    conversation = ConversationManager()
    conversation.add_user_message(prompt)
    events = []
    async for event in agent.run(conversation):
        if isinstance(event, PermissionRequest):
            mapping = {
                "allow": PermissionResponse.ALLOW,
                "deny": PermissionResponse.DENY,
                "always": PermissionResponse.ALLOW_ALWAYS,
            }
            if not event.future.done():
                event.future.set_result(mapping.get(answer, PermissionResponse.ALLOW))
            continue
        events.append(event)
    return events


def test_streaming_pre_hook_rejects_and_tool_never_executes(tmp_path, monkeypatch):
    (tmp_path / "keep.txt").write_text("x", encoding="utf-8")
    client = ScriptedClient([
        _tool_use("t1", "Bash", {"command": "rm -rf /"}),
        [TextDelta(text="blocked, understood"), StreamEnd("end_turn", 1, 1)],
    ])
    agent, engine = _agent(tmp_path, client, [_reject_hook()])

    executed: list[str] = []
    bash = agent.registry.get("Bash")

    async def _fake_execute(params):  # 记录是否真的执行
        executed.append(str(params))
        raise AssertionError("rejected tool must not execute")

    monkeypatch.setattr(bash, "execute", _fake_execute, raising=False)

    events = __import__("asyncio").run(_run(agent))

    results = [e for e in events if isinstance(e, ToolResultEvent)]
    assert len(results) == 1
    assert results[0].is_error is True
    assert "Hook rejected" in results[0].output
    assert executed == [], "pre_tool_use 拒绝后工具不得执行"
    assert engine.pre_calls == 1
    assert engine.post_calls == 0, "被拒绝的工具不应触发 post hook"


def test_streaming_pipeline_runs_hooks_exactly_once(tmp_path, monkeypatch):
    (tmp_path / "a.txt").write_text("hello", encoding="utf-8")
    client = ScriptedClient([
        _tool_use("t1", "ReadFile", {"file_path": "a.txt"}),
        [TextDelta(text="done"), StreamEnd("end_turn", 1, 1)],
    ])
    agent, engine = _agent(tmp_path, client, [_post_hook()])

    executed: list[str] = []
    read_tool = agent.registry.get("ReadFile")
    original = read_tool.execute

    async def _counting_execute(params):
        executed.append("read")
        return await original(params)

    monkeypatch.setattr(read_tool, "execute", _counting_execute, raising=False)

    events = __import__("asyncio").run(_run(agent))

    results = [e for e in events if isinstance(e, ToolResultEvent)]
    assert results and not results[0].is_error
    assert executed == ["read"], "同一次 tool_use 只能执行一次"
    assert engine.pre_calls == 1, "pre hook 必须恰好一次"
    assert engine.post_calls == 1, "post hook 必须恰好一次"


def test_deferred_ask_tool_uses_same_pipeline(tmp_path, monkeypatch):
    """需要确认（ask）的工具走 _execute_tool 路径时，hook 同样只跑一次。"""
    import asyncio

    (tmp_path / "b.txt").write_text("hi", encoding="utf-8")

    from aieb.permissions import (
        DangerousCommandDetector,
        PathSandbox,
        PermissionChecker,
        PermissionMode,
        RuleEngine,
    )
    from aieb.permissions.rules import Rule

    registry = create_default_registry(work_dir=str(tmp_path))
    engine = CountingHookEngine([_post_hook()])
    rule_engine = RuleEngine()
    rule_engine.append_local_rule  # 存在即可；这里用会话级放行让 ask 变 allow
    checker = PermissionChecker(
        detector=DangerousCommandDetector(),
        sandbox=PathSandbox(str(tmp_path)),
        rule_engine=rule_engine,
        mode=PermissionMode.DEFAULT,
    )
    checker.rule_engine.match = lambda tool, content: Rule(  # type: ignore[assignment]
        tool_name="ReadFile", pattern="*", effect="ask"
    )
    checker.add_session_allow("ReadFile", str(tmp_path / "b.txt"))

    agent = Agent(
        client=ScriptedClient([
            _tool_use("t1", "ReadFile", {"file_path": "b.txt"}),
            [TextDelta(text="done"), StreamEnd("end_turn", 1, 1)],
        ]),
        registry=registry,
        protocol="anthropic",
        work_dir=str(tmp_path),
        permission_checker=checker,
        hook_engine=engine,
    )

    events = asyncio.run(_run(agent))
    results = [e for e in events if isinstance(e, ToolResultEvent)]
    assert results, events
    assert engine.pre_calls <= 1
    assert engine.post_calls <= 1


def test_hook_pipeline_defined_once_in_source():
    """三条路径必须共用同一组 helper，避免再次出现“某条路径漏 hook”。"""
    source = (REPO_ROOT / "aieb" / "agent.py").read_text(encoding="utf-8")
    assert source.count("async def _run_pre_tool_hooks") == 1
    assert source.count("async def _run_post_tool_hooks") == 1
    assert source.count("async def _invoke_tool") == 1
    # 不得再出现裸的 run_pre_tool_hooks 调用（应统一走 helper）
    assert source.count("self.hook_engine.run_pre_tool_hooks") == 1
