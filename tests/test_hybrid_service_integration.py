"""业务接入测试：混合检索模式下的上传 / 问答 / 删除是否真的联动两个索引。

用确定性的 Fake（不下载模型、不连 Qdrant、不调 LLM）验证**接线**本身：
如果这里错了，再好的检索算法也不会在页面上生效。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from aieb.brain.knowledge import BM25Index
from aieb.brain.retrieval.config import RetrievalConfig
from aieb.brain.retrieval.service import RetrievalService
from aieb.brain.service import BrainService
from tests.test_hybrid_modes import FakeEmbedder, FakeReranker, FakeVectorStore

DOC_A = (
    "第一章 总则\n第1条 供应商逾期交付的，按合同总额每日 0.5% 计收违约金，"
    "累计不超过合同总额的 20%。\n第2条 采购部应在 10 个工作日内完成初审。"
)
DOC_B = "第1条 项目分为需求、设计、开发、测试、上线五个里程碑。\n第2条 差旅住宿标准每晚不超过 600 元。"


@pytest.fixture()
def service(tmp_path: Path, monkeypatch):
    from aieb.config import ProviderConfig

    provider = ProviderConfig(
        name="offline", protocol="openai-compat", base_url="http://127.0.0.1:9/x",
        model="offline", api_key="offline",
    )
    cfg = RetrievalConfig(mode="hybrid", candidates=10, context_budget="budgeted")
    svc = BrainService(work_dir=tmp_path, provider=provider, retrieval_config=cfg)
    # 注入确定性依赖，避免测试依赖网络/模型
    svc._retrieval = RetrievalService(
        bm25=svc.index,
        config=cfg,
        store=svc.store,
        embedder=FakeEmbedder(),
        vector_store=FakeVectorStore(),
        reranker=FakeReranker(),
    )

    # `_sync_complete` 是同步函数（内部 asyncio.run），桩函数也必须是同步的
    def fake_complete(provider_arg, system, prompt, **kwargs):  # noqa: ANN001
        return "违约金上限为合同总额的 20% [来源：供应商制度.md · 第1页]"

    monkeypatch.setattr("aieb.brain.service._sync_complete", fake_complete)
    yield svc
    svc.store.close()


def test_upload_writes_both_indexes(service):
    doc = service.upload("供应商制度.md", DOC_A.encode())
    assert doc["status"] == "ready"
    assert service.index.size > 0, "BM25 应已建立"
    assert service.retrieval.vector_store.count() == service.index.size, "向量索引应与分块数一致"


def test_reupload_same_file_does_not_duplicate(service):
    first = service.upload("供应商制度.md", DOC_A.encode())
    before_docs = len(service.store.list_documents())
    before_points = service.retrieval.vector_store.count()

    second = service.upload("供应商制度.md", DOC_A.encode())
    assert second.get("reused") is True
    assert second["id"] == first["id"], "重复上传应复用同一文档记录"
    assert len(service.store.list_documents()) == before_docs
    assert service.retrieval.vector_store.count() == before_points, "不得产生重复向量"


def test_env_context_and_hint_are_unaffected(service):
    """知识库状态注入是 Agent 路由的关键，改造后必须仍然正常。"""
    empty = service.knowledge_context()
    assert "为空" in empty
    service.upload("供应商制度.md", DOC_A.encode())
    assert "供应商制度.md" in service.knowledge_context()
    assert service.has_documents() is True


@pytest.mark.asyncio
async def test_answer_returns_citations_and_retrieval_trace(service):
    service.upload("供应商制度.md", DOC_A.encode())
    payload = await service.answer("违约金上限是多少", top_k=5)
    assert "20%" in payload["answer"]
    assert payload["citations"], "必须返回引用卡片（含页码）"
    assert payload["citations"][0]["page"] >= 1
    assert payload["retrieval"]["mode"] == "hybrid", "回答结果应带检索溯源"
    assert payload["context_meta"]["retrieved"] >= payload["context_meta"]["included"]


@pytest.mark.asyncio
async def test_answer_degrades_loudly_when_vector_store_down(service, monkeypatch):
    """向量库挂掉不能静默：结果里必须能看到降级原因，且仍能返回 BM25 结果。"""
    service.upload("供应商制度.md", DOC_A.encode())
    service.retrieval.vector_store.fail = True
    payload = await service.answer("违约金上限是多少", top_k=5)
    assert payload["retrieval"]["degraded"] is True
    assert "VectorStoreError" in payload["retrieval"]["degraded_reason"]
    assert payload["citations"], "降级后仍应给出 BM25 的引用"


def test_delete_clears_both_indexes(service):
    doc = service.upload("供应商制度.md", DOC_A.encode())
    service.upload("项目规范.md", DOC_B.encode())
    assert service.retrieval.vector_store.count() > 0

    assert service.delete_document(doc["id"]) is True
    remaining = service.index.size
    assert remaining > 0
    assert service.retrieval.vector_store.count() == remaining, "删除后向量与 BM25 数量应一致"
    hits = service.retrieval.vector_store.search(
        service.retrieval.embedder.embed_query("违约金"), top_k=10
    )
    assert all("供应商制度" not in h.doc_name for h in hits), "被删文档不应还能被召回"


def test_bm25_only_mode_never_touches_vectors(tmp_path: Path):
    """默认模式必须保持"零向量依赖"，否则没装 Qdrant 的环境会直接崩。"""
    from aieb.config import ProviderConfig

    provider = ProviderConfig(
        name="offline", protocol="openai-compat", base_url="http://127.0.0.1:9/x",
        model="offline", api_key="offline",
    )
    svc = BrainService(work_dir=tmp_path, provider=provider, retrieval_config=RetrievalConfig())
    try:
        assert svc.retrieval_config.mode == "bm25"
        svc.upload("供应商制度.md", DOC_A.encode())
        hits = svc.search("违约金", top_k=3)
        assert hits and hits[0]["doc_name"] == "供应商制度.md"
        assert svc.last_retrieval["mode"] == "bm25"
        assert svc._retrieval is None, "bm25 模式下不应构造 RetrievalService"
    finally:
        svc.store.close()
