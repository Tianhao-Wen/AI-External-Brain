"""编辑任意历史消息 + 对话分支（版本）的测试。

覆盖四个层面：
1. **持久化**（Session 对话树）：编辑早期消息生成兄弟分支，旧分支不丢，
   关闭会话再重新 resume 后仍然能切回去。
2. **后端处理器**（RemoteServer）：按 message_id 精确定位、拒绝非法消息、
   跨会话不可编辑、并发保护、切换版本不调用模型。
3. **附件**：编辑时按前端选择保留/移除，不做静默丢弃。
4. **前端契约**（web_content.py 源码）：每条用户消息都有编辑入口、
   外部链接一律新标签页打开且不绕过 DOMPurify。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from aieb.conversation import (  # noqa: E402
    ConversationManager,
    DocumentBlock,
    ImageBlock,
    Message,
    ToolResultBlock,
    ToolUseBlock,
)
from aieb.memory.session import (  # noqa: E402
    SessionManager,
    is_human_message,
    legacy_message_id,
)
from aieb.remote import RemoteServer  # noqa: E402

WEB = (REPO_ROOT / "aieb" / "web_content.py").read_text(encoding="utf-8")
REMOTE = (REPO_ROOT / "aieb" / "remote.py").read_text(encoding="utf-8")


def run(coro):
    return asyncio.run(coro)


def js_function(name: str) -> str:
    """截取一个顶层 JS 函数体（到下一个顶层声明之前）。"""
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


# ---------------------------------------------------------------- 假服务器


class BranchHarness(RemoteServer):
    """只跑编辑/切换分支相关逻辑，用假的 _handle_user_message 代替真实模型。"""

    def __init__(self, tmp_path: Path) -> None:
        self.sent: list[dict] = []
        self.manager = SessionManager(str(tmp_path))
        self.session = self.manager.create()
        self.session_id = self.session.session_id
        self.conversation = ConversationManager()
        self._persisted_ids: set[int] = set()
        self._streaming = False
        self._branch_busy = False
        self._turn_no = 0
        self._stores: dict = {}
        self.agent = None
        self.registry = None
        self._cancel_event = None
        self.resent: list[tuple[str, list, list]] = []

    # -- 覆盖掉真正的模型调用 --
    async def _handle_user_message(
        self, content, images=None, documents=None, _same_turn: bool = False
    ) -> None:
        self.resent.append((content, list(images or []), list(documents or [])))
        # 复刻真实 agent 的行为：用户消息之后会追加环境说明与 system-reminder
        if not self.conversation.env_injected:
            self.conversation.inject_environment("Current working directory: /tmp")
        self.conversation.add_user_message(content, images=images, documents=documents)
        question = self.conversation.history[-1]
        self.session.append(question)
        self._persisted_ids.add(id(question))
        if not _same_turn:
            reminder = Message(role="user", content="<system-reminder>\nAs you answer…")
            self.conversation.history.append(reminder)
            self.session.append(reminder)
            self._persisted_ids.add(id(reminder))
        self.conversation.add_assistant_message(f"回答：{content}")
        answer = self.conversation.history[-1]
        self.session.append(answer)
        self._persisted_ids.add(id(answer))

    # -- 回放不需要真实产物 --
    def _replay_artifacts(self):
        return {}

    async def _broadcast(self, payload):
        self.sent.append(payload)

    # -- 测试辅助 --
    @staticmethod
    def _visible(message: Message) -> bool:
        if message.tool_results:
            return False
        content = message.content or ""
        return not content.startswith("<system-reminder>") and not content.startswith(
            "Current working directory:"
        )

    def contents(self) -> list[str]:
        """只看真实对话轮次（跳过 env / system-reminder 注入）。"""
        return [m.content for m in self.conversation.history if self._visible(m)]

    def human(self, index: int = 0) -> Message:
        humans = [m for m in self.conversation.history if is_human_message(m)]
        return humans[index]

    def errors(self) -> list[str]:
        return [
            p["data"]["message"]
            for p in self.sent
            if p.get("type") == "error"
        ]


def seed(h: BranchHarness) -> None:
    """三轮对话：杭州 → 预算 → 美食。"""
    run(h._handle_user_message("我准备去杭州旅行。"))
    run(h._handle_user_message("帮我安排预算。"))
    run(h._handle_user_message("推荐一些美食。"))


def visible(messages: list[Message]) -> list[str]:
    """只保留真实对话轮次（跳过 env / system-reminder 注入）。"""
    return [m.content for m in messages if BranchHarness._visible(m)]


# ---------------------------------------------------------------- 1. 持久化：分支


def test_edit_first_turn_creates_sibling_branch(tmp_path: Path) -> None:
    h = BranchHarness(tmp_path)
    seed(h)
    first_id = h.human(0).message_id

    run(h._handle_edit_message(first_id, "我准备去苏州旅行。"))

    # 新分支：只有新的第一轮 + 它的回答
    assert h.contents() == ["我准备去苏州旅行。", "回答：我准备去苏州旅行。"]
    # 旧分支的消息一条都没删：旧 6 条真实消息全部还在树里，再加新的 2 条
    all_humans = [mid for mid, m in h.session.nodes.items() if is_human_message(m)]
    assert len(all_humans) == 4  # 杭州 / 预算 / 美食 / 苏州
    assert "我准备去杭州旅行。" in [m.content for m in h.session.nodes.values()]


def test_old_branch_is_preserved_and_switchable(tmp_path: Path) -> None:
    h = BranchHarness(tmp_path)
    seed(h)
    first_id = h.human(0).message_id
    old_leaves = [m.message_id for m in h.conversation.history if BranchHarness._visible(m)]

    run(h._handle_edit_message(first_id, "我准备去苏州旅行。"))
    assert h.contents()[0] == "我准备去苏州旅行。"

    # 版本信息：同一个位置两个版本
    info = h.session.version_info(first_id)
    assert info == {"index": 0, "total": 2}

    new_id = h.human(0).message_id
    assert h.session.version_info(new_id) == {"index": 1, "total": 2}
    assert h.session.siblings(first_id) == [first_id, new_id]

    # 切回旧分支：杭州预算美食的完整三轮都在
    run(h._handle_switch_turn_version(new_id, index=0))
    assert visible(h.conversation.history) == [
        "我准备去杭州旅行。",
        "回答：我准备去杭州旅行。",
        "帮我安排预算。",
        "回答：帮我安排预算。",
        "推荐一些美食。",
        "回答：推荐一些美食。",
    ]
    # 切回去的是**同一批消息**（没重新生成、没重复执行）
    assert [m.message_id for m in h.conversation.history if BranchHarness._visible(m)] == old_leaves
    # 再切到新分支
    run(h._handle_switch_turn_version(first_id, index=1))
    assert h.contents() == ["我准备去苏州旅行。", "回答：我准备去苏州旅行。"]


def test_branch_survives_reopen(tmp_path: Path) -> None:
    h = BranchHarness(tmp_path)
    seed(h)
    first_id = h.human(0).message_id
    run(h._handle_edit_message(first_id, "我准备去苏州旅行。"))
    new_id = h.human(0).message_id
    h.session.close()

    # 模拟重启服务
    manager = SessionManager(str(tmp_path))
    result = manager.resume(h.session_id)
    assert result is not None
    assert visible(result.messages) == [
        "我准备去苏州旅行。",
        "回答：我准备去苏州旅行。",
    ]
    # 两个版本都还在，可以切回杭州分支
    assert result.session.siblings(first_id) == [first_id, new_id]
    back = result.session.switch_to(first_id)
    assert visible(back)[:2] == ["我准备去杭州旅行。", "回答：我准备去杭州旅行。"]
    assert len(visible(back)) == 6
    result.session.close()


def test_switch_branch_does_not_call_model(tmp_path: Path) -> None:
    h = BranchHarness(tmp_path)
    seed(h)
    first_id = h.human(0).message_id
    run(h._handle_edit_message(first_id, "我准备去苏州旅行。"))
    calls_before = len(h.resent)

    run(h._handle_switch_turn_version(h.human(0).message_id, index=0))
    run(h._handle_switch_turn_version(first_id, index=1))
    assert len(h.resent) == calls_before  # 没有重新请求模型


def test_new_branch_does_not_inherit_old_branch_tail(tmp_path: Path) -> None:
    h = BranchHarness(tmp_path)
    seed(h)
    first_id = h.human(0).message_id
    run(h._handle_edit_message(first_id, "我准备去苏州旅行。"))

    contents = h.contents()
    assert "帮我安排预算。" not in contents
    assert "推荐一些美食。" not in contents
    # 继续提问会挂在新分支上
    run(h._handle_user_message("苏州住哪里？"))
    assert h.contents()[:2] == ["我准备去苏州旅行。", "回答：我准备去苏州旅行。"]
    assert h.contents()[2] == "苏州住哪里？"


def test_edit_middle_turn_keeps_prefix(tmp_path: Path) -> None:
    h = BranchHarness(tmp_path)
    seed(h)
    second_id = h.human(1).message_id
    run(h._handle_edit_message(second_id, "预算不用太紧。"))
    assert h.contents() == [
        "我准备去杭州旅行。",
        "回答：我准备去杭州旅行。",
        "预算不用太紧。",
        "回答：预算不用太紧。",
    ]
    # 第一轮没有被改成版本
    assert h.session.version_info(h.human(0).message_id) == {"index": 0, "total": 1}


def test_edit_last_turn_also_branches(tmp_path: Path) -> None:
    h = BranchHarness(tmp_path)
    seed(h)
    last_id = h.human(2).message_id
    run(h._handle_edit_message(last_id, "推荐一些咖啡店。"))
    assert h.contents()[-2:] == ["推荐一些咖啡店。", "回答：推荐一些咖啡店。"]
    assert h.session.version_info(last_id) == {"index": 0, "total": 2}


def test_regenerate_creates_second_version(tmp_path: Path) -> None:
    h = BranchHarness(tmp_path)
    seed(h)
    last_id = h.human(2).message_id
    run(h._handle_regenerate())
    assert h.session.version_info(last_id) == {"index": 0, "total": 2}
    # 重新生成后 history 里最后一条仍然是同一个问题的新版本
    assert h.contents()[-2] == "推荐一些美食。"
    assert len(h.contents()) == 6


# ---------------------------------------------------------------- 2. 定位与校验


def test_edit_unknown_message_id_is_rejected(tmp_path: Path) -> None:
    h = BranchHarness(tmp_path)
    seed(h)
    before = h.contents()
    run(h._handle_edit_message("m_does_not_exist", "改一下"))
    assert h.contents() == before
    assert any("找不到" in e for e in h.errors())


def test_edit_tool_result_message_is_rejected(tmp_path: Path) -> None:
    h = BranchHarness(tmp_path)
    h.conversation.add_user_message("用工具查一下")
    h.conversation.add_assistant_message(
        "", tool_uses=[ToolUseBlock(tool_use_id="t1", tool_name="Bash", arguments={})]
    )
    h.conversation.add_user_message("")  # 占位，下面替换成工具结果消息
    h.conversation.history[-1] = Message(
        role="user", content="", tool_results=[ToolResultBlock(tool_use_id="t1", content="ok")]
    )
    for m in h.conversation.history:
        h.session.append(m)
        h._persisted_ids.add(id(m))
    tool_msg_id = h.conversation.history[-1].message_id

    before = len(h.conversation.history)
    run(h._handle_edit_message(tool_msg_id, "改一下"))
    assert len(h.conversation.history) == before
    assert h.errors()


def test_edit_system_reminder_is_rejected(tmp_path: Path) -> None:
    h = BranchHarness(tmp_path)
    h.conversation.add_user_message("正常提问")
    h.conversation.add_system_reminder("环境信息")
    for m in h.conversation.history:
        h.session.append(m)
        h._persisted_ids.add(id(m))
    reminder_id = h.conversation.history[-1].message_id
    run(h._handle_edit_message(reminder_id, "改一下"))
    assert h.errors()


def test_cannot_edit_message_from_another_session(tmp_path: Path) -> None:
    h = BranchHarness(tmp_path)
    seed(h)
    other = h.manager.create()
    other.append(Message(role="user", content="另一个会话的问题"))
    foreign_id = other.current_path()[0].message_id
    other.close()

    before = h.contents()
    run(h._handle_edit_message(foreign_id, "试图跨会话编辑"))
    assert h.contents() == before
    assert any("找不到" in e for e in h.errors())


def test_empty_message_id_falls_back_to_last_human(tmp_path: Path) -> None:
    """刚发出、前端还没拿到 ID 时，退化成编辑最后一条（兼容路径）。"""
    h = BranchHarness(tmp_path)
    seed(h)
    run(h._handle_edit_message("", "推荐一些咖啡店。"))
    assert h.contents()[-2] == "推荐一些咖啡店。"


def test_edit_while_streaming_is_rejected(tmp_path: Path) -> None:
    h = BranchHarness(tmp_path)
    seed(h)
    first_id = h.human(0).message_id
    before = h.contents()
    h._streaming = True
    run(h._handle_edit_message(first_id, "改一下"))
    assert h.contents() == before
    assert any("正在生成" in e for e in h.errors())
    assert not h.resent or len(h.resent) == 3


def test_concurrent_edit_requests_do_not_interleave(tmp_path: Path) -> None:
    """_branch_busy 在第一次 await 之前置位，第二个请求必须被拒绝。"""
    h = BranchHarness(tmp_path)
    seed(h)
    first_id = h.human(0).message_id
    h._branch_busy = True
    before = h.contents()
    run(h._handle_edit_message(first_id, "改一下"))
    assert h.contents() == before
    assert any("正在生成" in e or "稍后" in e for e in h.errors())
    h._branch_busy = False


# ---------------------------------------------------------------- 3. 附件


def test_edit_keeps_attachments_when_requested(tmp_path: Path) -> None:
    h = BranchHarness(tmp_path)
    img = ImageBlock(media_type="image/png", data="AAA")
    doc = DocumentBlock(media_type="application/pdf", data="BBB", name="行程.pdf")
    run(h._handle_user_message("看看这个", images=[img], documents=[doc]))
    msg_id = h.human(0).message_id

    run(
        h._handle_edit_message(
            msg_id, "还是看看这个", images=[img], documents=[doc]
        )
    )
    content, images, documents = h.resent[-1]
    assert content == "还是看看这个"
    assert len(images) == 1 and images[0].data == "AAA"
    assert len(documents) == 1 and documents[0].name == "行程.pdf"
    # 新分支的消息里附件仍然在
    latest = [m for m in h.conversation.history if m.images or m.documents][0]
    assert latest.images and latest.documents


def test_edit_drops_removed_attachments(tmp_path: Path) -> None:
    h = BranchHarness(tmp_path)
    img = ImageBlock(media_type="image/png", data="AAA")
    run(h._handle_user_message("看看这个", images=[img]))
    msg_id = h.human(0).message_id

    run(h._handle_edit_message(msg_id, "不看图了", images=[], documents=[]))
    content, images, documents = h.resent[-1]
    assert content == "不看图了"
    assert images == [] and documents == []
    assert not any(m.images for m in h.conversation.history)


# ---------------------------------------------------------------- 4. 回放载荷


def test_replay_carries_message_id_and_version(tmp_path: Path) -> None:
    h = BranchHarness(tmp_path)
    seed(h)
    first_id = h.human(0).message_id
    run(h._handle_edit_message(first_id, "我准备去苏州旅行。"))
    h.sent.clear()
    run(RemoteServer._replay_messages(h, h.conversation.history))
    users = [p for p in h.sent if p.get("type") == "replay_user"]
    assert len(users) == 1
    data = users[0]["data"]
    assert data["message_id"] == h.human(0).message_id
    assert data["version"]["total"] == 2
    assert data["version"]["index"] == 1


def test_replay_of_legacy_messages_gets_ids(tmp_path: Path) -> None:
    """旧版本写盘的 jsonl 没有 message_id：resume 时补确定性 ID，可编辑可回放。"""
    sessions = tmp_path / ".aieb" / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    sid = "session_legacy_0001"
    jsonl = sessions / f"{sid}.jsonl"
    jsonl.write_text(
        "\n".join([
            '{"type":"user","content":"老问题","timestamp":"2026-10-01T10:00:00+00:00"}',
            '{"type":"assistant","content":"老回答","timestamp":"2026-10-01T10:00:01+00:00"}',
        ]) + "\n",
        encoding="utf-8",
    )
    (sessions / f"{sid}.meta").write_text(
        '{"id":"%s","title":"旧会话","summary":"","message_count":2,'
        '"total_tokens":0,"created_at":"2026-10-01T10:00:00+00:00",'
        '"last_active":"2026-10-01T10:00:01+00:00"}' % sid,
        encoding="utf-8",
    )

    manager = SessionManager(str(tmp_path))
    result = manager.resume(sid)
    assert result is not None
    assert [m.message_id for m in result.messages] == [
        legacy_message_id(1),
        legacy_message_id(2),
    ]
    assert result.messages[1].parent_id == legacy_message_id(1)
    result.session.close()


# ---------------------------------------------------------------- 5. 前端契约


def test_every_user_message_gets_edit_button():
    fn = js_function("addUser")
    assert "startEditMessage(row, col, bubble, editBtn, actionsRow)" in fn
    # 不再有「只保留最新一条」的逻辑
    assert "lastUserActionsRow" not in WEB
    assert "actionsRow.remove()" not in fn


def test_edit_ui_has_chinese_buttons_and_attachment_control():
    fn = js_function("startEditMessage")
    assert "'取消'" in fn
    assert "'保存并发送'" in fn
    # 附件可逐个移除，且会随请求回传
    assert "keptImages" in fn and "keptDocuments" in fn and "keptFiles" in fn
    assert "type: 'edit_message'" in fn
    assert "message_id: msg.id" in fn


def test_edit_is_blocked_while_streaming():
    fn = js_function("startEditMessage")
    assert "if (streaming)" in fn
    assert "暂时不能编辑" in fn
    # 同时只允许一个编辑框
    assert "activeEditor" in fn


def test_version_nav_uses_message_id():
    fn = js_function("renderTurnVersionNav")
    assert "messageId" in fn
    assert "switchTurnVersion(messageId" in fn
    switcher = js_function("switchTurnVersion")
    assert "message_id: messageId" in switcher


def test_external_links_open_in_new_tab():
    policy = js_function("applyExternalLinkPolicy")
    assert "'target', '_blank'" in policy
    assert "'rel', SAFE_LINK_REL" in policy
    assert "referrerpolicy" in policy
    assert "noopener noreferrer" in WEB
    # 危险协议被摘掉 href
    assert "a.removeAttribute('href')" in policy
    # 三处接线都在
    assert "hardenLinks(contentEl)" in js_function("updateAssistant")
    assert "ensureSanitizerHooks()" in js_function("renderMarkdown")
    assert "DOMPurify.addHook('afterSanitizeAttributes'" in WEB
    # 点击兜底：补属性但不阻止默认行为（不用 window.open，避免被弹窗拦截）
    assert "addEventListener('click'" in WEB  # 委托存在
    click_guard = WEB[WEB.index("messagesEl.addEventListener('click'"):]
    click_guard = click_guard[: click_guard.index("});") + 3]
    assert "setAttribute('target', '_blank')" in click_guard
    assert "preventDefault" not in click_guard
    assert "window.open" not in click_guard


def test_sanitizer_still_enabled():
    assert "DOMPurify.sanitize(html, SANITIZE_OPTIONS)" in WEB
    assert "ALLOW_UNKNOWN_PROTOCOLS: false" in WEB
    # 不允许为了新标签页关闭过滤
    assert "ALLOWED_TAGS: ['a'" not in WEB


def test_backend_no_longer_uses_last_message_only():
    assert "_find_last_human_message_index" in REMOTE  # 仍保留给 regenerate
    assert "def _find_human_message_index(self, message_id: str)" in REMOTE
    assert "is_human_message(message)" in REMOTE
    assert "def _start_branch_and_resend" in REMOTE
    # 编辑/切换不再重写（会抹掉旧分支）
    edit_body = REMOTE[REMOTE.index("async def _handle_edit_message"):]
    edit_body = edit_body[: edit_body.index("async def _handle_switch_turn_version")]
    assert "session.rewrite" not in edit_body
