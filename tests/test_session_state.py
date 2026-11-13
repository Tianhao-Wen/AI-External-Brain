"""阶段 A：会话状态与历史会话管理的单元测试。

覆盖两处后端行为改动：
1. ``derive_title``：会话标题必须简短可读，不能把整句长问题塞进侧边栏；
2. ``RemoteServer._build_session_list``：空会话不进列表（但当前会话必须保留）。
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from aieb.memory.session import TITLE_MAX_LENGTH, TITLE_PREVIEW_LENGTH, derive_title
from aieb.remote import RemoteServer


# ── derive_title ────────────────────────────────────────────


def test_long_question_is_shortened() -> None:
    # 问号在预览长度之内 → 截到问号为止，标题很短且不需要省略号
    title = derive_title("上一次供应商评审会的时间、参与人和主要决策是什么？")
    assert title == "上一次供应商评审会的时间、参与人和主要决策是什么"
    assert len(title) <= TITLE_PREVIEW_LENGTH + 1


def test_question_without_punctuation_gets_ellipsis() -> None:
    title = derive_title("请帮我统计一下这三个月所有供应商的到货准时率和来料不良率的变化趋势")
    assert title.endswith("…")
    assert len(title) == TITLE_PREVIEW_LENGTH + 1


def test_short_question_kept_as_is() -> None:
    assert derive_title("用一句话说明你能帮我做什么") == "用一句话说明你能帮我做什么"


def test_sentence_boundary_is_used() -> None:
    # 第一个句号在预览长度之内 → 截到句号，不加省略号
    assert derive_title("帮我做一份汇报。要 8 页，面向管理层。") == "帮我做一份汇报"


def test_markdown_and_emoji_are_stripped() -> None:
    assert derive_title("## **重点** 结论") == "重点 结论"
    assert derive_title("🎯 上线计划") == "上线计划"


def test_only_first_line_is_used() -> None:
    assert derive_title("第一行问题\n第二行补充") == "第一行问题"


def test_never_exceeds_storage_limit() -> None:
    title = derive_title("啊" * 200)
    assert len(title) <= TITLE_MAX_LENGTH


def test_empty_input_gives_empty_title() -> None:
    assert derive_title("   ") == ""


# ── _build_session_list ─────────────────────────────────────


def _meta(sid: str, *, count: int, title: str = "") -> SimpleNamespace:
    now = datetime.now(timezone.utc)
    return SimpleNamespace(
        id=sid, title=title, summary="", message_count=count,
        created_at=now, last_active=now,
    )


def _server(metas: list, current: str) -> RemoteServer:
    srv = RemoteServer.__new__(RemoteServer)          # 只测纯函数，不初始化服务
    srv.session_manager = SimpleNamespace(list=lambda: metas)  # type: ignore[assignment]
    srv.session_id = current                          # type: ignore[attr-defined]
    return srv


def test_empty_sessions_are_filtered() -> None:
    srv = _server([_meta("s1", count=4, title="有内容"), _meta("s2", count=0)], "s1")
    ids = [item["id"] for item in srv._build_session_list()]
    assert ids == ["s1"]


def test_current_empty_session_is_kept() -> None:
    # 刚点“新建对话”时当前会话是空的，不能从列表里消失，否则用户以为会话没了
    srv = _server([_meta("s1", count=4, title="有内容"), _meta("s2", count=0)], "s2")
    ids = [item["id"] for item in srv._build_session_list()]
    assert ids == ["s1", "s2"]


def test_session_list_without_manager_is_empty() -> None:
    srv = RemoteServer.__new__(RemoteServer)
    srv.session_manager = None  # type: ignore[assignment]
    assert srv._build_session_list() == []
