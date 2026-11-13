"""长期记忆保存流程（SaveMemory）与 Web 呈现的回归测试。

对应本轮修复：
  * 用户说"记住我的偏好"应走受控记忆接口，而不是通用 WriteFile/ReadFile；
  * 用户级记忆不再因为"在工作区之外"而反复触发路径沙箱确认；
  * 保存结果由服务端确认，失败时不能说"已记住"；
  * 一轮对话不再被工具事件切成多条空白助手消息 / 多个朗读按钮。

所有用例都使用临时工作区与临时记忆目录，不触碰真实记忆和聊天记录。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from aieb.memory import auto_memory, provenance  # noqa: E402
from aieb.memory.auto_memory import MemoryManager  # noqa: E402


# ---------------------------------------------------------------- 辅助


@pytest.fixture
def mm(tmp_path, monkeypatch):
    """项目级 + 隔离的用户级记忆目录。"""
    ws = tmp_path / "ws"
    ws.mkdir()
    user = tmp_path / "user_mem"
    user.mkdir()
    monkeypatch.setattr(
        auto_memory, "get_user_auto_mem_path", lambda: str(user) + os.sep
    )
    return MemoryManager(str(ws)), ws, user


PREFERENCE = "我平时比较喜欢自然风景，不喜欢太赶的旅行安排，以后推荐旅行计划时可以考虑这一点。"


def memory_files(directory: Path) -> list[str]:
    return sorted(
        p.name for p in Path(directory).glob("*.md") if p.name != "MEMORY.md"
    )


def outside_workspace(name: str = "x.md") -> Path:
    """一个既不属工作区、也不属临时目录的路径（沙箱真正会拦的区域）。

    注意：pytest 的 tmp_path 落在系统临时目录里，而临时目录本来就在沙箱白名单内，
    所以"越界"必须用家目录下的路径来构造。这里只计算路径，不会真的创建文件。
    """
    return Path.home() / ".aieb-sandbox-probe" / name


def make_checker(work_dir: Path):
    from aieb.permissions import PermissionChecker, PermissionMode
    from aieb.permissions.dangerous import DangerousCommandDetector
    from aieb.permissions.rules import RuleEngine
    from aieb.permissions.sandbox import PathSandbox

    return PermissionChecker(
        detector=DangerousCommandDetector(),
        sandbox=PathSandbox(str(work_dir)),
        rule_engine=RuleEngine(),
        mode=PermissionMode.DEFAULT,
    )


# ---------------------------------------------------------------- 1. 用户级保存


def test_save_preference_goes_to_user_level(mm):
    """1. 明确"记住我的偏好"应落到用户级长期记忆。"""
    manager, _ws, user = mm
    result = manager.save_memory(PREFERENCE, scope="user", title="旅行偏好")

    assert result.ok is True
    assert result.status == "saved"
    assert result.scope == "user"
    assert memory_files(user) == [result.filename]
    # 真的落到了用户级目录，而不是项目级
    assert (user / result.filename).is_file()
    assert memory_files(Path(manager.project_mem_dir)) == []
    # 正文可读回
    assert "自然风景" in (user / result.filename).read_text(encoding="utf-8")


def test_save_memory_tool_has_no_path_parameter():
    """记忆接口不接受任意磁盘路径 —— 这是它能免授权的前提。"""
    from aieb.tools.save_memory import Params

    props = Params.model_json_schema()["properties"]
    assert "file_path" not in props
    assert "path" not in props
    assert {"content", "scope"} <= set(props)


# ---------------------------------------------------------------- 2. 不触发越界授权


def test_memory_save_does_not_trigger_file_sandbox_prompt(mm):
    """2. 保存用户级记忆不需要普通文件工具的授权，但文件工具本身没被放开。"""
    manager, ws, user = mm
    from aieb.tools.save_memory import SaveMemory
    from aieb.tools.workspace import WorkspaceResolver, WorkspaceViolation
    from aieb.tools.write_file import WriteFile

    checker = make_checker(ws)
    save_tool = SaveMemory(manager)
    decision = checker.check(save_tool, {"content": PREFERENCE, "scope": "user"})
    assert decision.effect == "allow"

    # 更硬的阻断：通用写工具的路径解析层直接拒绝工作区外的路径。
    # 这正是历史上"用户级记忆写不进去、只能退到项目级"的底层原因。
    with pytest.raises(WorkspaceViolation):
        WorkspaceResolver(str(ws)).resolve(str(user / "x.md"))

    # 对照：同一次运行里，用 WriteFile 去写工作区外的路径仍然要被拦下来确认。
    write_tool = WriteFile(work_dir=str(ws))
    outside = checker.check(
        write_tool, {"file_path": str(outside_workspace()), "content": "hi"}
    )
    assert outside.effect == "ask"
    assert "沙箱" in outside.reason


def test_save_memory_tool_writes_and_reports(mm):
    """工具路径与直接调用一致，并且带 session 归属。"""
    manager, _ws, user = mm
    from aieb.tools.save_memory import SaveMemory

    tool = SaveMemory(manager)
    tool.set_session_id("sess-1")
    params = tool.params_model.model_validate(
        {"content": PREFERENCE, "scope": "user", "title": "travel-preference"}
    )
    result = asyncio.run(tool.execute(params))

    assert result.is_error is False
    assert "status: saved" in result.output
    assert provenance.sessions_of(str(user), "travel-preference.md") == ["sess-1"]


# ---------------------------------------------------------------- 3. 重复保存


def test_duplicate_preference_creates_single_file(mm):
    """3. 同一偏好重复保存不会生成多个文件，也不会反复产生工具调用。"""
    manager, _ws, user = mm

    first = manager.save_memory(PREFERENCE, scope="user", title="travel-preference")
    second = manager.save_memory(PREFERENCE, scope="user", title="travel-preference")

    assert first.status == "saved"
    assert second.status == "duplicate"
    assert second.ok is True
    assert memory_files(user) == ["travel-preference.md"]
    index = (user / "MEMORY.md").read_text(encoding="utf-8")
    assert index.count("travel-preference.md") == 1


def test_same_title_updates_existing_memory(mm):
    """同标题不同内容 → 更新同一条记忆，而不是新增一条。"""
    manager, _ws, user = mm
    manager.save_memory("喜欢自然风景", scope="user", title="travel-preference")
    result = manager.save_memory(
        "喜欢自然风景，但完全不能接受徒步超过两小时", scope="user", title="travel-preference"
    )

    assert result.status == "updated"
    assert memory_files(user) == ["travel-preference.md"]
    assert "徒步" in (user / "travel-preference.md").read_text(encoding="utf-8")


# ---------------------------------------------------------------- 4. 新会话可读取


def test_new_session_prompt_contains_saved_preference(mm):
    """4. 新建会话能通过记忆提示读到已保存的偏好。"""
    manager, ws, _user = mm
    manager.save_memory(PREFERENCE, scope="user", title="travel-preference")

    fresh = MemoryManager(str(ws))
    prompt = fresh.load()
    assert "自然风景" in prompt


# ---------------------------------------------------------------- 5. 关闭记忆


def test_disabled_memory_blocks_save_and_read(mm):
    """5. 记忆关闭后既不保存也不读取。"""
    manager, ws, _user = mm
    manager.save_memory(PREFERENCE, scope="user", title="travel-preference")
    assert manager.set_enabled(False) is True

    result = manager.save_memory("新的偏好", scope="user", title="another")
    assert result.ok is False
    assert result.status == "disabled"

    # 已有记忆也不再注入提示，也不再被提取流程触碰
    fresh = MemoryManager(str(ws))
    assert fresh.load() == ""


# ---------------------------------------------------------------- 6. 失败不得宣称成功


def test_failed_save_output_forbids_claimed_success(mm):
    """6. 保存失败时返回的状态里必须明确禁止说"已记住"。"""
    manager, _ws, _user = mm
    manager.set_enabled(False)

    output = manager.save_memory("内容", scope="user").as_tool_output()
    assert "ok: false" in output
    assert "status: disabled" in output
    assert "Do not tell the user" in output


def test_tool_marks_failed_save_as_error(mm):
    from aieb.tools.save_memory import SaveMemory

    manager, _ws, _user = mm
    manager.set_enabled(False)
    tool = SaveMemory(manager)
    params = tool.params_model.model_validate({"content": "内容", "scope": "user"})
    result = asyncio.run(tool.execute(params))
    assert result.is_error is True
    assert "ok: false" in result.output


# ---------------------------------------------------------------- 7. 来源记录


def test_save_records_source_session(mm):
    """7. 来源 Session 在写入时刻被记录（不靠 mtime 猜）。"""
    manager, _ws, user = mm
    result = manager.save_memory(
        PREFERENCE, scope="user", title="travel-preference", session_id="sess-42"
    )
    assert provenance.sessions_of(str(user), result.filename) == ["sess-42"]
    entries = provenance.load_entries(str(user))
    assert entries[result.filename]["source"] == provenance.SOURCE_SAVE


# ---------------------------------------------------------------- 8. 删除后不再读取


def test_deleted_memory_is_not_loaded_again(mm):
    """8. 删除一条记忆后，新对话不会继续从长期记忆里读到它。"""
    manager, ws, _user = mm
    result = manager.save_memory(PREFERENCE, scope="user", title="travel-preference")
    assert "自然风景" in MemoryManager(str(ws)).load()

    ok, message = manager.delete_by_id(manager.memory_id("user", result.filename))
    assert ok is True
    assert "删除" in message

    prompt = MemoryManager(str(ws)).load()
    assert "自然风景" not in prompt


# ---------------------------------------------------------------- 9. 删除会话


def test_purge_session_removes_its_exclusive_memory(mm):
    """9. 删除会话仍能清掉该会话专属的记忆。"""
    manager, _ws, user = mm
    manager.save_memory(
        PREFERENCE, scope="user", title="travel-preference", session_id="sess-a"
    )
    manager.save_memory("共享偏好", scope="user", title="shared", session_id="sess-a")
    manager.save_memory("共享偏好", scope="user", title="shared", session_id="sess-b")

    result = provenance.prune_session(str(user), "sess-a")

    assert "travel-preference.md" in result.deleted
    assert "shared.md" in result.kept_shared
    assert memory_files(user) == ["shared.md"]
    assert provenance.sessions_of(str(user), "shared.md") == ["sess-b"]


# ---------------------------------------------------------------- 10. 高风险仍需授权


def test_high_risk_operations_still_require_permission(mm, tmp_path):
    """10. 高风险文件/命令操作没有被记忆改动影响。"""
    _manager, ws, _user = mm
    from aieb.tools.bash import Bash
    from aieb.tools.write_file import WriteFile

    checker = make_checker(ws)
    write_tool = WriteFile(work_dir=str(ws))

    outside = checker.check(
        write_tool, {"file_path": str(outside_workspace("escape.txt")), "content": "x"}
    )
    assert outside.effect == "ask"

    dangerous = checker.check(Bash(), {"command": "rm -rf /"})
    assert dangerous.effect == "deny"


def test_allow_always_scopes_to_directory_only_for_session(mm):
    """用户点 Allow Always 后，同目录复用生效，但不会扩大到别处。"""
    _manager, ws, _user = mm
    from aieb.tools.write_file import WriteFile

    checker = make_checker(ws)
    write_tool = WriteFile(work_dir=str(ws))
    scope_dir = Path.home() / ".aieb-sandbox-probe"

    assert checker.check(
        write_tool, {"file_path": str(scope_dir / "a.md"), "content": "x"}
    ).effect == "ask"

    checker.approve_directory(str(scope_dir / "a.md"))
    assert checker.check(
        write_tool, {"file_path": str(scope_dir / "b.md"), "content": "x"}
    ).effect == "allow"
    # 换一个目录仍然需要确认
    assert checker.check(
        write_tool, {"file_path": str(Path.home() / ".aieb-other" / "c.md"), "content": "x"}
    ).effect == "ask"


# ---------------------------------------------------------------- 11. 恶意输入


@pytest.mark.parametrize(
    "title",
    ["../../escape", "/etc/passwd", "C:\\Windows\\evil", "..\\..\\x", "a/b/../../c"],
)
def test_malicious_title_cannot_escape_memory_dir(mm, title):
    """11. 恶意输入不能借记忆接口读写任意文件。"""
    manager, _ws, user = mm
    result = manager.save_memory("正文", scope="user", title=title)

    assert result.ok is True
    written = Path(user / result.filename).resolve()
    assert written.parent == Path(user).resolve()
    # 记忆目录之外不应出现新文件
    assert not (user.parent / "escape.md").exists()
    assert not (user.parent / "passwd.md").exists()


def test_memory_dir_stays_inside_managed_root(mm):
    """即便内容里塞了路径，落盘位置也只由 scope 决定。"""
    manager, ws, user = mm
    result = manager.save_memory(
        f"请看 {ws}/secret.txt 和 ~/.ssh/id_rsa", scope="project", title="paths"
    )
    assert Path(manager.project_mem_dir) == Path(user.parent) / "ws" / ".aieb" / "memory"
    assert (Path(manager.project_mem_dir) / result.filename).is_file()


# ---------------------------------------------------------------- 12–14. Web 呈现

WEB_SOURCE = (REPO_ROOT / "aieb" / "web_content.py").read_text(encoding="utf-8")


def _switch_case(name: str) -> str:
    start = WEB_SOURCE.index(f"case '{name}'")
    end = WEB_SOURCE.find("case '", start + 10)
    return WEB_SOURCE[start:end if end > 0 else start + 2000]


def test_stream_end_does_not_split_turns():
    """12. 工具事件不再切断助手回复：stream_end 不重置当前气泡。"""
    block = _switch_case("stream_end")
    assert "currentAssistantEl = null" not in block
    assert "attachSpeakButton" not in block


def test_blank_stream_text_does_not_create_bubble():
    """纯空白分片不新建助手气泡。"""
    block = _switch_case("stream_text")
    assert "trim()" in block


def test_empty_turn_bubble_is_removed():
    """只调工具、没有正文的一轮不留空气泡。"""
    block = _switch_case("loop_complete")
    assert "currentAssistantEl.remove()" in block


def test_speak_button_only_on_final_answer():
    """13. 消息级操作只挂在最终回答上。"""
    # 朗读按钮只在 loop_complete / replay_assistant 挂载
    occurrences = WEB_SOURCE.count("attachSpeakButton(")
    # 定义 1 次 + 调用 2 次（replay_assistant, loop_complete）
    assert occurrences == 3
    assert "attachSpeakButton" in _switch_case("loop_complete")
    assert "attachSpeakButton" in _switch_case("replay_assistant")


def test_tool_segments_use_friendly_labels():
    """用户视图里不直接显示原始工具名（原始名只出现在开发者详情的行里）。"""
    assert "TOOL_ACTION_LABELS" in WEB_SOURCE
    # 普通模式的进度文案来自 actionLabelForTool
    add = WEB_SOURCE[WEB_SOURCE.index("function addToolUse"):]
    add = add[: add.index("function updateToolResult")]
    assert "addAssistant(" not in add
    summary = WEB_SOURCE[WEB_SOURCE.index("function execSummary"):]
    summary = summary[: summary.index("function renderExecPanel")]
    assert "actionLabelForTool(running.name)" in summary
    # 原始工具名只用于开发者模式的行标题
    row = WEB_SOURCE[WEB_SOURCE.index("function renderExecRow"):]
    row = row[: row.index("function refreshAllExecPanels")]
    assert "name.textContent = tool.name" in row


# ---------------------------------------------------------------- 14. 不暴露内部细节


def test_memory_prompt_has_no_internal_paths(mm):
    """14. 记忆提示不再把目录/索引路径喂给模型，也不再要求用 Write 工具。"""
    manager, _ws, user = mm
    manager.save_memory(PREFERENCE, scope="user", title="travel-preference")
    prompt = manager.load()

    assert str(user) not in prompt
    assert str(Path(manager.project_mem_dir)) not in prompt
    assert "MEMORY.md" not in prompt
    assert ".aieb" not in prompt
    assert "Write tool" not in prompt
    assert "SaveMemory" in prompt
    # 仍然告诉模型两个作用域的存在
    assert "User-level" in prompt
    assert "Project-level" in prompt


def test_replay_hides_tool_names():
    """历史回放不再出现 "used WriteFile" 这类实现细节。"""
    from aieb.conversation import Message, ToolUseBlock
    from aieb.remote import RemoteServer

    sent: list[dict] = []

    class Harness(RemoteServer):
        """只测回放逻辑，不跑完整服务器初始化。"""

        def __init__(self):
            self.sent = sent

        def _replay_artifacts(self):
            return {}

        async def _broadcast(self, payload):
            self.sent.append(payload)

    messages = [
        Message(role="user", content="记住我的偏好"),
        Message(
            role="assistant",
            content="好的，记住了。",
            tool_uses=[ToolUseBlock(tool_use_id="t1", tool_name="WriteFile", arguments={})],
        ),
    ]
    asyncio.run(RemoteServer._replay_messages(Harness(), messages))

    assistants = [p for p in sent if p["type"] == "replay_assistant"]
    assert len(assistants) == 1
    text = assistants[0]["data"]["content"]
    assert "WriteFile" not in text
    assert "used" not in text
    assert "记住了" in text


def test_replay_skips_tool_only_assistant_messages():
    """只有工具调用、没有正文的历史消息不再生成空气泡。"""
    from aieb.conversation import Message, ToolUseBlock
    from aieb.remote import RemoteServer

    sent: list[dict] = []

    class Harness(RemoteServer):
        """只测回放逻辑，不跑完整服务器初始化。"""

        def __init__(self):
            self.sent = sent

        def _replay_artifacts(self):
            return {}

        async def _broadcast(self, payload):
            self.sent.append(payload)

    messages = [
        Message(role="user", content="你好"),
        Message(
            role="assistant",
            content="",
            tool_uses=[ToolUseBlock(tool_use_id="t1", tool_name="ReadFile", arguments={})],
        ),
        Message(role="assistant", content="这是最终回答。"),
    ]
    asyncio.run(RemoteServer._replay_messages(Harness(), messages))

    assistants = [p for p in sent if p["type"] == "replay_assistant"]
    assert len(assistants) == 1
    assert assistants[0]["data"]["content"] == "这是最终回答。"


# ---------------------------------------------------------------- 15. 提取去重


def test_extract_reuses_explicit_save(mm, monkeypatch):
    """自动提取与显式保存不会写出两份相同的偏好。"""
    manager, _ws, user = mm
    manager.save_memory(PREFERENCE, scope="user", title="travel-preference", session_id="s1")

    reply = (
        "---\n"
        "MEMORY_NAME: travel-preference\n"
        "MEMORY_TYPE: user\n"
        "MEMORY_DESC: 旅行偏好\n"
        f"MEMORY_BODY: {PREFERENCE}\n"
    )

    class FakeClient:
        async def stream(self, conversation, system=""):
            from aieb.tools.base import StreamEnd, TextDelta

            yield TextDelta(text=reply)
            yield StreamEnd(stop_reason="stop")

    from aieb.conversation import ConversationManager, Message

    conv = ConversationManager()
    conv.history = [Message(role="user", content=PREFERENCE)]
    asyncio.run(manager.extract(FakeClient(), conv, "openai", session_id="s1"))

    assert memory_files(user) == ["travel-preference.md"]
    assert (user / "MEMORY.md").read_text(encoding="utf-8").count("travel-preference.md") == 1


def test_extract_respects_disabled(mm):
    """关闭记忆后自动提取不写任何文件。"""
    manager, _ws, user = mm
    manager.set_enabled(False)

    class FakeClient:
        async def stream(self, conversation, system=""):
            from aieb.tools.base import StreamEnd, TextDelta

            yield TextDelta(text="MEMORY_NAME: x\nMEMORY_TYPE: user\nMEMORY_BODY: y\n---\n")
            yield StreamEnd(stop_reason="stop")

    from aieb.conversation import ConversationManager, Message

    conv = ConversationManager()
    conv.history = [Message(role="user", content="记住 y")]
    asyncio.run(manager.extract(FakeClient(), conv, "openai", session_id="s1"))
    assert memory_files(user) == []


# ---------------------------------------------------------------- 16. 注册与注入


def test_agent_registers_save_memory_and_injects_session(tmp_path, monkeypatch):
    """Agent 自动注册 SaveMemory，并在执行前注入当前 session。"""
    from aieb.agent import Agent
    from aieb.memory.auto_memory import MemoryManager as MM
    from aieb.tools import create_default_registry

    ws = tmp_path / "ws"
    ws.mkdir()
    user = tmp_path / "user_mem"
    user.mkdir()
    monkeypatch.setattr(auto_memory, "get_user_auto_mem_path", lambda: str(user) + os.sep)

    manager = MM(str(ws))
    registry = create_default_registry(work_dir=str(ws))

    agent = Agent(
        client=SimpleNamespace(),
        registry=registry,
        protocol="openai",
        work_dir=str(ws),
        memory_manager=manager,
    )
    tool = registry.get("SaveMemory")
    assert tool is not None

    agent.session_id = "sess-injected"
    from aieb.tools.base import ToolCallComplete

    tc = ToolCallComplete(
        tool_id="c1",
        tool_name="SaveMemory",
        arguments={"content": PREFERENCE, "scope": "user", "title": "travel-preference"},
    )
    asyncio.run(agent._invoke_tool(tc, tool))

    assert provenance.sessions_of(str(user), "travel-preference.md") == ["sess-injected"]
    # 工具执行后仍走原有来源登记逻辑，且不因为新路径报错
    items = manager.list_for_web({})
    assert len(items) == 1
    assert "对话" in items[0]["source_label"] or items[0]["source_label"]
