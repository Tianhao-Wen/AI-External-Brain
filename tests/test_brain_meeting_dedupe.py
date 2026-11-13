"""会议重复上传的复用逻辑 + 记忆搜索无关（前端）在后端侧的可测部分。

背景：同一份会议记录被重复上传时，以前会生成两场会议、两套风险/待办/记忆，
长期记忆页就出现"同一条决策两遍"。现在按原文（归一化后）复用已有会议。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aieb.brain import llm, meetings
from aieb.brain.service import BrainService
from aieb.brain.store import BrainStore
from aieb.config import ProviderConfig

RAW = """会议主题：供应商年度评审
会议时间：2025 年 10 月 28 日 9:30
参会人员：陈明、刘洋
刘洋报告：第三季度来料不合格率达到 4.7%，高于合同约定的 2% 上限。
陈明：请刘洋在 11 月 5 日前输出质量整改要求清单。
"""

PAYLOAD = {
    "title": "供应商年度评审",
    "meeting_time": "2025 年 10 月 28 日",
    "location": "",
    "participants": ["陈明", "刘洋"],
    "discussion": ["来料不合格率 4.7%"],
    "decisions": ["要求供应商提交整改计划"],
    "summary": "供应商质量存在明显风险",
    "risks": [{"title": "质量不合格", "level": "高", "description": "不合格率 4.7%",
               "evidence": "不合格率 4.7%", "suggestion": "限期整改"}],
    "todos": [{"content": "输出质量整改要求清单", "owner": "刘洋",
               "due_date": "11 月 5 日", "priority": "高"}],
}


def _provider() -> ProviderConfig:
    return ProviderConfig(name="t", protocol="openai-compat", base_url="http://x",
                          model="m", api_key="k")


@pytest.fixture()
def store(tmp_path: Path) -> BrainStore:
    s = BrainStore(tmp_path)
    yield s
    s.close()


def _service(store: BrainStore) -> BrainService:
    """只用到 store + provider 的轻量 BrainService（不碰网络与记忆目录）。"""
    svc = BrainService.__new__(BrainService)
    svc.store = store          # type: ignore[attr-defined]
    svc.provider = _provider()  # type: ignore[attr-defined]
    return svc


def test_same_text_is_reused_not_analyzed_twice(store: BrainStore, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    async def fake_complete_json(*args, **kwargs):
        calls["n"] += 1
        return PAYLOAD

    monkeypatch.setattr(llm, "complete_json", fake_complete_json)
    svc = _service(store)

    first = _run(svc.analyze_meeting("评审.txt", RAW))
    assert calls["n"] == 1
    assert not first.get("reused")

    # 同样的内容（只多了一个空行）再提交一次：必须复用
    second = _run(svc.analyze_meeting("评审.txt", RAW + "\n\n"))
    assert second.get("reused") is True
    assert second["id"] == first["id"]
    assert calls["n"] == 1, "重复上传不应该再调用一次模型"

    assert len(store.list_meetings()) == 1
    facts = store.list_memory_facts()
    assert len(facts) == len({(f["category"], f["content"]) for f in facts}), "不应产生重复记忆"


def test_different_text_is_analyzed_separately(store: BrainStore, monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_complete_json(*args, **kwargs):
        return PAYLOAD

    monkeypatch.setattr(llm, "complete_json", fake_complete_json)
    svc = _service(store)

    _run(svc.analyze_meeting("a.txt", RAW))
    other = _run(svc.analyze_meeting("b.txt", RAW + "\n补充：下周复盘。\n"))
    assert not other.get("reused")
    assert len(store.list_meetings()) == 2


def test_find_meeting_by_text_ignores_whitespace(store: BrainStore) -> None:
    cleaned = meetings.sanitize(meetings.MeetingAnalysis.model_validate(PAYLOAD), RAW)
    saved = meetings.save(store, cleaned, RAW, "评审.txt")

    assert store.find_meeting_by_text("  " + RAW.replace("\n", "  \n") + "  ")["id"] == saved["id"]
    assert store.find_meeting_by_text("完全不同的一份会议记录内容") is None


def _run(coro):
    import asyncio

    return asyncio.run(coro)
