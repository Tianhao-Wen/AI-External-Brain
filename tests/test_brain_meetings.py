"""会议分析：结构化校验、防编造清洗、落库与记忆沉淀。"""

from __future__ import annotations

from pathlib import Path

import pytest

from aieb.brain import llm, meetings
from aieb.brain.store import BrainStore
from aieb.config import ProviderConfig

RAW = """会议主题：供应商年度评审
会议时间：2025 年 10 月 28 日 9:30
参会人员：陈明、刘洋
刘洋报告：第三季度来料不合格率达到 4.7%，高于合同约定的 2% 上限。
陈明：请刘洋在 11 月 5 日前输出质量整改要求清单。
"""


def _provider() -> ProviderConfig:
    return ProviderConfig(name="t", protocol="openai-compat", base_url="http://x", model="m",
                          api_key="k")


@pytest.fixture()
def store(tmp_path: Path) -> BrainStore:
    s = BrainStore(tmp_path)
    yield s
    s.close()


def test_sanitize_removes_fabricated_facts() -> None:
    raw_analysis = meetings.MeetingAnalysis(
        title="供应商年度评审",
        meeting_time="2025 年 10 月 28 日",
        location="总部的 8 楼会议室",       # 原文没有 -> 必须清空
        participants=["陈明", "刘洋", "张三"],  # 张三不存在 -> 移除
        discussion=["质量不达标"],
        decisions=[],
        summary="质量风险突出",
        risks=[
            meetings.RiskIn(title="质量风险", level="high", description="不合格率超标",
                            evidence="不合格率达到 4.7%", suggestion="要求整改"),
            meetings.RiskIn(title="编造风险", level="medium", description="描述",
                            evidence="原文里没有这句话", suggestion=""),
        ],
        todos=[
            meetings.TodoIn(content="输出整改清单", owner="刘洋", due_date="11 月 5 日",
                            priority="high"),
            meetings.TodoIn(content="给出赔付方案", owner="王五", due_date="12 月 31 日"),
        ],
    )
    cleaned = meetings.sanitize(raw_analysis, RAW)

    assert cleaned.location == "", "原文没有的地点必须清空"
    assert cleaned.participants == ["陈明", "刘洋"], "原文没有的人名必须移除"
    # 行为收紧：依据在原文中找不到的风险**整条丢弃**，而不是保留一条"无依据的风险"。
    # 无依据的风险正是误报的主要来源，因此这里断言它不出现在结果里。
    assert len(cleaned.risks) == 1, "无原文依据的风险必须整条丢弃"
    assert cleaned.risks[0].evidence == "不合格率达到 4.7%"
    assert [r.title for r in cleaned.risks] == ["质量风险"]
    todo_ok, todo_bad = cleaned.todos
    assert (todo_ok.owner, todo_ok.due_date) == ("刘洋", "11 月 5 日")
    assert todo_bad.owner == "", "原文没有的负责人不能保留"
    assert todo_bad.due_date == "", "原文没有的截止时间不能保留"


def test_analyze_is_structured_and_saved(store: BrainStore, monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {
        "title": "供应商年度评审",
        "meeting_time": "2025 年 10 月 28 日",
        "location": "",
        "participants": ["陈明", "刘洋"],
        "discussion": ["来料不合格率 4.7%"],
        "decisions": ["要求供应商提交整改计划"],
        "summary": "供应商质量存在明显风险",
        "risks": [{"title": "质量不合格", "level": "高", "description": "不合格率 4.7%",
                   "evidence": "不合格率达到 4.7%", "suggestion": "限期整改"}],
        "todos": [{"content": "输出质量整改要求清单", "owner": "刘洋",
                   "due_date": "11 月 5 日", "priority": "高"}],
    }

    async def fake_complete_json(*args, **kwargs):
        return payload

    monkeypatch.setattr(llm, "complete_json", fake_complete_json)

    analysis = meetings.analyze(_provider(), RAW, source_name="供应商评审.txt")
    assert analysis.title == "供应商年度评审"
    assert analysis.risks[0].level == "high", "中文风险等级必须归一化"
    assert analysis.todos[0].priority == "high"

    cleaned = meetings.sanitize(analysis, RAW)
    meeting = meetings.save(store, cleaned, RAW, "供应商评审.txt")
    assert meeting["risks"][0]["evidence"] == "不合格率达到 4.7%"
    assert meeting["todos"][0]["owner"] == "刘洋"

    facts = store.list_memory_facts()
    categories = {f["category"] for f in facts}
    assert {"person", "topic", "todo", "decision"} <= categories
    assert all(f["source_id"] == meeting["id"] for f in facts)


def test_analysis_rejects_short_input() -> None:
    with pytest.raises(meetings.MeetingAnalysisError):
        meetings.analyze(_provider(), "太短了")


def test_analysis_raises_when_model_returns_garbage(monkeypatch: pytest.MonkeyPatch) -> None:
    async def bad(*args, **kwargs):
        raise llm.LLMError("模型返回的结构化结果不是合法 JSON")

    monkeypatch.setattr(llm, "complete_json", bad)
    with pytest.raises(meetings.MeetingAnalysisError):
        meetings.analyze(_provider(), RAW)


def test_email_fallback_template_is_grounded(monkeypatch: pytest.MonkeyPatch) -> None:
    async def bad(*args, **kwargs):
        raise llm.LLMError("offline")

    monkeypatch.setattr(llm, "complete_json", bad)
    import asyncio

    meeting = {
        "title": "供应商年度评审", "meeting_time": "", "location": "", "participants": [],
        "summary": "",
        "risks": [{"title": "质量不合格", "level": "high", "description": "不合格率 4.7%"}],
        "todos": [{"content": "输出整改清单", "owner": "", "due_date": "", "priority": "high"}],
    }
    draft = asyncio.run(meetings.draft_email(_provider(), meeting, to_addr="a@b.com"))
    assert draft["subject"]
    assert "待确认" in draft["body"], "缺失信息必须标注待确认"
    assert "4.7%" in draft["body"]
