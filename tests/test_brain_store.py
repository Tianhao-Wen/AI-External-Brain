"""AI 外脑 Demo —— 业务数据层（SQLite）单元测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from aieb.brain.store import BrainStore


@pytest.fixture()
def store(tmp_path: Path) -> BrainStore:
    s = BrainStore(tmp_path)
    yield s
    s.close()


def test_document_lifecycle(store: BrainStore) -> None:
    doc = store.add_document(
        name="需求说明.pdf", ext="pdf", size=1234, stored_path=str(store.path.parent / "x.pdf"),
        status="ready", pages=3, chars=500,
    )
    store.add_chunks(doc["id"], [{"page": 1, "idx": 0, "text": "验收条款 A"}])
    docs = store.list_documents()
    assert len(docs) == 1
    assert docs[0]["name"] == "需求说明.pdf"
    assert "stored_path" not in docs[0], "对外列表不应暴露本地路径"
    assert len(store.chunks_of(doc["id"])) == 1

    assert store.delete_document(doc["id"]) is True
    assert store.list_documents() == []
    assert store.chunks_of(doc["id"]) == [], "删除文档必须级联清理分块"


def test_meeting_with_risks_and_todos(store: BrainStore) -> None:
    meeting = store.add_meeting(
        title="供应商评审会",
        meeting_time="2025-10-28",
        location="线上",
        participants=["陈明", "刘洋"],
        summary="单一供应商风险",
        decisions=["暂停涨价审批"],
        risks=[{"title": "断供风险", "level": "high", "description": "占比 62%",
                "evidence": "占该品类采购额约 62%", "suggestion": "引入备选供应商"}],
        todos=[{"content": "输出整改清单", "owner": "刘洋", "due_date": "11月5日",
                "priority": "high"}],
    )
    assert meeting["id"].startswith("mtg_")
    assert len(meeting["risks"]) == 1
    assert meeting["risks"][0]["level"] == "high"
    assert len(meeting["todos"]) == 1

    stats = store.stats()
    assert stats["meetings"] == 1
    assert stats["todos_open"] == 1
    assert stats["risks_high"] == 1

    # 待办状态更新后统计随之变化
    todo = meeting["todos"][0]
    updated = store.update_todo(todo["id"], status="done")
    assert updated is not None and updated["status"] == "done"
    assert store.stats()["todos_open"] == 0
    assert store.stats()["todos_done"] == 1

    # 删除会议必须级联清理风险
    assert store.delete_meeting(meeting["id"]) is True
    assert store.list_risks() == []
    assert store.list_todos() == []


def test_memory_facts_and_activity(store: BrainStore) -> None:
    added = store.add_memory_facts([
        {"category": "person", "content": "陈明参加了供应商评审会",
         "source_kind": "meeting", "source_id": "mtg_1", "source_label": "会议「供应商评审会」"},
        {"category": "decision", "content": "决策：暂停涨价审批",
         "source_kind": "meeting", "source_id": "mtg_1", "source_label": "会议「供应商评审会」"},
        {"category": "todo", "content": "   "},  # 空白内容应被跳过
    ])
    assert added == 2
    facts = store.list_memory_facts()
    assert {f["category"] for f in facts} == {"person", "decision"}
    assert facts[0]["source_label"].startswith("会议")

    assert store.delete_memory_fact(facts[0]["id"]) is True
    assert len(store.list_memory_facts()) == 1

    store.add_activity("document", "上传知识文档：需求说明.pdf", "3 页")
    acts = store.list_activities()
    assert acts[0]["title"].startswith("上传知识文档")


def test_email_status_flow(store: BrainStore) -> None:
    mail = store.create_email(to_addr="a@b.com", subject="提醒", body="正文", kind="reminder")
    assert mail["status"] == "draft"
    sent = store.update_email(mail["id"], status="sent", sent_at="2025-11-01T00:00:00+00:00")
    assert sent is not None and sent["status"] == "sent"
    failed = store.update_email(mail["id"], status="failed", error="SMTP 认证失败")
    assert failed is not None and failed["error"]
    assert len(store.list_emails()) == 1


def test_memory_facts_are_deduplicated_per_source(store: BrainStore) -> None:
    """同一会议重复分析，不应让记忆页出现成倍的重复条目。"""
    fact = {"category": "decision", "content": "决策：暂停涨价审批",
            "source_kind": "meeting", "source_id": "mtg_dup",
            "source_label": "会议「供应商评审」"}
    assert store.add_memory_facts([fact, dict(fact)]) == 1
    assert store.add_memory_facts([dict(fact)]) == 0, "第二次写入应全部去重"
    assert len(store.list_memory_facts()) == 1

    # 不同来源的相同文字属于不同事实，必须都保留
    other = dict(fact, source_id="mtg_other")
    assert store.add_memory_facts([other]) == 1
    assert len(store.list_memory_facts()) == 2


def test_deleting_meeting_removes_its_memory_facts(store: BrainStore) -> None:
    """删除会议后不能留下指向不存在会议的“幽灵记忆”。"""
    meeting = store.add_meeting(title="待删除会议", participants=["刘洋"], summary="s")
    store.add_memory_facts([
        {"category": "person", "content": "刘洋参加待删除会议",
         "source_kind": "meeting", "source_id": meeting["id"],
         "source_label": "会议「待删除会议」"},
    ])
    kept = store.add_meeting(title="保留会议")
    store.add_memory_facts([
        {"category": "topic", "content": "保留会议主题",
         "source_kind": "meeting", "source_id": kept["id"],
         "source_label": "会议「保留会议」"},
    ])
    assert len(store.list_memory_facts()) == 2

    assert store.delete_meeting(meeting["id"]) is True
    remaining = store.list_memory_facts()
    assert [f["content"] for f in remaining] == ["保留会议主题"], "只应删除被删会议的记忆事实"
