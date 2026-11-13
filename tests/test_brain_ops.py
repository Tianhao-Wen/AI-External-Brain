"""WebSocket 业务操作（ops）与 Agent 工具的集成测试（不访问网络）。"""

from __future__ import annotations

import base64
from pathlib import Path

import pytest

from aieb.brain import llm, meetings
from aieb.brain.ops import BrainOps, BrainOpsError
from aieb.brain.service import BrainService
from aieb.brain.tools import (
    CreatePresentation,
    CreatePresentationParams,
    ListOpenTodos,
    ListOpenTodosParams,
    SearchKnowledge,
    SearchKnowledgeParams,
)
from aieb.config import ProviderConfig

SAMPLE_PDF = Path(__file__).resolve().parents[1] / "samples" / "产品需求说明书.pdf"


def _provider() -> ProviderConfig:
    return ProviderConfig(name="t", protocol="openai-compat", base_url="http://x", model="m",
                          api_key="k")


@pytest.fixture()
def service(tmp_path: Path) -> BrainService:
    svc = BrainService(work_dir=tmp_path, provider=_provider())
    yield svc
    svc.store.close()


@pytest.fixture()
def ops(service: BrainService) -> BrainOps:
    return BrainOps(service)


@pytest.mark.asyncio
async def test_upload_list_query_delete(ops: BrainOps, service: BrainService,
                                        monkeypatch: pytest.MonkeyPatch) -> None:
    data = base64.b64encode(SAMPLE_PDF.read_bytes()).decode("ascii")
    res = await ops.dispatch("documents.upload", {"name": "产品需求说明书.pdf", "data": data})
    doc = res["document"]
    assert doc["status"] == "ready"
    assert "stored_path" not in doc

    listed = await ops.dispatch("documents.list", {})
    assert len(listed["documents"]) == 1

    async def fake_complete(*args, **kwargs):
        return "验收条款 C 要求给出页码引用 [来源：产品需求说明书 · 第2页]"

    monkeypatch.setattr(llm, "complete", fake_complete)
    answer = await ops.dispatch("knowledge.query",
                                {"question": "验收条款 C 有什么要求？", "doc_ids": [doc["id"]]})
    assert "页码引用" in answer["answer"]
    assert answer["citations"][0]["page"] == 2

    await ops.dispatch("documents.delete", {"id": doc["id"]})
    assert (await ops.dispatch("documents.list", {}))["documents"] == []
    assert service.store.chunks_of(doc["id"]) == []


@pytest.mark.asyncio
async def test_activities_delete_and_clear(ops: BrainOps, service: BrainService) -> None:
    service.store.add_activity("document", "上传知识文档：a.pdf", "1 页")
    service.store.add_activity("document", "上传知识文档：b.pdf", "1 页")

    listed = await ops.dispatch("activities.list", {})
    assert len(listed["activities"]) == 2

    await ops.dispatch("activities.delete", {"id": listed["activities"][0]["id"]})
    assert len((await ops.dispatch("activities.list", {}))["activities"]) == 1

    cleared = await ops.dispatch("activities.clear", {})
    assert cleared["deleted"] == 1
    assert (await ops.dispatch("activities.list", {}))["activities"] == []

    with pytest.raises(BrainOpsError):
        await ops.dispatch("activities.delete", {"id": "act_not_exist"})


@pytest.mark.asyncio
async def test_upload_rejects_bad_payload(ops: BrainOps) -> None:
    with pytest.raises(BrainOpsError):
        await ops.dispatch("documents.upload", {"name": "x.pdf", "data": "not-base64!!"})


