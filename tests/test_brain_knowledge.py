"""知识库：PDF 解析、分块、BM25 检索与引用页码。"""

from __future__ import annotations

from pathlib import Path

import pytest

from aieb.brain import knowledge
from aieb.brain.store import BrainStore

SAMPLE_PDF = Path(__file__).resolve().parents[1] / "samples" / "产品需求说明书.pdf"


@pytest.fixture()
def store(tmp_path: Path) -> BrainStore:
    s = BrainStore(tmp_path)
    yield s
    s.close()


def _pdf_bytes(pages: list[str]) -> bytes:
    try:
        import pymupdf as fitz
    except ImportError:  # pragma: no cover - 老版本 PyMuPDF
        import fitz

    doc = fitz.open()
    for text in pages:
        page = doc.new_page()
        if text:
            page.insert_textbox(fitz.Rect(72, 72, 520, 700), text, fontsize=12, fontname="china-s")
    data = doc.tobytes()
    doc.close()
    return data


def test_ingest_sample_pdf_and_search_by_page(store: BrainStore, tmp_path: Path) -> None:
    data = SAMPLE_PDF.read_bytes()
    doc = knowledge.ingest(store, filename="产品需求说明书.pdf", data=data,
                           upload_dir=tmp_path / "uploads")
    assert doc["status"] == "ready", doc.get("error")
    assert doc["pages"] == 3
    assert doc["chars"] > 100

    index = knowledge.BM25Index(store)
    hits = index.search("验收条款 C 页码引用", top_k=3)
    assert hits, "应能检索到验收标准相关段落"
    assert hits[0]["doc_name"] == "产品需求说明书.pdf"
    assert hits[0]["page"] == 2, "验收标准在示例 PDF 的第 2 页"

    citations = knowledge.format_citations(hits)
    assert citations[0]["page"] == 2
    assert citations[0]["doc_name"] == "产品需求说明书.pdf"

    context = knowledge.build_context(hits)
    assert "第2页" in context


def test_search_scoped_to_single_document(store: BrainStore, tmp_path: Path) -> None:
    doc_a = knowledge.ingest(
        store, filename="a.txt", data="项目甲：交付时间为 12 月 20 日。".encode("utf-8"),
        upload_dir=tmp_path / "up",
    )
    doc_b = knowledge.ingest(
        store, filename="b.txt", data="项目乙：验收标准为响应时间 2 秒。".encode("utf-8"),
        upload_dir=tmp_path / "up",
    )
    index = knowledge.BM25Index(store)
    hits = index.search("验收标准", top_k=5, doc_ids={doc_b["id"]})
    assert hits and hits[0]["doc_id"] == doc_b["id"]
    assert all(h["doc_id"] != doc_a["id"] for h in hits)


def test_scanned_pdf_reports_no_text(store: BrainStore, tmp_path: Path) -> None:
    # 空白页 = 没有文字层，模拟扫描件
    data = _pdf_bytes(["", ""])
    doc = knowledge.ingest(store, filename="扫描件.pdf", data=data,
                           upload_dir=tmp_path / "up")
    assert doc["status"] == "no_text"
    assert "OCR" in (doc["error"] or "")
    assert store.all_chunks() == [], "无文字层时不应产生任何检索分块"


def test_corrupt_pdf_fails_cleanly(store: BrainStore, tmp_path: Path) -> None:
    doc = knowledge.ingest(store, filename="坏文件.pdf", data=b"%PDF-1.4 not really a pdf",
                           upload_dir=tmp_path / "up")
    assert doc["status"] == "failed"
    assert doc["error"]


def test_unsupported_extension(store: BrainStore, tmp_path: Path) -> None:
    """不支持的格式在落库前就被拒绝，不应污染文档列表。"""
    with pytest.raises(knowledge.DocumentParseError) as exc:
        knowledge.ingest(store, filename="图片.png", data=b"\x89PNG\r\n",
                         upload_dir=tmp_path / "up")
    assert "不支持" in str(exc.value)
    assert store.list_documents() == [], "被拒绝的上传不应留下文档记录"
    assert not (tmp_path / "up").exists() or not list((tmp_path / "up").iterdir())


def test_delete_document_removes_chunks_and_file(store: BrainStore, tmp_path: Path) -> None:
    doc = knowledge.ingest(store, filename="x.txt", data=b"hello world content for chunking",
                           upload_dir=tmp_path / "up")
    assert doc["status"] == "ready"
    stored = Path(store.get_document(doc["id"])["stored_path"])
    assert stored.is_file()

    assert knowledge.delete_document(store, doc["id"]) is True
    assert store.chunks_of(doc["id"]) == []
    assert not stored.exists(), "删除文档应同时清理上传副本"


def test_delete_document_cascades_its_activities(store: BrainStore, tmp_path: Path) -> None:
    """删除文档后，它的活动记录也应从数据库里清掉，不能残留在「最新活动」。"""
    doc = knowledge.ingest(
        store, filename="制度.txt",
        data="第一条 单价上调幅度大于 8% 时，须由 CEO 与财务总监联合审批。".encode(),
        upload_dir=tmp_path / "up",
    )
    assert doc["status"] == "ready"
    assert [a["title"] for a in store.list_activities()] == ["上传知识文档：制度.txt"]

    assert knowledge.delete_document(store, doc["id"]) is True
    assert store.list_activities() == [], "文档已删除，不应留下“上传知识文档”的活动记录"


def test_delete_document_keeps_activities_while_same_name_remains(
    store: BrainStore, tmp_path: Path
) -> None:
    """同名文档还有一份没删时，活动记录不能提前清掉。"""
    first = knowledge.ingest(store, filename="同名.txt", data="甲内容".encode(),
                             upload_dir=tmp_path / "up")
    second = knowledge.ingest(store, filename="同名.txt", data="乙内容".encode(),
                              upload_dir=tmp_path / "up")

    knowledge.delete_document(store, first["id"])
    titles = [a["title"] for a in store.list_activities()]
    assert titles.count("上传知识文档：同名.txt") == 2, "还有同名文档时应保留活动记录"

    knowledge.delete_document(store, second["id"])
    assert store.list_activities() == [], "同名文档全部删完后应清空活动记录"


def test_chunk_pages_keeps_page_numbers() -> None:
    pages = [{"page": 1, "text": "甲" * 800}, {"page": 2, "text": "乙" * 50}]
    chunks = knowledge.chunk_pages(pages, target=300, overlap=50)
    assert {c["page"] for c in chunks} == {1, 2}
    assert all(len(c["text"]) <= 300 for c in chunks)
    # 800 字的页在 300/50 的窗口下最多约 4 块；不能因为重叠退化成逐字滑动
    assert len([c for c in chunks if c["page"] == 1]) <= 5
    total = sum(len(c["text"]) for c in chunks)
    assert total < 1200, f"分块总字数不应远大于原文（实际 {total}）"
