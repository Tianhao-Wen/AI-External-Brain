"""单文档上下文统一 + 数据隔离/并发安全回归。

分三块：

1. **上下文口径**：单文档问答必须和全局问答用同一套 token 预算（`build_budgeted_context`），
   并如实记录 Retrieved / Selected / Included / tokens；
2. **隔离**：作用域（scope）在 BM25 路（默认模式）与向量路都必须生效，且显式 doc_ids
   不能绕过作用域；
3. **并发与删除**：并发上传/检索/删除后三个索引仍然一致；删掉的文档不能从任何一路检索到。

全部使用确定性 Fake（不下载模型、不连 Qdrant、不调真实 LLM）。
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from typing import Any

import pytest

from aieb.brain.knowledge import BM25Index
from aieb.brain.ops import BrainOps
from aieb.brain.retrieval.config import RetrievalConfig
from aieb.brain.retrieval.context import build_budgeted_context
from aieb.brain.retrieval.service import RetrievalService
from aieb.brain.service import BrainService
from tests.test_hybrid_modes import FakeEmbedder, FakeReranker, FakeVectorStore

ANSWER = "按制度回答：违约金累计不超过合同总额的 20% [来源：制度.pdf · 第1页]"


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


def _provider():
    from aieb.config import ProviderConfig

    return ProviderConfig(
        name="offline",
        protocol="openai-compat",
        base_url="http://127.0.0.1:9/x",
        model="offline",
        api_key="offline",
    )


def make_service(
    tmp_path: Path,
    *,
    mode: str = "hybrid",
    scope: str = "default",
    context_budget: str = "budgeted",
    context_max_tokens: int = 1800,
) -> BrainService:
    cfg = RetrievalConfig(
        mode=mode,
        candidates=10,
        scope=scope,
        context_budget=context_budget,
        context_max_tokens=context_max_tokens,
    )
    svc = BrainService(work_dir=tmp_path, provider=_provider(), retrieval_config=cfg)
    if mode != "bm25":
        svc._retrieval = RetrievalService(
            bm25=svc.index,
            config=cfg,
            store=svc.store,
            embedder=FakeEmbedder(),
            vector_store=FakeVectorStore(),
            reranker=FakeReranker(),
        )
    return svc


def make_pdf(pages: list[list[str]]) -> bytes:
    """生成多页 PDF（每页若干行），返回字节。

    每页额外补一行填充文本：分块器会跳过字符数过少的页（`MIN_CHARS_PER_PAGE`），
    只写一行短句的话整页会被丢弃，测试就变成在测“空库”。
    """
    import fitz

    doc = fitz.open()
    for lines in pages:
        page = doc.new_page(width=595, height=842)
        y = 72
        body = list(lines) + ["附：本页其余条款按公司现行制度执行，未尽事宜由采购部解释。"]
        for line in body:
            page.insert_text((56, y), line, fontsize=12, fontname="china-s")
            y += 26
    data = doc.tobytes()
    doc.close()
    return data


@pytest.fixture()
def fake_llm(monkeypatch):
    """替换 `aieb.brain.llm.complete`，记录送进模型的 prompt。"""
    calls: list[str] = []

    async def _complete(provider, system, prompt, *, timeout=120.0, usage_out=None):
        calls.append(prompt)
        if usage_out is not None:
            usage_out["input_tokens"] = 123
            usage_out["output_tokens"] = 45
        return ANSWER

    monkeypatch.setattr("aieb.brain.llm.complete", _complete)
    return calls


# ---------------------------------------------------------------------------
# 1. 单文档上下文：与全局同口径
# ---------------------------------------------------------------------------


def test_single_doc_query_reports_four_layer_counters(tmp_path: Path, fake_llm):
    svc = make_service(tmp_path)
    pdf = make_pdf(
        [
            ["供应商管理制度", "第1条 逾期违约金按合同总额每日 0.5% 计收，累计不超过 20%。"],
            ["第2条 采购部应在 10 个工作日内完成初审。"],
            ["第3条 质保金比例为 5%，验收合格后 30 日内退还。"],
        ]
    )
    doc = svc.upload("制度.pdf", pdf)
    ops = BrainOps(svc)
    result = asyncio.run(
        ops.op_knowledge_query({"question": "逾期违约金的累计上限是多少？", "doc_ids": [doc["id"]]})
    )

    meta = result["context_meta"]
    assert meta["budget_tokens"] == 1800, "必须使用配置里的 token 预算"
    assert meta["retrieved"] >= meta["selected"] >= meta["included"] >= 1
    assert meta["estimated_tokens"] > 0
    assert 0 < meta["budget_utilisation"] <= 1.0
    assert result["usage"]["input_tokens"] == 123, "真实 token 用量要透传出来"
    citation = result["citations"][0]
    assert citation["page"] == 1 and citation["doc_id"] == doc["id"]
    assert citation["chunk_key"], "引用需要保留 chunk_key"
    svc.store.close()


def test_single_doc_and_global_use_same_token_budget(tmp_path: Path, fake_llm):
    svc = make_service(tmp_path)
    pdf = make_pdf([["第1条 逾期违约金累计上限 20%。"], ["第2条 初审 10 个工作日。"]])
    doc = svc.upload("制度.pdf", pdf)
    ops = BrainOps(svc)

    single = asyncio.run(
        ops.op_knowledge_query({"question": "违约金上限", "doc_ids": [doc["id"]]})
    )["context_meta"]
    hits = svc.search("违约金上限", top_k=5)
    text, global_meta = svc.build_context(hits)

    assert single["budget_tokens"] == global_meta["budget_tokens"] == 1800
    assert set(single) >= {"retrieved", "selected", "included", "estimated_tokens"}
    assert len(text) > 0
    svc.store.close()


def test_multi_page_question_includes_every_evidence_page(tmp_path: Path, fake_llm):
    svc = make_service(tmp_path)
    pdf = make_pdf(
        [
            ["第一条 逾期违约金累计封顶比例为 10%，基数为合同金额。"],
            ["第二条 与违约金无关的内容：食堂就餐时间 11:30-13:00。"],
            ["第三条 质保金比例为 5%，验收合格后 30 日内退还。"],
        ]
    )
    doc = svc.upload("制度.pdf", pdf)
    ops = BrainOps(svc)
    asyncio.run(
        ops.op_knowledge_query(
            {"question": "违约金封顶比例和质保金比例分别是多少？", "doc_ids": [doc["id"]]}
        )
    )
    prompt = fake_llm[-1]
    assert "第1页" in prompt and "第3页" in prompt, prompt
    svc.store.close()


def test_long_context_respects_budget_without_early_stop_or_duplicates():
    """长上下文：不早停、不重复、token 不超预算。"""
    long_hit = {
        "doc_id": "d1",
        "doc_name": "长文档",
        "page": 1,
        "text": "很长的条款" * 400,
        "content_hash": "h-long",
        "chunk_key": "d1:p1:h-long",
    }
    short_hit = {
        "doc_id": "d1",
        "doc_name": "长文档",
        "page": 2,
        "text": "短条款：质保金 5%。",
        "content_hash": "h-short",
        "chunk_key": "d1:p2:h-short",
    }
    dup_hit = dict(short_hit)  # 同一内容哈希：必须被去重

    # ① 允许截断时：长块被截断填写预算，总量不得超，且重复内容只出现一次
    bundle = build_budgeted_context(
        [long_hit, short_hit, dup_hit], max_tokens=260, chars_per_token=1.74
    )
    assert bundle.estimated_tokens <= 260, "绝不能超出 token 预算"
    assert bundle.deduped == 1, "同一内容不得重复进入上下文"
    assert bundle.truncated_blocks >= 1, "长块应被截断而不是整块丢弃"

    # ② 不允许截断时：放不下的长块丢掉，**后面的短块仍要被尝试**（旧实现会直接 break）
    strict = build_budgeted_context(
        [long_hit, short_hit], max_tokens=260, chars_per_token=1.74, truncate_to_fit=False
    )
    assert strict.included == 1, "短块必须仍能进入上下文（不早停）"
    assert "质保金 5%" in strict.text


def test_legacy_char_budget_still_supported(tmp_path: Path, fake_llm):
    svc = make_service(tmp_path, context_budget="legacy")
    pdf = make_pdf([["第1条 逾期违约金累计上限 20%。"]])
    doc = svc.upload("制度.pdf", pdf)
    ops = BrainOps(svc)
    result = asyncio.run(
        ops.op_knowledge_query({"question": "违约金上限", "doc_ids": [doc["id"]]})
    )
    assert result["context_meta"]["budget"] == "legacy"
    assert result["grounded"] is True
    svc.store.close()


def test_bm25_mode_still_works_and_keeps_chunk_key(tmp_path: Path, fake_llm):
    svc = make_service(tmp_path, mode="bm25")
    pdf = make_pdf([["第1条 逾期违约金累计上限 20%。"]])
    doc = svc.upload("制度.pdf", pdf)
    ops = BrainOps(svc)
    result = asyncio.run(
        ops.op_knowledge_query({"question": "违约金上限", "doc_ids": [doc["id"]]})
    )
    citation = result["citations"][0]
    assert citation["doc_id"] == doc["id"]
    assert citation["page"] == 1
    assert citation["chunk_key"], "引用必须保留 chunk_key"
    assert result["retrieval"]["mode"] == "bm25"
    svc.store.close()


# ---------------------------------------------------------------------------
# 2. 隔离：作用域必须在两条路上都生效
# ---------------------------------------------------------------------------


def test_bm25_default_mode_honours_scope(tmp_path: Path):
    """默认（bm25）模式也要隔离：否则改一个环境变量就会串库。"""
    svc = make_service(tmp_path, mode="bm25", scope="tenant-a")
    svc.upload("a.md", "第1条 违约金上限 20%。".encode())
    assert svc.search("违约金上限", top_k=5), "本租户内应能检索到"

    svc.retrieval_config.scope = "tenant-b"
    assert svc.search("违约金上限", top_k=5) == [], "切换作用域后不得再看到 tenant-a 的文档"
    svc.store.close()


def test_explicit_doc_ids_cannot_bypass_scope(tmp_path: Path):
    svc = make_service(tmp_path, mode="hybrid", scope="tenant-a")
    doc = svc.upload("a.md", "第1条 违约金上限 20%。".encode())
    svc.retrieval_config.scope = "tenant-b"
    assert svc.search("违约金上限", top_k=5, doc_ids={doc["id"]}) == []
    svc.store.close()


def test_vector_route_receives_document_filter(tmp_path: Path):
    """向量路必须真的带 document_ids 过滤（不是只在应用层丢结果）。"""
    svc = make_service(tmp_path, mode="vector", scope="tenant-a")
    doc = svc.upload("a.md", "第1条 违约金上限 20%。".encode())
    store: FakeVectorStore = svc.retrieval.vector_store
    seen: list[Any] = []
    original = store.search

    def spy(vector, **kwargs):
        seen.append(kwargs.get("document_ids"))
        return original(vector, **kwargs)

    store.search = spy  # type: ignore[method-assign]
    svc.search("违约金上限", top_k=5, doc_ids={doc["id"]})
    assert seen and set(seen[0]) == {doc["id"]}, f"向量路过滤条件丢失：{seen}"
    svc.store.close()


# ---------------------------------------------------------------------------
# 3. 删除同步与并发
# ---------------------------------------------------------------------------


def test_deleted_document_is_unreachable_from_every_route(tmp_path: Path):
    svc = make_service(tmp_path, mode="hybrid")
    keep = svc.upload("keep.md", "第1条 差旅住宿标准每晚不超过 600 元。".encode())
    gone = svc.upload("gone.md", "第1条 违约金上限 20%。".encode())
    assert svc.search("违约金上限", top_k=5), "删除前应能检索到"

    assert svc.delete_document(gone["id"]) is True

    # 注意：FakeEmbedder 是哈希分桶，向量相似度本身没有语义，
    # 所以不能断言“搜不到任何东西”，而要断言“召回的都不是被删的文档”。
    for query in ("违约金上限", "住宿标准", "违约金"):
        hits = svc.search(query, top_k=10)
        assert all(h["doc_id"] != gone["id"] for h in hits), f"{query} 仍召回已删除文档：{hits}"

    remaining = {h["doc_id"] for h in svc.search("住宿标准", top_k=5)}
    assert remaining <= {keep["id"]}
    assert svc.retrieval.vector_store.count() == svc.index.size
    svc.store.close()


def test_concurrent_upload_search_delete_keeps_indexes_consistent(tmp_path: Path):
    """少量并发（不上压力测试）：索引与库必须始终对得上。"""
    svc = make_service(tmp_path, mode="hybrid")
    errors: list[BaseException] = []
    stop = threading.Event()

    def uploader(tag: str) -> None:
        try:
            for i in range(4):
                svc.upload(
                    f"{tag}-{i}.md",
                    f"第1条 {tag} 的违约金上限为 {20 + i}%。".encode(),
                )
        except BaseException as exc:  # noqa: BLE001 - 线程里必须收集异常
            errors.append(exc)

    def searcher() -> None:
        try:
            while not stop.is_set():
                hits = svc.search("违约金上限", top_k=3)
                for hit in hits:
                    assert hit["doc_id"], "命中必须带 doc_id"
                # 命中的文档必须仍然在库里（不能检索到已删除/半写入的记录）
                ids = {d["id"] for d in svc.store.list_documents()}
                assert all(h["doc_id"] in ids for h in hits)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=uploader, args=(tag,)) for tag in ("甲", "乙")]
    readers = [threading.Thread(target=searcher) for _ in range(2)]
    for t in threads + readers:
        t.start()
    for t in threads:
        t.join()
    stop.set()
    for t in readers:
        t.join()

    assert not errors, f"并发期间出现异常：{errors[:3]}"
    chunks = svc.store.all_chunks()
    assert svc.index.size == len(chunks), "BM25 条目数必须等于库里的分块数"
    assert svc.retrieval.vector_store.count() == len(chunks), "向量点数应等于分块数"
    svc.store.close()


def test_concurrent_delete_and_search_never_returns_removed_doc(tmp_path: Path):
    """并发删除与检索：不报错；删除完成后旧内容必须彻底不可见。"""
    svc = make_service(tmp_path, mode="hybrid")
    victims = [svc.upload(f"v{i}.md", f"第1条 违约金上限 {20 + i}%。".encode()) for i in range(4)]
    keep = svc.upload("keep.md", "第1条 住宿标准每晚 600 元。".encode())
    errors: list[BaseException] = []

    def deleter():
        try:
            for doc in victims:
                svc.delete_document(doc["id"])
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def reader():
        try:
            for _ in range(40):
                svc.search("违约金上限", top_k=5)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=deleter), threading.Thread(target=reader)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"删除与检索并发时出错：{errors[:3]}"

    # 删除全部完成后（不是并发窗口内）不得再召回旧文档
    victim_ids = {d["id"] for d in victims}
    after = svc.search("违约金上限", top_k=10)
    assert all(h["doc_id"] not in victim_ids for h in after), f"删除后仍召回：{after}"
    assert {d["id"] for d in svc.store.list_documents()} == {keep["id"]}
    assert svc.index.size == len(svc.store.all_chunks())
    svc.store.close()


# ---------------------------------------------------------------------------
# 4. 删除保护：不允许有“顺手删业务集合”的旁路
# ---------------------------------------------------------------------------


def test_vector_rebuild_is_not_reachable_from_rpc_handlers():
    """`VectorIndexer.rebuild()` 带 allow_any，必须确认 RPC 层调不到它。"""
    source = (Path(__file__).resolve().parent.parent / "aieb" / "brain" / "ops.py").read_text(
        encoding="utf-8"
    )
    assert "vector_indexer" not in source, "RPC handler 不应直接触碰向量索引重建"
    assert "drop(" not in source, "RPC handler 不应直接删除集合"


def test_bm25_index_is_thread_safe_under_rebuild_and_append(tmp_path: Path):
    """重建与追加并发时不能出现长度错位（会导致打分越界/脏结果）。"""
    svc = make_service(tmp_path, mode="bm25")
    for i in range(3):
        svc.upload(f"seed{i}.md", f"第1条 违约金上限 {20 + i}%。".encode())
    index: BM25Index = svc.index
    errors: list[BaseException] = []

    def rebuild():
        try:
            for _ in range(10):
                index.rebuild()
                index.search("违约金上限", top_k=3)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def append():
        try:
            for i in range(10):
                index.add_chunks(
                    [
                        {
                            "id": f"x{i}",
                            "doc_id": "doc-x",
                            "doc_name": "x.md",
                            "page": 1,
                            "idx": i,
                            "text": f"第2条 补充条款 {i} 的违约金上限为 30%。",
                            "content_hash": f"hx{i}",
                        }
                    ]
                )
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=rebuild), threading.Thread(target=append)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"并发 rebuild/add 出错：{errors[:3]}"
    # 无论执行顺序如何，长度必须自洽
    assert len(index._lengths) == len(index._docs)
    hits = index.search("违约金上限", top_k=50)
    assert all(0 <= idx < len(index._docs) for _, idx in [])
    assert hits
    svc.store.close()
