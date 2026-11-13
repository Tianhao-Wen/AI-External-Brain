"""普通模式 / 开发者模式（Web 展示层）的测试。

约束：这些测试只验证「展示层」——不改变 Agent 决策、工具调用或权限等级。
前端逻辑以源码结构断言 + 后端回放载荷断言为主，真实交互在浏览器里另行验收。
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from aieb.conversation import Message, ToolResultBlock, ToolUseBlock  # noqa: E402
from aieb.remote import RemoteServer  # noqa: E402

WEB = (REPO_ROOT / "aieb" / "web_content.py").read_text(encoding="utf-8")
REMOTE = (REPO_ROOT / "aieb" / "remote.py").read_text(encoding="utf-8")


def js_function(name: str) -> str:
    """截取一个顶层 JS 函数体（到下一个顶层 function/const 之前）。"""
    start = WEB.index(f"function {name}(")
    rest = WEB[start + 1:]
    candidates = [
        pos for pos in (
            rest.find("\nfunction "),
            rest.find("\nconst "),
            rest.find("\nlet "),
        ) if pos > 0
    ]
    end = start + 1 + (min(candidates) if candidates else len(rest))
    return WEB[start:end]


def js_block(start_marker: str, length: int = 1200) -> str:
    start = WEB.index(start_marker)
    return WEB[start:start + length]


class ReplayHarness(RemoteServer):
    """只借用 RemoteServer 的回放逻辑，不跑完整服务器初始化。"""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    def _replay_artifacts(self):
        return {}

    async def _broadcast(self, payload):
        self.sent.append(payload)


def run_replay(messages: list[Message]) -> list[dict]:
    harness = ReplayHarness()
    asyncio.run(RemoteServer._replay_messages(harness, messages))
    return harness.sent


# ---------------------------------------------------------------- 1–2 模式入口


def test_mode_switcher_exists_and_defaults_to_normal():
    """1. 首次打开默认普通模式：控件存在且普通为选中态。"""
    assert 'class="mode-seg"' in WEB
    assert 'data-mode="normal"' in WEB
    assert 'data-mode="developer"' in WEB
    # 普通按钮默认 aria-pressed=true，开发者按钮默认 false
    normal = WEB[WEB.index('data-mode="normal"'):]
    assert 'aria-pressed="true"' in normal[:120]
    dev = WEB[WEB.index('data-mode="developer"'):]
    assert 'aria-pressed="false"' in dev[:120]

    # 运行时默认值也必须是 normal（读不到 localStorage 时）
    loader = js_function("loadDisplayMode")
    assert "return 'normal';" in loader
    assert "let displayMode = 'normal';" in WEB


def test_mode_is_persisted_with_localstorage():
    """2. 模式持久化：写入并读取 localStorage，刷新后保持。"""
    assert "const DISPLAY_MODE_KEY = 'aieb_display_mode';" in WEB
    setter = js_function("applyDisplayMode")
    assert "localStorage.setItem(DISPLAY_MODE_KEY, mode)" in setter
    loader = js_function("loadDisplayMode")
    assert "localStorage.getItem(DISPLAY_MODE_KEY)" in loader
    # 启动时用保存值初始化（而不是硬编码 normal）
    assert "applyDisplayMode(loadDisplayMode(), { persist: false });" in WEB


def test_mode_switch_sends_no_requests():
    """15. 切换模式不会重新执行任务：applyDisplayMode 里没有任何网络调用。"""
    setter = js_function("applyDisplayMode")
    for forbidden in ("ws.send", "fetch(", "user_message", "XMLHttpRequest"):
        assert forbidden not in setter
    # 切换只做重绘
    assert "refreshAllExecPanels()" in setter


# ---------------------------------------------------------------- 3–8 渲染


def test_normal_mode_does_not_build_tool_rows():
    """3/8. 普通模式不渲染工具行；切回普通模式时重建 DOM（不是 CSS 隐藏）。"""
    render = js_function("renderExecPanel")
    # 普通模式在构建 body 之前就返回，工具节点根本不进 DOM
    guard = "if (displayMode !== 'developer' && !summary.count)"
    assert guard in render
    assert "if (displayMode !== 'developer') return;" in render
    # 重绘是基于数据重建：容器数据存在元素上
    assert "__wdExec" in WEB


def test_mode_switch_rerenders_existing_turns():
    """4/8. 切换后已显示的工具记录按新模式重新渲染。"""
    refresh = js_function("refreshAllExecPanels")
    assert ".thinking-block.exec-panel" in refresh
    assert "renderExecPanel(el)" in refresh


def test_tool_events_never_create_assistant_bubbles():
    """4/13. 工具事件不生成独立助手气泡（每次调用只更新一个容器）。"""
    add = js_function("addToolUse")
    update = js_function("updateToolResult")
    for body in (add, update):
        assert "addAssistant(" not in body
        assert "renderExecPanel(currentExecEl)" in body
    # 只有一个容器元素
    assert "function ensureExecPanel()" in WEB


def test_single_container_per_turn():
    """5. 一轮一个执行容器：重复调用复用同一个元素。"""
    ensure = js_function("ensureExecPanel")
    assert "if (currentExecEl && currentExecEl.isConnected) return currentExecEl;" in ensure
    # 新的一轮才重置
    assert "resetExecState()" in WEB


def test_progress_status_comes_from_tool_result():
    """5. 完成状态以执行结果为依据，不靠工具名猜。"""
    summary = js_function("execSummary")
    assert "tool.status" in summary or "execToolStatus" in summary
    update = js_function("updateToolResult")
    assert "tool.status = data.isError ? 'failed' : 'ok';" in update
    # 运行中 → 完成/失败 由数据驱动
    assert "d.running" in summary


def test_developer_mode_shows_real_tool_details():
    """6/7. 开发者模式展示工具名、状态、耗时、参数与结果。"""
    row = js_function("renderExecRow")
    for field in ("tool.name", "execToolStatus(tool)", "tool.elapsed", "tool.args", "tool.output"):
        assert field in row
    assert "name.textContent = tool.name" in row          # 原始工具名（仅详情内）
    assert "'参数'" in row and "'结果'" in row
    for label in ("成功", "失败", "进行中", "已取消", "无记录"):
        assert label in row


def test_developer_header_shows_call_count():
    """6. 标题形如「执行详情 · N 次工具调用」。"""
    render = js_function("renderExecPanel")
    assert "'执行详情 · ' + summary.count + ' 次工具调用'" in render


# ---------------------------------------------------------------- 9–10 历史回放


def test_replay_does_not_append_tool_names_to_text():
    """9. 历史回放不再把 used WriteFile 之类拼进回答正文。"""
    assert "used {" not in REMOTE
    assert "🔧 已执行" not in REMOTE

    messages = [
        Message(role="user", content="记住我的偏好"),
        Message(
            role="assistant",
            content="好的，记住了。",
            tool_uses=[ToolUseBlock(tool_use_id="t1", tool_name="WriteFile", arguments={"file_path": "a.md"})],
        ),
        Message(role="user", content="", tool_results=[ToolResultBlock(tool_use_id="t1", content="ok")]),
    ]
    sent = run_replay(messages)
    assistants = [p for p in sent if p["type"] == "replay_assistant"]
    assert len(assistants) == 1
    content = assistants[0]["data"]["content"]
    assert content == "好的，记住了。"
    assert "WriteFile" not in content
    assert "🔧" not in content
    # 工具信息走结构化字段
    tools = assistants[0]["data"]["tools"]
    assert tools[0]["name"] == "WriteFile"
    assert tools[0]["status"] == "ok"


def test_replay_marks_missing_result_as_unknown():
    """10. 旧会话没有保存工具结果时，状态为 unknown，且不编造耗时。"""
    messages = [
        Message(role="user", content="看看这个"),
        Message(
            role="assistant",
            content="",
            tool_uses=[ToolUseBlock(tool_use_id="t9", tool_name="ReadFile", arguments={"file_path": "x.md"})],
        ),
    ]
    sent = run_replay(messages)
    assistants = [p for p in sent if p["type"] == "replay_assistant"]
    assert len(assistants) == 1
    tool = assistants[0]["data"]["tools"][0]
    assert tool["status"] == "unknown"
    assert tool["elapsed"] is None
    assert tool["output"] == ""


def test_replay_aggregates_one_turn_into_one_message():
    """多段助手文本 + 多次工具调用 → 一条回答（不制造多个空气泡）。"""
    messages = [
        Message(role="user", content="做两件事"),
        Message(role="assistant", content="先看一下。", tool_uses=[
            ToolUseBlock(tool_use_id="a", tool_name="ReadFile", arguments={}),
        ]),
        Message(role="user", content="", tool_results=[ToolResultBlock(tool_use_id="a", content="r1")]),
        Message(role="assistant", content="再写一下。", tool_uses=[
            ToolUseBlock(tool_use_id="b", tool_name="WriteFile", arguments={}),
        ]),
        Message(role="user", content="", tool_results=[ToolResultBlock(tool_use_id="b", content="r2")]),
        Message(role="assistant", content="都完成了。"),
    ]
    sent = run_replay(messages)
    assistants = [p for p in sent if p["type"] == "replay_assistant"]
    assert len(assistants) == 1
    data = assistants[0]["data"]
    assert data["content"] == "先看一下。\n\n再写一下。\n\n都完成了。"
    assert [t["name"] for t in data["tools"]] == ["ReadFile", "WriteFile"]


def test_replay_strips_legacy_tool_notes_but_keeps_prose():
    """5.6 兼容旧会话：剥离程序拼接的提示行，但不误删正文里提到工具名的句子。"""
    legacy = (
        "我读了文件，然后写好了。\n\n"
        "> 🔧 used WriteFile, ReadFile"
    )
    assert RemoteServer._strip_legacy_tool_notes(legacy) == "我读了文件，然后写好了。"

    prose = "你说的是 WriteFile 这个工具名吧？它负责写文件。"
    assert RemoteServer._strip_legacy_tool_notes(prose) == prose

    chinese_note = "已完成。\n\n> 🔧 已执行 3 个操作步骤"
    assert RemoteServer._strip_legacy_tool_notes(chinese_note) == "已完成。"


def test_replay_tool_only_turn_keeps_metadata_only():
    """纯工具轮次：没有正文就不发空内容气泡数据（由前端决定不建气泡）。"""
    messages = [
        Message(role="user", content="存一下"),
        Message(role="assistant", content="", tool_uses=[
            ToolUseBlock(tool_use_id="z", tool_name="SaveMemory", arguments={"content": "x"}),
        ]),
        Message(role="user", content="", tool_results=[ToolResultBlock(tool_use_id="z", content="status: saved")]),
    ]
    sent = run_replay(messages)
    assistants = [p for p in sent if p["type"] == "replay_assistant"]
    assert len(assistants) == 1
    assert assistants[0]["data"]["content"] == ""
    assert assistants[0]["data"]["tools"][0]["status"] == "ok"


# ---------------------------------------------------------------- 脱敏 / 截断


def test_sensitive_arguments_are_masked():
    args = RemoteServer._preview_args({
        "api_key": "sk-123456",
        "Authorization": "Bearer abc",
        "password": "hunter2",
        "file_path": "notes.txt",
    })
    assert args["api_key"] == "«已隐藏»"
    assert args["Authorization"] == "«已隐藏»"
    assert args["password"] == "«已隐藏»"
    assert args["file_path"] == "notes.txt"


def test_long_arguments_and_output_are_truncated():
    args = RemoteServer._preview_args({"content": "x" * 5000})
    assert len(args["content"]) < 700
    assert args["content"].endswith("…")

    output = RemoteServer._preview_output("y" * 5000)
    assert len(output) < 1400
    assert "已截断" in output


def test_live_payloads_use_the_same_redaction():
    """实时事件与历史回放共用同一套脱敏逻辑。

    实时路径先经过 ``_extract_email_draft``（内部调用 ``_preview_output``）再广播，
    历史回放路径直接调用 ``_preview_output``；两条路径都必须走脱敏。
    """
    assert '"args": self._preview_args(event.arguments)' in REMOTE
    assert '"output": clean_output' in REMOTE
    assert 'return draft, cls._preview_output(cleaned)' in REMOTE
    assert '"output": self._preview_output(cleaned)' in REMOTE


# ---------------------------------------------------------------- 权限


def test_permission_payload_has_friendly_and_technical_views():
    """11. 权限请求：普通模式看人话，开发者模式额外看工具名/详情。"""
    view = RemoteServer._permission_view("WriteFile", "C:\\Users\\someone\\notes.md")
    assert view["action"] == "保存一个文件"
    assert view["target"] == "notes.md"          # 不暴露完整路径
    assert "同意" in view["why"]

    payload = REMOTE[REMOTE.index('"type": "permission_request"'):][:800]
    assert '"action": view["action"]' in payload
    assert '"toolName": event.tool_name' in payload
    assert '"description": event.description' in payload


def test_permission_dialog_is_mode_aware_but_always_asks():
    """11. 弹窗只改描述方式；三种选择仍原样回传给后端。"""
    dialog = js_function("addPermissionDialog")
    assert "'允许'" in dialog and "'本会话一直允许'" in dialog and "'拒绝'" in dialog
    assert "mk('btn-allow', '允许', 'allow')" in dialog
    assert "mk('btn-deny', '拒绝', 'deny')" in dialog
    # 开发者专属字段默认隐藏，切换模式时同步
    assert "perm-tool" in dialog and "perm-detail" in dialog
    assert "refreshPermissionDialogs" in js_function("applyDisplayMode")


def test_normal_mode_surfaces_tool_errors():
    """工具失败必须在普通模式里说清楚，而不是藏起来。"""
    hint = js_function("execFailureText")
    assert "failed" in hint
    # 权限被拒 → 翻译成人话
    assert "permission denied" in hint
    assert "你拒绝了这一步" in hint
    render = js_function("renderExecPanel")
    assert "execFailureText(el)" in render
    assert "summary.state === 'failed'" in render


# ---------------------------------------------------------------- XSS


def test_exec_details_build_dom_without_innerhtml_for_tool_data():
    """14. 恶意工具输出无法通过详情视图触发 XSS。"""
    for fn in ("renderExecRow", "renderExecPanel"):
        body = js_function(fn)
        assert "innerHTML" not in body
    row = js_function("renderExecRow")
    assert "document.createTextNode(argsText)" in row
    assert "document.createTextNode(tool.output)" in row


def test_panel_rebuild_clears_previous_children():
    """重绘必须先清空旧节点，否则切回普通模式时开发者内容会残留在 DOM 里。"""
    render = js_function("renderExecPanel")
    assert "while (el.firstChild) el.removeChild(el.firstChild);" in render


def test_artifact_and_memory_ui_untouched():
    """12. 产物卡片、记忆面板等既有组件仍在。"""
    for marker in ("function renderArtifacts", "function openMemoryPanel", "function renderMemoryState"):
        assert marker in WEB


# ---------------------------------------------------------------- 移动端


def test_mobile_layout_rules_exist():
    """16. 窄屏适配：执行详情与权限卡片贴边。"""
    idx = WEB.rindex("@media (max-width: 640px)")
    block = WEB[idx:idx + 1200]
    assert ".thinking-block.exec-panel { margin-left: 0;" in block
    assert ".dialog-card { margin-left: 0;" in block
    assert ".mode-btn" in block