@pytest.mark.asyncio
async def test_upload_filename_cannot_escape_upload_dir(ops: BrainOps, service: BrainService) -> None:
    """路径穿越型文件名必须被规整到上传目录内部。"""
    from aieb.brain import paths

    data = base64.b64encode(b"project timeline: 2026-12-01 delivery").decode("ascii")
    res = await ops.dispatch(
        "documents.upload", {"name": "../../../evil.txt", "data": data}
    )
    doc = res["document"]
    assert doc["name"] == "evil.txt", "只保留 basename，不能带目录"

    stored = Path(service.store.get_document(doc["id"])["stored_path"]).resolve()
    upload_root = paths.uploads_dir(service.work_dir).resolve()
    assert upload_root in stored.parents, f"上传副本必须落在 {upload_root} 内，实际 {stored}"
    assert stored.is_file()


@pytest.mark.asyncio
async def test_artifact_download_ids_are_not_paths(ops: BrainOps, service: BrainService) -> None:
    """删除文档时只能按数据库里的文档 ID 操作，不接受路径参数。"""
    with pytest.raises(BrainOpsError):
        await ops.dispatch("documents.delete", {"id": "../../.aieb/brain/brain.sqlite3"})
    assert service.store.get_document("../../.aieb/brain/brain.sqlite3") is None


