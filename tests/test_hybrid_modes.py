"""四种检索模式的组合行为测试（用确定性 Fake，不下载模型、不连 Qdrant）。

覆盖点：

* `bm25` 模式绝不触碰 embedding / 向量库；
* `vector` 模式只走向量路；
* `hybrid` 用 RRF 融合两路，并保留来源与名次；
* `hybrid_rerank` 真正调用 reranker，且保留 document_id / page / chunk_key；
* 向量库不可用 → `degraded=True` 且**不静默**（有原因串），BM25 结果仍返回；
* reranker 失败 → 退回融合结果并标注降级。
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Sequence

import pytest

from aieb.brain.chunkkey import chunk_key, content_hash
from aieb.brain.knowledge import BM25Index
from aieb.brain.retrieval.config import RetrievalConfig
from aieb.brain.retrieval.embedding import EmbeddingStats
from aieb.brain.retrieval.rerank import RerankerError
from aieb.brain.retrieval.service import RetrievalService
from aieb.brain.store import BrainStore


class FakeEmbedder:
    """确定性 4 维向量：用字符哈希做桶，语义无关但完全可复现。"""

    model_version = "fake-embedder@4"
    dim = 4

    def __init__(self) -> None:
        self.stats = EmbeddingStats()
        self.query_calls = 0

    def _vector(self, text: str) -> list[float]:
        buckets = [0.0, 0.0, 0.0, 0.0]
        for ch in text:
            buckets[ord(ch) % 4] += 1.0
        norm = math.sqrt(sum(v * v for v in buckets)) or 1.0
        return [v / norm for v in buckets]

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        self.stats.texts += len(texts)
        self.stats.computed += len(texts)
        return [self._vector(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        self.query_calls += 1
        return self._vector(text)


class FakeVectorStore:
    def __init__(self, *, fail: bool = False) -> None:
        self.points: dict[str, dict[str, Any]] = {}
        self.vectors: dict[str, list[float]] = {}
        self.fail = fail

    def _guard(self) -> None:
        if self.fail:
            from aieb.brain.retrieval.vector_store import VectorStoreError

            raise VectorStoreError("连接 Qdrant 失败（测试注入）")

    def upsert(self, records, vectors, batch_size: int = 256) -> int:
        self._guard()
        for record, vector in zip(records, vectors):
            self.points[record.point_id] = record.payload()
            self.vectors[record.point_id] = list(vector)
        return len(records)

    def search(self, vector, *, top_k=10, scope_id="default", knowledge_base_id="default",
               document_ids=None, score_threshold=None):
        from aieb.brain.retrieval.vector_store import VectorHit

        self._guard()
        wanted = set(document_ids or [])
        scored = []
        for pid, payload in self.points.items():
            if payload.get("scope_id") != scope_id:
                continue
            if payload.get("knowledge_base_id") != knowledge_base_id:
                continue
            if wanted and payload.get("document_id") not in wanted:
                continue
            other = self.vectors[pid]
            dot = sum(a * b for a, b in zip(vector, other))
            na = math.sqrt(sum(a * a for a in vector)) or 1.0
            nb = math.sqrt(sum(b * b for b in other)) or 1.0
            scored.append((dot / (na * nb), payload))
        scored.sort(key=lambda item: -item[0])
        return [
            VectorHit(
                doc_id=str(p.get("document_id")),
                doc_name=str(p.get("doc_name")),
                page=int(p.get("page") or 0),
                text=str(p.get("content")),
                score=float(score),
                chunk_key=str(p.get("chunk_key")),
            )
            for score, p in scored[:top_k]
        ]

    def delete_by_document(self, document_id: str) -> None:
        self._guard()
        for pid in [k for k, v in self.points.items() if v.get("document_id") == document_id]:
            self.points.pop(pid, None)
            self.vectors.pop(pid, None)

    def delete_by_document_key(self, doc_key: str) -> None:
        for pid in [k for k, v in self.points.items() if v.get("document_key") == doc_key]:
            self.points.pop(pid, None)
            self.vectors.pop(pid, None)

    def count(self) -> int:
        return len(self.points)

    def info(self) -> dict[str, Any]:
        return {"points_count": len(self.points), "status": "green"}

    def health(self) -> bool:
        return not self.fail


class FakeReranker:
    """按"是否包含关键词"打分的假 reranker：只为验证流程，不代表真实效果。"""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls = 0

    def rerank(self, query: str, hits, top_k=None):
        from aieb.brain.retrieval.rerank import RerankResult
        from aieb.brain.retrieval.fusion import rank_changes

        if self.fail:
            raise RerankerError("模型加载失败（测试注入）")
        self.calls += 1
        candidates = [dict(h) for h in hits]
        before = [str(h.get("chunk_key")) for h in candidates]
        for hit in candidates:
            hit["rerank_score"] = float(hit["text"].count("违约金")) + float(
                hit["text"].count("逾期")
            )
        ranked = sorted(candidates, key=lambda h: -h["rerank_score"])
        if top_k:
            ranked = ranked[:top_k]
        return RerankResult(
            hits=ranked,
            latency_ms=1.0,
            applied=True,
            model="fake-reranker",
            changes=rank_changes(before, [str(h["chunk_key"]) for h in ranked]),
            scored=len(candidates),
        )


DOCS = (
    ("DOC-A", "供应商管理办法.md", "供应商逾期交付的，按合同总额每日 0.5% 计收违约金，累计不超过合同总额的 20%。"),
    ("DOC-B", "项目交付规范.md", "项目分为需求、设计、开发、测试、上线五个里程碑，每个里程碑需项目经理签字确认。"),
    ("DOC-C", "差旅报销制度.md", "出差结束后 15 个工作日内提交报销单，住宿标准为一线城市每晚 600 元。"),
)


@pytest.fixture()
def store(tmp_path: Path) -> BrainStore:
    brain = BrainStore(tmp_path)
    for doc_id, name, text in DOCS:
        brain.add_document(
            name=name, ext="md", size=len(text), stored_path="", status="ready",
            pages=1, chars=len(text), document_key=f"key-{doc_id}", scope_id="default",
        )
        docs = [d for d in brain.list_documents() if d["name"] == name]
        brain.add_chunks(docs[0]["id"], [{"page": 1, "idx": 0, "text": text}])
    yield brain
    brain.close()


def _service(store: BrainStore, mode: str, *, vector_store=None, reranker=None) -> RetrievalService:
    index = BM25Index(store)
    index.rebuild()
    return RetrievalService(
        bm25=index,
        config=RetrievalConfig(mode=mode, candidates=10, rrf_k=60),
        store=store,
        embedder=FakeEmbedder(),
        vector_store=vector_store or FakeVectorStore(),
        reranker=reranker,
    )


def _prime(service: RetrievalService) -> None:
    """把 SQLite 里的分块灌进 Fake 向量库（模拟一次全量同步）。"""
    from aieb.brain.retrieval.vector_store import ChunkRecord

    records = []
    for chunk in service.bm25.store.all_chunks():
        records.append(
            ChunkRecord(
                document_id=chunk["doc_id"],
                document_key=chunk.get("document_key") or chunk["doc_id"],
                doc_name=chunk["doc_name"],
                page=int(chunk["page"]),
                chunk_index=int(chunk["idx"]),
                content=chunk["text"],
                content_hash=chunk.get("content_hash") or content_hash(chunk["text"]),
                embedding_model_version="fake-embedder@4",
            )
        )
    vectors = service.embedder.embed_documents([r.content for r in records])
    service.vector_store.upsert(records, vectors)


# ---------------------------------------------------------------------------


def test_bm25_mode_never_touches_embedding_or_vector_store(store):
    service = _service(store, "bm25")
    result = service.search("违约金", top_k=3)
    assert result.mode == "bm25"
    assert result.routes_used == ["bm25"]
    assert service.embedder.query_calls == 0
    assert service.vector_store.count() == 0
    assert result.hits and result.hits[0]["doc_name"] == "供应商管理办法.md"


def test_vector_mode_returns_only_vector_route(store):
    service = _service(store, "vector")
    _prime(service)
    result = service.search("违约金", top_k=3)
    assert result.mode == "vector"
    assert set(result.routes_used) == {"vector"}
    assert service.embedder.query_calls == 1
    assert all(h["routes"] == ["vector"] for h in result.hits)


def test_hybrid_mode_fuses_and_keeps_provenance(store):
    service = _service(store, "hybrid")
    _prime(service)
    result = service.search("供应商逾期交付违约金", top_k=3)
    assert result.mode == "hybrid"
    assert set(result.routes_used) == {"bm25", "vector"}
    # 融合分数必须单调不增；每条命中都要能追溯来源与名次
    scores = [h["score"] for h in result.hits]
    assert scores == sorted(scores, reverse=True)
    assert any("bm25" in h["routes"] for h in result.hits)
    assert all(h["route_ranks"] and h["chunk_key"] for h in result.hits)
    # BM25 的第 1 名必须出现在结果里（融合不应丢掉单路最强项）
    bm25_top = service.bm25.search("供应商逾期交付违约金", top_k=1)[0]
    assert bm25_top["chunk_key"] in {h["chunk_key"] for h in result.hits}
    assert result.counts["bm25_candidates"] >= 1 and result.counts["vector_candidates"] >= 1


def test_hybrid_rerank_applies_model_and_keeps_page_and_ids(store):
    reranker = FakeReranker()
    service = _service(store, "hybrid_rerank", reranker=reranker)
    _prime(service)
    result = service.search("违约金上限", top_k=3)
    assert reranker.calls == 1
    assert result.rerank["applied"] is True
    assert result.hits[0]["doc_name"] == "供应商管理办法.md"
    for hit in result.hits:
        assert hit["doc_id"] and hit["page"] == 1 and hit["chunk_key"]


def test_vector_store_failure_is_reported_not_silent(store):
    service = _service(store, "hybrid", vector_store=FakeVectorStore(fail=True))
    result = service.search("违约金", top_k=3)
    assert result.degraded is True
    assert "VectorStoreError" in result.degraded_reason
    # 仍然返回 BM25 的结果，但必须带着降级标记
    assert result.hits and result.routes_used == ["bm25"]
    assert result.counts["vector_candidates"] == 0


def test_reranker_failure_falls_back_to_fusion_with_flag(store):
    service = _service(store, "hybrid_rerank", reranker=FakeReranker(fail=True))
    _prime(service)
    result = service.search("违约金", top_k=3)
    assert result.rerank["applied"] is False
    assert "rerank" in result.degraded_reason
    assert result.hits, "降级后仍应返回融合结果"


def test_bm25_route_honours_scope_filter(store):
    """作用域隔离不能只做在向量路上 —— BM25 也必须过滤。"""
    service = _service(store, "hybrid")
    _prime(service)
    allowed = service.search("违约金", top_k=3, scope_id="default")
    assert allowed.hits
    blocked = service.search("违约金", top_k=3, scope_id="another-tenant")
    assert blocked.hits == []
    assert blocked.counts.get("bm25_candidates", 0) == 0


def test_doc_ids_filter_applies_to_both_routes(store):
    service = _service(store, "hybrid")
    _prime(service)
    only_a = [d["id"] for d in store.list_documents() if d["name"] == "供应商管理办法.md"]
    result = service.search("交付 里程碑 违约金", top_k=3, doc_ids=only_a)
    assert result.hits
    assert {h["doc_name"] for h in result.hits} == {"供应商管理办法.md"}


def test_health_snapshot_reports_mode_and_counts(store):
    service = _service(store, "hybrid")
    _prime(service)
    health = service.health()
    assert health["mode"] == "hybrid"
    assert health["qdrant_ok"] is True
    assert health["vector_points"] == service.vector_store.count()
