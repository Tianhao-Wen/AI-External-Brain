"""混合检索引擎：BM25 / Vector / Hybrid(RRF) / Hybrid+Rerank。

统一入口只有一个 :meth:`RetrievalService.search`，`BrainService` 只在它外面
做"用哪种上下文预算"的选择。这样 `bm25` 模式可以做到**逐位等价于改造前**，
而混合检索完全是"新增分支"，不是"改写老路径"。

失败语义（关键）：

* 向量库不可用 → **不静默降级**：`RetrievalResult.degraded=True` 且
  `degraded_reason` 写明原因，调用方可以据此在界面上如实说明。
* Reranker 超时/失败 → 退回融合结果，同样标注 `rerank.degraded_reason`。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Sequence

from aieb.brain.knowledge import BM25Index
from aieb.brain.retrieval.config import RetrievalConfig
from aieb.brain.retrieval.fusion import FusedHit, rrf_fuse
from aieb.brain.retrieval.rerank import CrossEncoderReranker, RerankerError
from aieb.brain.retrieval.vector_store import QdrantVectorStore, VectorStoreError
from aieb.brain.retrieval.embedding import EmbeddingError, FastEmbedEmbedder


@dataclass
class RetrievalResult:
    hits: list[dict[str, Any]] = field(default_factory=list)
    mode: str = "bm25"
    degraded: bool = False
    degraded_reason: str = ""
    timings: dict[str, float] = field(default_factory=dict)
    counts: dict[str, int] = field(default_factory=dict)
    rerank: dict[str, Any] = field(default_factory=dict)
    routes_used: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "degraded": self.degraded,
            "degraded_reason": self.degraded_reason,
            "timings_ms": {k: round(v, 2) for k, v in self.timings.items()},
            "counts": dict(self.counts),
            "rerank": dict(self.rerank),
            "routes_used": list(self.routes_used),
        }


class RetrievalService:
    """按配置组合多路召回。所有依赖都是可注入的，便于测试与消融。"""

    def __init__(
        self,
        *,
        bm25: BM25Index,
        config: RetrievalConfig,
        store: Any = None,
        embedder: FastEmbedEmbedder | None = None,
        vector_store: QdrantVectorStore | None = None,
        reranker: CrossEncoderReranker | None = None,
    ) -> None:
        self.bm25 = bm25
        self.config = config
        self.store = store
        self._embedder = embedder
        self._vector_store = vector_store
        self._reranker = reranker

    # -- 懒建依赖：bm25 模式下不加载任何模型/网络连接 ----------------

    @property
    def embedder(self) -> FastEmbedEmbedder:
        if self._embedder is None:
            from aieb.brain.retrieval.embedding import get_embedder

            self._embedder = get_embedder(self.config.embedding_model)
        return self._embedder

    @property
    def vector_store(self) -> QdrantVectorStore:
        if self._vector_store is None:
            self._vector_store = QdrantVectorStore(
                url=self.config.qdrant_url,
                api_key=self.config.qdrant_api_key,
                collection=self.config.collection,
                dim=self.embedder.dim,
                timeout=self.config.qdrant_timeout,
            )
        return self._vector_store

    @property
    def reranker(self) -> CrossEncoderReranker:
        if self._reranker is None:
            self._reranker = CrossEncoderReranker(
                self.config.rerank_model,
                timeout=self.config.rerank_timeout,
                max_passage_chars=self.config.rerank_max_passage_chars,
            )
        return self._reranker

    # ------------------------------------------------------------------

    def search(
        self,
        query: str,
        *,
        top_k: int = 5,
        doc_ids: Sequence[str] | None = None,
        scope_id: str | None = None,
        knowledge_base_id: str | None = None,
    ) -> RetrievalResult:
        mode = self.config.mode
        if mode == "bm25":
            return self._bm25_only(query, top_k, doc_ids)

        timings: dict[str, float] = {}
        counts: dict[str, int] = {"requested_top_k": int(top_k)}
        degraded_reason = ""
        routes: list[tuple[str, list[dict[str, Any]]]] = []

        wanted = {str(d) for d in (doc_ids or []) if d} or None
        candidates = max(int(self.config.candidates), int(top_k))
        scope = scope_id or self.config.scope

        # BM25 是纯内存索引，没有 payload 过滤能力：必须在应用层按作用域
        # 先把"允许的 doc_id"算出来，否则会出现"向量路隔离了、BM25 路泄漏了"。
        scoped_ids = self._scope_doc_ids(scope)
        bm25_scope = self._intersect(wanted, scoped_ids)
        if bm25_scope is not None and not bm25_scope:
            bm25_scope = {"__no_document_in_scope__"}  # 空集合无法表达"一个都不允许"

        # --- BM25 路（hybrid 家族才有）---
        if mode in {"hybrid", "hybrid_rerank"}:
            started = time.perf_counter()
            bm25_hits = self.bm25.search(query, top_k=candidates, doc_ids=bm25_scope)
            timings["bm25_ms"] = (time.perf_counter() - started) * 1000
            counts["bm25_candidates"] = len(bm25_hits)
            routes.append(("bm25", bm25_hits))

        # --- 向量路 ---
        vector_hits: list[dict[str, Any]] = []
        started = time.perf_counter()
        try:
            vector = self.embedder.embed_query(query)
            timings["embed_query_ms"] = (time.perf_counter() - started) * 1000
            started = time.perf_counter()
            vector_hits = [
                hit.as_bm25_like()
                for hit in self.vector_store.search(
                    vector,
                    top_k=candidates,
                    scope_id=scope_id or self.config.scope,
                    knowledge_base_id=knowledge_base_id or self.config.knowledge_base_id,
                    document_ids=wanted,
                )
            ]
            timings["vector_ms"] = (time.perf_counter() - started) * 1000
        except (VectorStoreError, EmbeddingError) as exc:
            degraded_reason = f"{type(exc).__name__}: {exc}"
            timings.setdefault("embed_query_ms", (time.perf_counter() - started) * 1000)
            timings["vector_ms"] = timings.get("vector_ms", 0.0)
        counts["vector_candidates"] = len(vector_hits)
        if vector_hits:
            routes.append(("vector", vector_hits))

        # --- 融合 ---
        if len(routes) > 1:
            started = time.perf_counter()
            fused = rrf_fuse(routes, k=self.config.rrf_k)
            timings["rrf_ms"] = (time.perf_counter() - started) * 1000
        elif routes:
            # 只有一路（例如向量库挂了）时不做"伪融合"，直接沿用该路排序，
            # 但仍按同一口径产出 hits，并在 degraded 里说明。
            name, hits = routes[0]
            fused = [
                FusedHit(
                    key=str(h.get("chunk_key") or f"{h.get('doc_id')}:{h.get('page')}"),
                    doc_id=str(h.get("doc_id") or ""),
                    doc_name=str(h.get("doc_name") or ""),
                    page=int(h.get("page") or 0),
                    text=str(h.get("text") or ""),
                    rrf_score=float(h.get("score") or 0.0),
                    routes=[name],
                    route_ranks={name: i + 1},
                    scores={name: float(h.get("score") or 0.0)},
                )
                for i, h in enumerate(hits)
            ]
        else:
            fused = []

        counts["fused"] = len(fused)
        hits = [item.as_hit() for item in fused]

        # --- 重排 ---
        rerank_info: dict[str, Any] = {"applied": False}
        if mode == "hybrid_rerank" and hits:
            rerank_candidates = max(int(top_k), min(len(hits), int(self.config.rerank_candidates)))
            pool = hits[:rerank_candidates]
            started = time.perf_counter()
            try:
                result = self.reranker.rerank(query, pool, top_k=top_k)
                timings["rerank_ms"] = (time.perf_counter() - started) * 1000
                rerank_info = result.as_dict()
                hits = result.hits
            except RerankerError as exc:
                timings["rerank_ms"] = (time.perf_counter() - started) * 1000
                rerank_info = {
                    "applied": False,
                    "degraded_reason": str(exc),
                    "model": self.config.rerank_model,
                }
                degraded_reason = degraded_reason or f"rerank 降级：{exc}"

        final = hits[:top_k]
        counts["returned"] = len(final)
        return RetrievalResult(
            hits=final,
            mode=mode,
            degraded=bool(degraded_reason),
            degraded_reason=degraded_reason,
            timings=timings,
            counts=counts,
            rerank=rerank_info,
            routes_used=[name for name, _ in routes],
        )

    def _scope_doc_ids(self, scope: str) -> set[str] | None:
        """返回该作用域下允许检索的文档 ID；`None` 表示不做限制。"""
        if self.store is None:
            return None
        try:
            return self.store.document_ids_in_scope(scope)
        except Exception:  # noqa: BLE001 - 老库无该字段时退化为不限制
            return None

    @staticmethod
    def _intersect(a: set[str] | None, b: set[str] | None) -> set[str] | None:
        if a is None:
            return b
        if b is None:
            return a
        return a & b

    def _bm25_only(
        self, query: str, top_k: int, doc_ids: Sequence[str] | None
    ) -> RetrievalResult:
        started = time.perf_counter()
        hits = self.bm25.search(
            query, top_k=top_k, doc_ids={str(d) for d in (doc_ids or [])} or None
        )
        elapsed = (time.perf_counter() - started) * 1000
        return RetrievalResult(
            hits=hits,
            mode="bm25",
            timings={"bm25_ms": elapsed},
            counts={
                "requested_top_k": int(top_k),
                "bm25_candidates": len(hits),
                "returned": len(hits),
            },
            routes_used=["bm25"],
        )

    # ------------------------------------------------------------------

    def health(self) -> dict[str, Any]:
        """给界面/运维用的健康快照：不抛异常，只报事实。"""
        payload: dict[str, Any] = {
            "mode": self.config.mode,
            "bm25_docs": self.bm25.size,
            "embedding_model": self.config.embedding_model,
            "collection": self.config.collection,
            "qdrant_url": self.config.qdrant_url,
        }
        if self.config.uses_vector:
            payload["qdrant_ok"] = self.vector_store.health()
            try:
                payload["vector_points"] = self.vector_store.count()
            except VectorStoreError as exc:
                payload["vector_points"] = None
                payload["vector_error"] = str(exc)
        return payload