@pytest.mark.asyncio
async def test_meeting_analyze_and_email_flow(ops: BrainOps, monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {
        "title": "供应商评审", "meeting_time": "2025 年 10 月 28 日", "location": "",
        "participants": ["刘洋"], "discussion": [], "decisions": [],
        "summary": "质量风险",
        "risks": [{"title": "质量风险", "level": "high", "description": "不合格率 4.7%",
                   "evidence": "不合格率达到 4.7%", "suggestion": "整改"}],
        "todos": [{"content": "输出整改清单", "owner": "刘洋", "due_date": "11 月 5 日",
                   "priority": "high"}],
    }

    async def fake_json(*args, **kwargs):
        return payload

    monkeypatch.setattr(llm, "complete_json", fake_json)
    text = (Path(__file__).resolve().parents[1] / "samples" / "会议记录_有风险.txt").read_text(
        encoding="utf-8"
    )
    res = await ops.dispatch("meetings.analyze", {"name": "评审.txt", "text": text})
    meeting = res["meeting"]
    assert len(meeting["risks"]) == 1 and len(meeting["todos"]) == 1

    draft = await ops.dispatch("email.draft", {"meeting_id": meeting["id"], "to": "a@b.com"})
    assert draft["email"]["status"] == "draft"

    with pytest.raises(BrainOpsError):
        await ops.dispatch("email.send", {"id": draft["email"]["id"]})  # 没有 confirm

    # 未配置 SMTP 时必须明确失败，而不是假装发送成功
    with pytest.raises(BrainOpsError) as exc:
        await ops.dispatch("email.send", {"id": draft["email"]["id"], "confirm": True})
    assert "SMTP" in str(exc.value)

    todos = await ops.dispatch("todos.list", {"status": "all"})
    assert len(todos["todos"]) == 1
    todo_id = todos["todos"][0]["id"]
    updated = await ops.dispatch("todos.update", {"id": todo_id, "status": "done"})
    assert updated["todo"]["status"] == "done"


@pytest.mark.asyncio
async def test_meeting_rejects_short_text(ops: BrainOps) -> None:
    with pytest.raises(BrainOpsError):
        await ops.dispatch("meetings.analyze", {"name": "x.txt", "text": "太短"})


def _meeting_payload() -> dict:
    return {
        "title": "供应商评审", "meeting_time": "2025 年 10 月 28 日", "location": "1508",
        "participants": ["刘洋"], "discussion": [], "decisions": ["暂停新增订单"],
        "summary": "质量风险",
        "risks": [{"title": "质量风险", "level": "high", "description": "不合格率 4.7%",
                   "evidence": "给予 30 天整改期", "suggestion": "整改"}],
        "todos": [{"content": "输出整改清单", "owner": "刘洋", "due_date": "11 月 5 日",
                   "priority": "high"}],
    }


def _make_docx(paragraphs: list[str]) -> bytes:
    """构造一个最小的 .docx（zip + word/document.xml），不依赖 python-docx。"""
    import io
    import zipfile
    from xml.sax.saxutils import escape

    body = "".join(
        f"<w:p><w:r><w:t>{escape(p)}</w:t></w:r></w:p>" for p in paragraphs
    )
    xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/'
        'wordprocessingml/2006/main"><w:body>' + body + "</w:body></w:document>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("word/document.xml", xml)
    return buf.getvalue()


@pytest.mark.asyncio
async def test_meeting_analyze_accepts_docx(ops: BrainOps, monkeypatch: pytest.MonkeyPatch) -> None:
    """会议文件不再只收 TXT：DOCX 也要能解析并分析。"""

    async def fake_json(*args, **kwargs):
        return _meeting_payload()

    monkeypatch.setattr(llm, "complete_json", fake_json)
    docx = _make_docx([
        "2025 年 10 月 28 日 华瑞精密质量风险专题会",
        "会议决定暂停新增订单，并给予 30 天整改期。",
        "待办：输出整改清单，负责人刘洋，截止 11 月 5 日。",
    ])
    res = await ops.dispatch(
        "meetings.analyze",
        {"name": "华瑞精密会议.docx", "data": base64.b64encode(docx).decode("ascii")},
    )
    assert res["meeting"]["title"] == "供应商评审"
    assert len(res["meeting"]["risks"]) == 1


@pytest.mark.asyncio
async def test_meeting_analyze_accepts_pdf(ops: BrainOps, monkeypatch: pytest.MonkeyPatch) -> None:
    import pymupdf

    async def fake_json(*args, **kwargs):
        return _meeting_payload()

    monkeypatch.setattr(llm, "complete_json", fake_json)
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_textbox(
        pymupdf.Rect(72, 72, 520, 700),
        "2025 年 10 月 28 日 华瑞精密质量风险专题会\n会议决定暂停新增订单，给予 30 天整改期。\n"
        "待办：输出整改清单，负责人刘洋，截止 11 月 5 日。",
        fontsize=12, fontname="china-s",
    )
    pdf = doc.tobytes()
    doc.close()

    res = await ops.dispatch(
        "meetings.analyze",
        {"name": "会议纪要.pdf", "data": base64.b64encode(pdf).decode("ascii")},
    )
    assert res["meeting"]["title"] == "供应商评审"


@pytest.mark.asyncio
async def test_meeting_analyze_rejects_unsupported_format(ops: BrainOps) -> None:
    with pytest.raises(BrainOpsError) as exc:
        await ops.dispatch(
            "meetings.analyze",
            {"name": "会议.doc", "data": base64.b64encode(b"legacy word").decode("ascii")},
        )
    assert "DOCX" in str(exc.value) or "支持" in str(exc.value)


@pytest.mark.asyncio
async def test_memory_ops(ops: BrainOps, service: BrainService) -> None:
    service.store.add_memory_facts(
        [{"category": "person", "content": "刘洋参加了供应商评审", "source_label": "会议「供应商评审」"}]
    )
    listing = await ops.dispatch("memory.list", {})
    assert any("刘洋" in m["content"] for m in listing["memories"])
    target = listing["memories"][0]["id"]
    await ops.dispatch("memory.delete", {"id": target})
    assert all(m["id"] != target for m in (await ops.dispatch("memory.list", {}))["memories"])


@pytest.mark.asyncio
async def test_memory_delete_batch(ops: BrainOps, service: BrainService) -> None:
    """批量删除记忆：数据库记录一起删掉，不是只从列表里隐藏。"""
    service.store.add_memory_facts([
        {"category": "decision", "content": "决策一", "source_id": "m1", "source_label": "会议一"},
        {"category": "decision", "content": "决策二", "source_id": "m1", "source_label": "会议一"},
        {"category": "decision", "content": "决策三", "source_id": "m1", "source_label": "会议一"},
    ])
    items = (await ops.dispatch("memory.list", {}))["memories"]
    assert len(items) == 3
    target = [m["id"] for m in items][:2]

    res = await ops.dispatch("memory.delete", {"ids": target})
    assert res["deleted"] == 2
    assert res["failed"] == []
    left = (await ops.dispatch("memory.list", {}))["memories"]
    assert len(left) == 1
    assert service.store.list_memory_facts()[0]["content"] == left[0]["content"]

    with pytest.raises(BrainOpsError):
        await ops.dispatch("memory.delete", {"ids": ["fact_not_exist"]})


@pytest.mark.asyncio
async def test_todos_delete_batch(ops: BrainOps) -> None:
    created = []
    for text in ("甲事项", "乙事项", "丙事项"):
        created.append((await ops.dispatch("todos.create", {"content": text}))["todo"]["id"])

    res = await ops.dispatch("todos.delete", {"ids": created[:2]})
    assert res["deleted"] == 2
    left = (await ops.dispatch("todos.list", {}))["todos"]
    assert [t["id"] for t in left] == [created[2]]

    # 单条删除也走同一条路径
    assert (await ops.dispatch("todos.delete", {"id": created[2]}))["deleted"] == 1
    assert (await ops.dispatch("todos.list", {}))["todos"] == []

    with pytest.raises(BrainOpsError):
        await ops.dispatch("todos.delete", {"ids": ["todo_not_exist"]})


@pytest.mark.asyncio
async def test_unknown_op_rejected(ops: BrainOps) -> None:
    with pytest.raises(BrainOpsError):
        await ops.dispatch("nope.nothing", {})


@pytest.mark.asyncio
async def test_tool_search_knowledge_uses_service(service: BrainService,
                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_answer(question: str, top_k: int = 5):
        return {"answer": "答案", "citations": [{"doc_name": "a.pdf", "page": 2}], "grounded": True}

    monkeypatch.setattr(service, "answer", fake_answer)
    tool = SearchKnowledge(service)
    result = await tool.execute(SearchKnowledgeParams(query="验收标准"))
    assert "答案" in result.output
    assert "a.pdf 第2页" in result.output


@pytest.mark.asyncio
async def test_tool_list_todos(service: BrainService) -> None:
    service.store.add_todo(content="输出整改清单", owner="刘洋", due_date="11 月 5 日",
                           priority="high")
    tool = ListOpenTodos(service)
    result = await tool.execute(ListOpenTodosParams())
    assert "输出整改清单" in result.output
    assert "刘洋" in result.output


@pytest.mark.asyncio
async def test_tool_create_presentation_copies_to_workspace(service: BrainService, tmp_path: Path,
                                                            monkeypatch: pytest.MonkeyPatch) -> None:
    src = tmp_path / ".aieb" / "brain" / "exports" / "deck.pptx"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(b"x" * 6000)

    async def fake_make_ppt(**kwargs):
        return {"filename": "deck.pptx", "slide_count": 6, "outline": {}, "_path": str(src)}

    monkeypatch.setattr(service, "make_ppt", fake_make_ppt)
    tool = CreatePresentation(service, work_dir=str(tmp_path))
    result = await tool.execute(CreatePresentationParams(topic="季度复盘"))
    assert not result.is_error
    assert (tmp_path / "汇报材料" / "deck.pptx").is_file()


# ---------------------------------------------------------------------------
# 跨会话记忆召回（新建对话也能答出上次会议）
# ---------------------------------------------------------------------------


def _seed_two_meetings(service: BrainService) -> None:
    service.store.add_memory_facts([
        {"category": "person", "content": "刘洋参加了供应商评审会",
         "source_kind": "meeting", "source_id": "m1", "source_label": "会议「供应商评审会」"},
        {"category": "decision", "content": "决策：暂停涨价审批",
         "source_kind": "meeting", "source_id": "m1", "source_label": "会议「供应商评审会」"},
        {"category": "time", "content": "迭代评审会时间：2025 年 11 月 3 日 14:00",
         "source_kind": "meeting", "source_id": "m2", "source_label": "会议「迭代评审会」"},
    ])


def test_recall_finds_relevant_facts_with_source(service: BrainService) -> None:
    _seed_two_meetings(service)
    hits = service.recall("上次供应商会议讲了什么", limit=5)
    assert hits, "应能召回供应商相关记忆"
    assert all("供应商" in h["content"] or "供应商" in h["source"] for h in hits)
    assert all(h["source"] for h in hits), "每条记忆都必须带来源"


def test_recall_returns_recent_when_query_empty(service: BrainService) -> None:
    _seed_two_meetings(service)
    assert len(service.recall("", limit=2)) == 2


def test_recall_does_not_invent_when_nothing_matches(service: BrainService) -> None:
    _seed_two_meetings(service)
    assert service.recall("火星殖民计划预算", limit=5) == []


@pytest.mark.asyncio
async def test_recall_memory_tool_output(service: BrainService) -> None:
    from aieb.brain.tools import RecallMemory, RecallMemoryParams

    _seed_two_meetings(service)
    tool = RecallMemory(service)
    result = await tool.execute(RecallMemoryParams(query="供应商"))
    assert not result.is_error
    assert "刘洋" in result.output
    assert "会议「供应商评审会」" in result.output

    empty = await tool.execute(RecallMemoryParams(query="火星殖民计划预算"))
    assert "没有找到" in empty.output
    assert "不要编造" in empty.output


# ---------------------------------------------------------------------------
# 已上传会议记录的读取（避免"我明明上传了，AI 却说没收到"）
# ---------------------------------------------------------------------------


def _seed_meeting(service: BrainService) -> str:
    meeting = service.store.add_meeting(
        title="A 类供应商华瑞精密（A-07）质量风险专题会",
        meeting_time="2025 年 10 月 28 日 09:30—11:20",
        location="总部 A 座 1508 会议室",
        participants=["陈明", "赵磊"],
        summary="连续两批来料不合格，暂停新增订单并限期整改",
        decisions=["自 2025 年 10 月 28 日起暂停新增订单"],
        source_name="03 10月28日_华瑞精密质量风险会议.txt",
        raw_text="2025 年 10 月 28 日，华瑞精密 A-07 质量风险专题会。"
                 "陈明：连续两批来料不合格，暂停新增订单，30 天整改。",
        risks=[{
            "title": "供应中断风险", "level": "high",
            "description": "现有库存仅可支撑到 11 月 6 日",
            "evidence": "现有库存仅可支撑到 11 月 6 日",
            "suggestion": "加快整改验证",
        }],
        todos=[{"content": "提交保节点与延期两套方案", "owner": "林舟",
                "due_date": "11 月 6 日", "priority": "high"}],
    )
    return meeting["id"]


@pytest.mark.asyncio
async def test_tool_list_meetings_finds_uploaded_meeting(service: BrainService) -> None:
    from aieb.brain.tools import ListMeetings, ListMeetingsParams

    _seed_meeting(service)
    tool = ListMeetings(service)
    result = await tool.execute(ListMeetingsParams(query="华瑞精密 2025-10-28"))
    assert not result.is_error
    assert "华瑞精密" in result.output
    assert "风险 1 条" in result.output

    empty = await tool.execute(ListMeetingsParams(query="火星殖民计划"))
    assert "没有" in empty.output


@pytest.mark.asyncio
async def test_tool_get_meeting_returns_details_and_raw_text(service: BrainService) -> None:
    from aieb.brain.tools import GetMeeting, GetMeetingParams

    meeting_id = _seed_meeting(service)
    tool = GetMeeting(service)

    by_id = await tool.execute(GetMeetingParams(meeting_id=meeting_id))
    assert not by_id.is_error
    assert "供应中断风险" in by_id.output
    assert "林舟" in by_id.output
    assert "现有库存仅可支撑到 11 月 6 日" in by_id.output
    assert "会议记录原文片段" in by_id.output

    by_query = await tool.execute(GetMeetingParams(query="华瑞精密"))
    assert by_id.output.splitlines()[0] in by_query.output

    missing = await tool.execute(GetMeetingParams(query="火星殖民计划"))
    assert "没有找到" in missing.output

