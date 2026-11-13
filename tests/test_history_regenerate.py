"""第三阶段：重新生成「任意历史回答」+ 版本切换 + 分支持久化。

与 ``test_branch_edit.py`` 的分工：
* 那边测的是「编辑历史提问」；
* 这边测「同一个提问再要一个回答」，也就是按 message_id 重新生成历史轮次，
  以及在生成任务占用时拒绝分支改写。

覆盖：
1. 定位：可以指定任意历史轮次，不只是最后一轮。
2. 上下文隔离：新分支只保留目标轮次之前的内容，后续轮次不重放、不泄漏。
3. 版本：原回答作为兄弟分支保留，可切回；切换不调用模型、不花钱。
4. 持久化：重启后两个版本都在。
5. 并发保护：生成任务进行中拒绝分支改写。
6. 前端契约：每条回答都有「重新生成」+ ‹ 1/2 ›。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "tests"))

from aieb.conversation import Message  # noqa: E402
from aieb.memory.session import SessionManager, is_human_message  # noqa: E402

from test_branch_edit import BranchHarness, js_function, visible  # noqa: E402

WEB = (REPO_ROOT / "aieb" / "web_content.py").read_text(encoding="utf-8")
REMOTE = (REPO_ROOT / "aieb" / "remote.py").read_text(encoding="utf-8")


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- 可辨识的回答


class RegenHarness(BranchHarness):
    """回答带序号，方便断言「第二次的回答确实是新的」。"""

    def __init__(self, tmp_path: Path) -> None:
        super().__init__(tmp_path)
        self._n = 0

    async def _handle_user_message(
        self, content, images=None, documents=None, _same_turn: bool = False
    ) -> None:
        self.resent.append((content, list(images or []), list(documents or [])))
        self._n += 1
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
        self.conversation.add_assistant_message(f"回答#{self._n}：{content}")
        answer = self.conversation.history[-1]
        self.session.append(answer)
        self._persisted_ids.add(id(answer))


def seed(h: RegenHarness) -> None:
    """三轮对话：杭州 → 预算 → 美食。"""
    run(h._handle_user_message("我准备去杭州旅行。"))
    run(h._handle_user_message("帮我安排预算。"))
    run(h._handle_user_message("推荐一些美食。"))


FULL_THREE_TURNS = [
    "我准备去杭州旅行。",
    "回答#1：我准备去杭州旅行。",
    "帮我安排预算。",
    "回答#2：帮我安排预算。",
    "推荐一些美食。",
    "回答#3：推荐一些美食。",
]


# ---------------------------------------------------------------- 1. 定位历史轮次


def test_regenerate_last_turn_by_default(tmp_path: Path) -> None:
    h = RegenHarness(tmp_path)
    seed(h)
    run(h._handle_regenerate())
    assert h.contents()[:4] == FULL_THREE_TURNS[:4]
    assert h.contents()[4:] == ["推荐一些美食。", "回答#4：推荐一些美食。"]


def test_regenerate_historical_turn_targets_that_message(tmp_path: Path) -> None:
    """传历史轮次的 ID：改的是那一轮，不是最后一轮。"""
    h = RegenHarness(tmp_path)
    seed(h)
    budget_id = h.human(1).message_id

    run(h._handle_regenerate(budget_id))

    # 时间线在第二轮处重新展开：第一轮保留，第三轮不出现
    assert h.contents() == [
        "我准备去杭州旅行。",
        "回答#1：我准备去杭州旅行。",
        "帮我安排预算。",
        "回答#4：帮我安排预算。",
    ]
    # 新回答挂在「同一个提问」的新版本上
    new_id = h.human(1).message_id
    assert new_id != budget_id
    assert h.session.siblings(budget_id) == [budget_id, new_id]
    assert h.session.version_info(budget_id) == {"index": 0, "total": 2}
    assert h.session.version_info(new_id) == {"index": 1, "total": 2}
    # 无关轮次没有被算成版本
    assert h.session.version_info(h.human(0).message_id) == {"index": 0, "total": 1}


def test_regenerate_historical_turn_does_not_leak_later_context(tmp_path: Path) -> None:
    h = RegenHarness(tmp_path)
    seed(h)
    budget_id = h.human(1).message_id

    run(h._handle_regenerate(budget_id))

    contents = h.contents()
    assert "推荐一些美食。" not in contents
    assert "回答#3：推荐一些美食。" not in contents
    # 只重新请求了目标轮次这一个问题
    assert h.resent[-1][0] == "帮我安排预算。"
    assert len(h.resent) == 4  # 3 次初始 + 1 次重新生成


def test_regenerate_reuses_original_attachments(tmp_path: Path) -> None:
    from aieb.conversation import DocumentBlock, ImageBlock

    h = RegenHarness(tmp_path)
    img = ImageBlock(media_type="image/png", data="AAA")
    doc = DocumentBlock(media_type="application/pdf", data="BBB", name="行程.pdf")
    run(h._handle_user_message("看看这个", images=[img], documents=[doc]))
    run(h._handle_user_message("再补充一下"))
    target = h.human(0).message_id

    run(h._handle_regenerate(target))

    content, images, documents = h.resent[-1]
    assert content == "看看这个"
    assert [i.data for i in images] == ["AAA"]
    assert [d.name for d in documents] == ["行程.pdf"]


def test_regenerate_unknown_message_id_is_rejected(tmp_path: Path) -> None:
    h = RegenHarness(tmp_path)
    seed(h)
    before = h.contents()
    run(h._handle_regenerate("m_not_here"))
    assert h.contents() == before
    assert any("找不到" in e for e in h.errors())


def test_regenerate_rejects_non_human_message(tmp_path: Path) -> None:
    h = RegenHarness(tmp_path)
    seed(h)
    assistant_id = next(
        m.message_id for m in h.session.nodes.values() if m.role == "assistant"
    )
    before = h.contents()
    run(h._handle_regenerate(assistant_id))
    assert h.contents() == before
    assert h.errors()


# ---------------------------------------------------------------- 2. 版本切换


def test_regenerated_answer_keeps_old_version_switchable(tmp_path: Path) -> None:
    h = RegenHarness(tmp_path)
    seed(h)
    budget_id = h.human(1).message_id
    run(h._handle_regenerate(budget_id))
    new_id = h.human(1).message_id

    # 切回旧版本：完整三轮都在，且内容是旧的
    run(h._handle_switch_turn_version(new_id, index=0))
    assert h.contents() == FULL_THREE_TURNS

    # 切到新版本：只剩两轮，第二轮是新的
    run(h._handle_switch_turn_version(budget_id, index=1))
    assert h.contents()[-2:] == ["帮我安排预算。", "回答#4：帮我安排预算。"]


def test_switching_regenerated_versions_does_not_call_model(tmp_path: Path) -> None:
    h = RegenHarness(tmp_path)
    seed(h)
    budget_id = h.human(1).message_id
    run(h._handle_regenerate(budget_id))
    new_id = h.human(1).message_id
    calls = len(h.resent)

    run(h._handle_switch_turn_version(new_id, index=0))
    run(h._handle_switch_turn_version(budget_id, index=1))
    assert len(h.resent) == calls  # 切换是纯读操作


def test_regenerate_twice_gives_three_versions(tmp_path: Path) -> None:
    h = RegenHarness(tmp_path)
    seed(h)
    last_id = h.human(2).message_id
    run(h._handle_regenerate(last_id))
    second_id = h.human(2).message_id
    run(h._handle_regenerate(second_id))

    assert h.session.version_info(second_id)["total"] == 3
    ids = h.session.siblings(second_id)
    assert len(ids) == 3
    # 三个版本都能按索引切到
    for index in range(3):
        run(h._handle_switch_turn_version(second_id, index=index))
        assert h.contents()[-2] == "推荐一些美食。"


def test_message_on_hidden_branch_cannot_be_regenerated(tmp_path: Path) -> None:
    """已经切走的分支上的提问不在当前历史里：明确拒绝，而不是猜。"""
    h = RegenHarness(tmp_path)
    seed(h)
    old_id = h.human(1).message_id
    run(h._handle_regenerate(old_id))
    h.errors().clear()

    # 现在当前分支上的第二轮是新的，old_id 只存在于兄弟分支
    run(h._handle_regenerate(old_id))

    assert any("找不到" in e for e in h.errors())
    # 当前分支没有被破坏
    assert h.contents()[-2:] == ["帮我安排预算。", "回答#4：帮我安排预算。"]


# ---------------------------------------------------------------- 3. 持久化


def test_regenerated_version_survives_restart(tmp_path: Path) -> None:
    h = RegenHarness(tmp_path)
    seed(h)
    budget_id = h.human(1).message_id
    run(h._handle_regenerate(budget_id))
    new_id = h.human(1).message_id
    h.session.close()

    manager = SessionManager(str(tmp_path))
    result = manager.resume(h.session_id)
    assert result is not None
    # 落盘的是当前分支（新版本，只有两轮）
    assert visible(result.messages) == [
        "我准备去杭州旅行。",
        "回答#1：我准备去杭州旅行。",
        "帮我安排预算。",
        "回答#4：帮我安排预算。",
    ]
    # 旧分支仍然能切回去
    assert result.session.siblings(budget_id) == [budget_id, new_id]
    back = result.session.switch_to(budget_id)
    assert visible(back) == FULL_THREE_TURNS
    result.session.close()


def test_edit_after_regenerate_branches_from_new_version(tmp_path: Path) -> None:
    """重新生成后再编辑同一轮，应该挂在**当前**版本之下，而不是旧分支。"""
    h = RegenHarness(tmp_path)
    seed(h)
    budget_id = h.human(1).message_id
    run(h._handle_regenerate(budget_id))
    new_id = h.human(1).message_id

    run(h._handle_edit_message(new_id, "预算要省一点。"))

    assert h.contents() == [
        "我准备去杭州旅行。",
        "回答#1：我准备去杭州旅行。",
        "预算要省一点。",
        "回答#5：预算要省一点。",
    ]
    assert h.session.version_info(new_id) == {"index": 1, "total": 3}
    assert len(h.session.siblings(new_id)) == 3


# ---------------------------------------------------------------- 4. 并发保护


class _BusyHost:
    """只要有生成任务在跑就算忙。"""

    def pending_video_tasks(self):
        return [object()]


def test_regenerate_blocked_while_generation_task_running(tmp_path: Path) -> None:
    h = RegenHarness(tmp_path)
    seed(h)
    h._cap_hosts = {h.session_id: _BusyHost()}
    before = h.contents()

    run(h._handle_regenerate(h.human(1).message_id))

    assert h.contents() == before
    assert len(h.resent) == 3  # 没有触发新的模型请求
    assert any("生成任务" in e for e in h.errors())


def test_edit_blocked_while_generation_task_running(tmp_path: Path) -> None:
    h = RegenHarness(tmp_path)
    seed(h)
    h._cap_hosts = {h.session_id: _BusyHost()}
    before = h.contents()

    run(h._handle_edit_message(h.human(1).message_id, "改一下"))

    assert h.contents() == before
    assert any("生成任务" in e for e in h.errors())


def test_busy_guard_is_quiet_when_no_generation_task(tmp_path: Path) -> None:
    h = RegenHarness(tmp_path)
    assert h._capability_busy_reason() == ""
    h._cap_hosts = {h.session_id: type("Idle", (), {"pending_video_tasks": lambda self: []})()}
    assert h._capability_busy_reason() == ""


# ---------------------------------------------------------------- 4.5 重启恢复


class _RestoreHarness:
    """只保留 _restore_last_session 需要的那几个属性。"""

    def __init__(self, tmp_path: Path) -> None:
        from aieb.conversation import ConversationManager

        self.session_manager = SessionManager(str(tmp_path))
        self.session = self.session_manager.create()
        self.session_id = self.session.session_id
        self.conversation = ConversationManager()
        self._persisted_ids: set[int] = set()
        self._turn_no = 0


def test_restart_restores_last_session(tmp_path: Path) -> None:
    """重启后应恢复到最近的会话，而不是一个空对话。"""
    mgr = SessionManager(str(tmp_path))
    sess = mgr.create()
    messages = [
        Message(role="user", content="第一问"),
        Message(role="assistant", content="第一答"),
        Message(role="user", content="第二问"),
        Message(role="assistant", content="第二答"),
    ]
    for m in messages:
        sess.append(m)
    sid = sess.session_id
    sess.close()

    h = _RestoreHarness(tmp_path)
    from aieb.remote import RemoteServer

    RemoteServer._restore_last_session(h)
    assert h.session_id == sid
    assert [m.content for m in h.conversation.history] == [
        "第一问", "第一答", "第二问", "第二答",
    ]
    assert h._turn_no == 2
    assert len(h._persisted_ids) == 4
    h.session.close()


def test_restart_skips_empty_sessions(tmp_path: Path) -> None:
    mgr = SessionManager(str(tmp_path))
    empty = mgr.create()  # 只有元数据、没有消息
    empty.close()
    good = mgr.create()
    for m in (Message(role="user", content="有内容"), Message(role="assistant", content="答")):
        good.append(m)
    good_id = good.session_id
    good.close()

    h = _RestoreHarness(tmp_path)
    from aieb.remote import RemoteServer

    RemoteServer._restore_last_session(h)
    assert h.session_id == good_id
    h.session.close()


def test_restart_without_history_keeps_new_session(tmp_path: Path) -> None:
    h = _RestoreHarness(tmp_path)
    original = h.session_id
    from aieb.remote import RemoteServer

    RemoteServer._restore_last_session(h)
    assert h.session_id == original
    assert h.conversation.history == []
    h.session.close()


def test_restart_restores_current_branch(tmp_path: Path) -> None:
    """重启后恢复的是**当前分支**，不是最早的叶子。"""
    mgr = SessionManager(str(tmp_path))
    sess = mgr.create()
    q1 = Message(role="user", content="杭州")
    a1 = Message(role="assistant", content="杭州答")
    sess.append(q1)
    sess.append(a1)
    # 用「第一轮重新生成」的方式分叉：从根节点重开一条分支
    sess.branch_to(None)
    q1b = Message(role="user", content="苏州")
    a1b = Message(role="assistant", content="苏州答")
    sess.append(q1b)
    sess.append(a1b)
    assert len(sess.siblings(q1b.message_id)) == 2
    sess.close()

    h = _RestoreHarness(tmp_path)
    from aieb.remote import RemoteServer

    RemoteServer._restore_last_session(h)
    # 当前分支是苏州那条
    assert [m.content for m in h.conversation.history] == ["苏州", "苏州答"]
    # 旧分支仍然可切回
    back = h.session.switch_to(q1.message_id)
    assert [m.content for m in back] == ["杭州", "杭州答"]
    h.session.close()


# ---------------------------------------------------------------- 5. 回放载荷

def test_replay_assistant_points_back_to_its_question(tmp_path: Path) -> None:
    h = RegenHarness(tmp_path)
    seed(h)
    budget_id = h.human(1).message_id
    run(h._handle_regenerate(budget_id))
    h.sent.clear()

    run(h._replay_messages(h.conversation.history))

    users = [p for p in h.sent if p.get("type") == "replay_user"]
    answers = [p for p in h.sent if p.get("type") == "replay_assistant"]
    assert [u["data"]["message_id"] for u in users] == [
        h.human(0).message_id,
        h.human(1).message_id,
    ]
    # 每条回答都能指回自己的提问，并带上「第几版 / 共几版」
    assert answers[0]["data"]["user_message_id"] == users[0]["data"]["message_id"]
    assert answers[1]["data"]["user_message_id"] == users[1]["data"]["message_id"]
    assert answers[1]["data"]["version"] == {"index": 1, "total": 2}
    assert answers[0]["data"]["version"] == {"index": 0, "total": 1}


def test_replay_keeps_artifacts_of_regenerated_turn(tmp_path: Path) -> None:
    """重新生成不会删掉上一轮已经生成好的产物（按轮次挂载）。"""

    class ArtifactHarness(RegenHarness):
        def _replay_artifacts(self):
            return {1: [{"kind": "image", "url": "https://example.invalid/a.png"}]}

    h = ArtifactHarness(tmp_path)
    seed(h)
    run(h._handle_regenerate(h.human(1).message_id))
    h.sent.clear()
    run(h._replay_messages(h.conversation.history))

    answers = [p for p in h.sent if p.get("type") == "replay_assistant"]
    assert answers[0]["data"]["artifacts"][0]["url"] == "https://example.invalid/a.png"


def test_human_ancestor_is_used_for_replay_turn(tmp_path: Path) -> None:
    """回放的轮次归属走 human_ancestor：工具轮次不会把回答挂到错的提问上。"""
    h = RegenHarness(tmp_path)
    seed(h)
    for message in h.conversation.history:
        if message.role == "assistant":
            assert h.session.human_ancestor(message.message_id) is not None
            assert is_human_message(h.session.nodes[h.session.human_ancestor(message.message_id)])


# ---------------------------------------------------------------- 6. 前端契约


def test_every_answer_gets_regenerate_button():
    fn = js_function("attachRetryButton")
    assert "重新生成" in fn
    assert "message_id: options.userMessageId || ''" in fn
    # 不再有「只保留最新一条」的摘除逻辑
    assert "lastRetryBtn" not in fn
    assert "lastRetryBtn" not in WEB


def test_regenerate_button_sits_in_shared_actions_bar():
    bar = js_function("assistantActionsBar")
    assert "msg-assistant-actions" in bar
    retry = js_function("attachRetryButton")
    assert "assistantActionsBar(row)" in retry
    nav = js_function("attachAssistantVersionNav")
    assert "assistantActionsBar(row)" in nav
    assert "renderTurnVersionNav(nav," in nav
    # CSS 里真的有这个容器
    assert ".msg-assistant-actions" in WEB


def test_replay_assistant_wires_version_nav_from_payload():
    start = WEB.index("case 'replay_assistant'")
    block = WEB[start: start + 1400]
    assert "msg.data.user_message_id" in block
    assert "attachAssistantVersionNav(el, msg.data.user_message_id" in block
    assert "msg.data.version" in block


def test_live_answer_gets_retry_and_version_nav():
    start = WEB.index("case 'loop_complete'")
    block = WEB[start: start + 1600]
    assert "attachRetryButton(currentAssistantEl" in block
    assert "userMessageId:" in block
    assert "attachAssistantVersionNav(" in block


def test_user_message_keeps_version_for_answer_nav():
    fn = js_function("addUser")
    assert "version: (meta && meta.version) || null" in fn
    start = WEB.index("case 'user_message_saved'")
    block = WEB[start: start + 700]
    assert "lastUserRow.__wdMsg.version = version" in block


def test_generated_buttons_visible_on_touch():
    hover_block = WEB[WEB.index("@media (hover: none)") - 400:]
    hover_block = hover_block[:1200]
    assert ".msg-retry-btn { opacity: 1; }" in WEB
    assert ".msg-edit-btn { opacity: 0.75; }" in WEB
    assert "@media (hover: none)" in hover_block or "@media (hover: none)" in WEB


def test_backend_regenerate_accepts_message_id():
    assert 'self._handle_regenerate(str(data.get("message_id") or ""))' in REMOTE
    assert "async def _handle_regenerate(self, message_id: str = \"\")" in REMOTE
    body = REMOTE[REMOTE.index("async def _handle_regenerate"):]
    body = body[: body.index("async def _handle_edit_message")]
    assert "_find_human_message_index(message_id)" in body
    assert "_capability_busy_reason()" in body
    assert "_start_branch_and_resend(" in body
    # 不重写历史、不删除旧分支
    assert "history.clear()" not in body
