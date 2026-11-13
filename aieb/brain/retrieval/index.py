"""向量索引同步：SQLite（真源）→ Embedding → Qdrant（派生索引）。

一致性策略（这是整个改造里最容易出错的地方）：

* **SQLite 是唯一真源**。向量库只是可重建的派生索引，任何时候都能用
  :meth:`VectorIndexer.rebuild` 从 SQLite 重新生成。
* 写入用确定性点 ID（`uuid5(chunk_key)`），所以"重复同步同一份文档"
  是幂等的，不需要先清空。
* 删除走 payload 过滤（`document_id` / `document_key`），保证文档删掉后
  向量不残留。
* 同步失败不回滚 SQLite：记录失败原因，界面可提示"向量索引待重建"，
  `rebuild` 是恢复路径。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Sequence

from aieb.brain.chunkkey import chunk_key, content_hash, document_key
from aieb.brain.retrieval.embedding import EmbeddingError, FastEmbedEmbedder
from aieb.brain.retrieval.vector_store import (
    ChunkRecord,
    QdrantVectorStore,
    VectorStoreError,
)
from aieb.brain.store import BrainStore


@dataclass
class SyncStats:
    documents: int = 0
    chunks: int = 0
    written: int = 0
    skipped: int = 0
    embed_seconds: float = 0.0
    upsert_seconds: float = 0.0
    cache_hits: int = 0
    computed: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "documents": self.documents,
            "chunks": self.chunks,
            "written": self.written,
            "skipped": self.skipped,
            "embed_seconds": round(self.embed_seconds, 3),
            "upsert_seconds": round(self.upsert_seconds, 3),
            "cache_hits": self.cache_hits,
            "computed": self.computed,
            "errors": self.errors[:5],
            "error_count": len(self.errors),
        }


class VectorIndexer:
    """把 SQLite 里的分块同步进 Qdrant。"""

    def __init__(
        self,
        store: BrainStore,
        embedder: FastEmbedEmbedder,
        vector_store: QdrantVectorStore,
        *,
        scope_id: str = "default",
        knowledge_base_id: str = "default",
        batch_size: int = 64,
    ) -> None:
        self.store = store
        self.embedder = embedder
        self.vector_store = vector_store
        self.scope_id = scope_id
        self.knowledge_base_id = knowledge_base_id
        self.batch_size = max(1, batch_size)

    # ------------------------------------------------------------------

    def _records_for(self, chunks: Sequence[dict[str, Any]], doc: dict[str, Any]) -> list[ChunkRecord]:
        doc_key = str(doc.get("document_key") or "")
        if not doc_key:
            # 老数据没有 document_key：用 doc_id 兜底，保证索引仍可建立
            doc_key = str(doc.get("id") or "")
        created_at = str(doc.get("created_at") or "")
        scope = str(doc.get("scope_id") or self.scope_id or "default")
        records: list[ChunkRecord] = []
        for index, chunk in enumerate(chunks):
            text = str(chunk.get("text") or "")
            if not text.strip():
                continue
            records.append(
                ChunkRecord(
                    document_id=str(doc.get("id") or ""),
                    document_key=doc_key,
                    doc_name=str(doc.get("name") or ""),
                    page=int(chunk.get("page") or 0),
                    chunk_index=int(chunk.get("idx", index)),
                    content=str(text),
                    content_hash=str(chunk.get("content_hash") or content_hash(text)),
                    embedding_model_version=self.embedder.model_version,
                    scope_id=scope,
                    knowledge_base_id=self.knowledge_base_id,
                    source_filename=str(doc.get("name") or ""),
                    created_at=created_at,
                )
            )
        return records

    def _write(self, records: list[ChunkRecord], stats: SyncStats) -> None:
        if not records:
            return
        for start in range(0, len(records), self.batch_size):
            window = records[start : start + self.batch_size]
            texts = [r.content for r in window]
            embed_started = time.perf_counter()
            try:
                vectors = self.embedder.embed_documents(texts)
            except EmbeddingError as exc:
                stats.errors.append(f"embedding: {exc}")
                stats.skipped += len(window)
                continue
            stats.embed_seconds += time.perf_counter() - embed_started
            upsert_started = time.perf_counter()
            try:
                stats.written += self.vector_store.upsert(window, vectors, batch_size=len(window))
            except VectorStoreError as exc:
                stats.errors.append(f"upsert: {exc}")
                stats.skipped += len(window)
            stats.upsert_seconds += time.perf_counter() - upsert_started
        snapshot = self.embedder.stats
        stats.cache_hits = snapshot.cache_hits
        stats.computed = snapshot.computed

    def sync_document(self, doc_id: str) -> SyncStats:
        """同步单个文档（幂等：同一内容重复同步不会产生重复点）。"""
        stats = SyncStats()
        doc = self.store.get_document(doc_id)
        if doc is None:
            stats.errors.append(f"文档不存在：{doc_id}")
            return stats
        if doc.get("status") not in {"ready", "partial"}:
            stats.errors.append(f"文档状态为 {doc.get('status')}，跳过向量化")
            return stats
        chunks = self.store.chunks_of(doc_id)
        records = self._records_for(chunks, doc)
        stats.documents += 1
        stats.chunks = len(records)
        # 先删旧点再写：处理"同名文档内容变化/分块策略变化"导致 key 变化的情况
        try:
            self.vector_store.delete_by_document(doc_id)
        except VectorStoreError as exc:
            stats.errors.append(f"清理旧向量失败：{exc}")
        self._write(records, stats)
        return stats

    def sync_all(self, *, limit: int = 0, progress: Any = None) -> SyncStats:
        """全量同步（首次建库 / 索引重建）。"""
        stats = SyncStats()
        documents = [
            d
            for d in self.store.list_documents()
            if d.get("status") in {"ready", "partial"}
        ]
        if limit:
            documents = documents[:limit]
        for index, doc in enumerate(documents, start=1):
            partial = self.sync_document(doc["id"])
            stats.documents += partial.documents
            stats.chunks += partial.chunks
            stats.written += partial.written
            stats.skipped += partial.skipped
            stats.embed_seconds += partial.embed_seconds
            stats.upsert_seconds += partial.upsert_seconds
            stats.errors.extend(partial.errors)
            if progress:
                progress(index, len(documents), doc.get("name") or doc["id"])
        snapshot = self.embedder.stats
        stats.cache_hits = snapshot.cache_hits
        stats.computed = snapshot.computed
        return stats

    def remove_document(self, doc_id: str) -> None:
        """删除文档对应的全部向量。"""
        self.vector_store.delete_by_document(doc_id)

    def rebuild(self, *, progress: Any = None) -> SyncStats:
        """丢弃集合并从 SQLite 全量重建（索引损坏 / 换模型后的恢复路径）。

        `allow_any=True`：这是用户/维护者主动发起的整库重建，目标就是当前配置的集合本身，
        不是基准脚本那种"顺手删一下"的场景。
        """
        try:
            self.vector_store.drop(allow_any=True)
        except VectorStoreError:
            pass
        return self.sync_all(progress=progress)
