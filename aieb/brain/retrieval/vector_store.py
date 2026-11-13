"""Qdrant 向量索引封装：HNSW、批量写入、payload 过滤、幂等 upsert。

设计要点：

1. **点 ID 是确定性的**（`uuid5(chunk_key)`），不是随机值。同一份文件重复上传、
   索引重建、增量补齐都会落到同一批点上，天然幂等，不需要"先删后插"。
2. **检索必须带作用域过滤**（`scope_id` + `knowledge_base_id`），过滤在服务端执行，
   不是拉回来再筛。这样即使未来多租户上线，也不会出现跨作用域召回。
3. **向量与 payload 一起存**：payload 保留 `content`，避免检索后再回 SQLite 逐条查文本
   （10 万块规模下那是 N 次随机读）。代价是磁盘上存了两份正文，报告里会量化。
4. 所有失败都转成 :class:`VectorStoreError`，并给出"下一步该做什么"的提示，
   而不是把 qdrant 的原始异常直接抛给用户。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from aieb.brain.chunkkey import chunk_key

#: 所有点 ID 用同一个命名空间，保证跨进程、跨机器结果一致。
POINT_NAMESPACE = uuid.UUID("6f1b0f5e-9f1c-5f5a-9a3e-1f2d3c4b5a60")

DEFAULT_HNSW_M = 16
DEFAULT_EF_CONSTRUCT = 100
DEFAULT_EF_SEARCH = 128
#: Qdrant 默认 10000：每个 segment 少于这么多点时会**暴力全扫**而不建 HNSW。
#: 单机单分片下 10 万点会被拆成若干 segment，默认值会让 HNSW 永远不生效，
#: 因此显式调低，保证"宣称用 HNSW"与"实际用 HNSW"一致。
DEFAULT_FULL_SCAN_THRESHOLD = 1000

#: `drop()` 默认只允许删除这些前缀开头的集合（基准/测试用），
#: 避免基准脚本在共享实例上误删业务集合。
DROPPABLE_PREFIXES = ("bench_", "scale_", "hybrid_", "emb_", "pytest_", "test_")


class VectorStoreError(RuntimeError):
    """向量库不可用、集合不匹配或写入失败。"""


def point_id_for(key: str) -> str:
    """由 chunk_key 派生稳定的 UUID（Qdrant 只接受 UUID / uint64 作为点 ID）。"""
    return str(uuid.uuid5(POINT_NAMESPACE, key))


@dataclass
class ChunkRecord:
    """写入向量库的一条 chunk。字段与任务书要求的元数据一一对应。"""

    document_id: str
    document_key: str
    doc_name: str
    page: int
    chunk_index: int
    content: str
    content_hash: str
    embedding_model_version: str
    scope_id: str = "default"
    knowledge_base_id: str = "default"
    source_filename: str = ""
    created_at: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> str:
        return chunk_key(self.document_key, self.content_hash, self.page)

    @property
    def point_id(self) -> str:
        return point_id_for(self.key)

    def payload(self) -> dict[str, Any]:
        payload = {
            "document_id": self.document_id,
            "document_key": self.document_key,
            "chunk_key": self.key,
            "doc_name": self.doc_name,
            "source_filename": self.source_filename or self.doc_name,
            "page": int(self.page),
            "chunk_index": int(self.chunk_index),
            "content": self.content,
            "content_hash": self.content_hash,
            "embedding_model_version": self.embedding_model_version,
            "scope_id": self.scope_id,
            "knowledge_base_id": self.knowledge_base_id,
            "created_at": self.created_at,
        }
        payload.update(self.extra)
        return payload


@dataclass
class VectorHit:
    """统一形状的检索命中，字段与 BM25 的 hit 对齐（多一个 distance/score 来源标记）。"""

    doc_id: str
    doc_name: str
    page: int
    text: str
    score: float
    chunk_key: str = ""
    source: str = "vector"

    def as_bm25_like(self) -> dict[str, Any]:
        """转换成既有代码期望的 dict 形状（doc_id/doc_name/page/text/score）。"""
        return {
            "doc_id": self.doc_id,
            "doc_name": self.doc_name,
            "page": int(self.page),
            "text": self.text,
            "score": float(self.score),
            "chunk_key": self.chunk_key,
            "source": self.source,
        }


class QdrantVectorStore:
    """Qdrant 客户端封装。构造时不连接，首次使用时才建连（bm25 模式下零开销）。"""

    def __init__(
        self,
        *,
        url: str = "http://127.0.0.1:6333",
        api_key: str = "",
        collection: str = "aieb_chunks",
        dim: int,
        timeout: float = 10.0,
        hnsw_m: int = DEFAULT_HNSW_M,
        ef_construct: int = DEFAULT_EF_CONSTRUCT,
        ef_search: int = DEFAULT_EF_SEARCH,
        full_scan_threshold: int = DEFAULT_FULL_SCAN_THRESHOLD,
    ) -> None:
        self.url = url
        self.api_key = api_key
        self.collection = collection
        self.dim = int(dim)
        self.timeout = float(timeout)
        self.hnsw_m = int(hnsw_m)
        self.ef_construct = int(ef_construct)
        self.ef_search = int(ef_search)
        self.full_scan_threshold = int(full_scan_threshold)
        self._client: Any = None
        self._ready = False

    # ------------------------------------------------------------------
    # 连接与集合管理
    # ------------------------------------------------------------------

    @property
    def client(self) -> Any:
        if self._client is None:
            try:
                from qdrant_client import QdrantClient
            except ImportError as exc:  # pragma: no cover - 依赖缺失
                raise VectorStoreError(
                    "未安装 qdrant-client，无法启用向量检索；请 pip install qdrant-client"
                ) from exc
            try:
                self._client = QdrantClient(
                    url=self.url, api_key=self.api_key or None, timeout=self.timeout
                )
            except Exception as exc:  # noqa: BLE001
                raise VectorStoreError(self._connect_hint(exc)) from exc
        return self._client

    def _connect_hint(self, exc: Exception) -> str:
        return (
            f"连接 Qdrant 失败（{self.url}）：{type(exc).__name__}: {exc}。"
            "请确认服务已启动：docker compose up -d qdrant，"
            "或在 Windows 上运行 tools/qdrant/qdrant.exe；"
            "也可用 AIEB_RETRIEVAL_MODE=bm25 回到纯关键词检索。"
        )

    def health(self) -> bool:
        try:
            self.client.get_collections()
            return True
        except Exception:  # noqa: BLE001 - 健康检查不抛异常，只回布尔
            return False

    def _ensure_collection(self) -> None:
        if self._ready:
            return
        from qdrant_client import models as qm

        try:
            exists = self.client.collection_exists(self.collection)
        except AttributeError:  # pragma: no cover - 老版本客户端
            exists = self.collection in {
                c.name for c in self.client.get_collections().collections
            }
        except Exception as exc:  # noqa: BLE001
            raise VectorStoreError(self._connect_hint(exc)) from exc

        if not exists:
            try:
                self.client.create_collection(
                    collection_name=self.collection,
                    vectors_config=qm.VectorParams(
                        size=self.dim,
                        distance=qm.Distance.COSINE,
                    ),
                    hnsw_config=qm.HnswConfigDiff(
                        m=self.hnsw_m,
                        ef_construct=self.ef_construct,
                        full_scan_threshold=self.full_scan_threshold,
                    ),
                    optimizers_config=qm.OptimizersConfigDiff(indexing_threshold=20000),
                )
            except Exception as exc:  # noqa: BLE001
                raise VectorStoreError(f"创建集合 {self.collection} 失败：{exc}") from exc
            self._create_payload_indexes()
        else:
            info = self.client.get_collection(self.collection)
            size = self._vector_size(info)
            if size and size != self.dim:
                raise VectorStoreError(
                    f"集合 {self.collection} 的向量维度是 {size}，"
                    f"但当前 Embedding 模型输出 {self.dim} 维。"
                    "换模型时必须换集合名（AIEB_QDRANT_COLLECTION）或重建索引。"
                )
        self._ready = True

    def _create_payload_indexes(self) -> None:
        from qdrant_client import models as qm

        for field in ("scope_id", "knowledge_base_id", "document_id", "document_key"):
            try:
                self.client.create_payload_index(
                    collection_name=self.collection,
                    field_name=field,
                    field_schema=qm.PayloadSchemaType.KEYWORD,
                )
            except Exception:  # noqa: BLE001 - 已存在则忽略
                pass

    @staticmethod
    def _vector_size(info: Any) -> int:
        try:
            params = info.config.params.vectors
            if isinstance(params, dict):
                first = next(iter(params.values()))
                return int(first.size)
            return int(params.size)
        except Exception:  # noqa: BLE001
            return 0

    # ------------------------------------------------------------------
    # 写入 / 删除
    # ------------------------------------------------------------------

    def upsert(
        self,
        records: Sequence[ChunkRecord],
        vectors: Sequence[Sequence[float]],
        *,
        batch_size: int = 256,
    ) -> int:
        """批量幂等写入。返回写入条数。"""
        if len(records) != len(vectors):
            raise VectorStoreError(
                f"记录数({len(records)})与向量数({len(vectors)})不一致，拒绝写入"
            )
        if not records:
            return 0
        self._ensure_collection()
        from qdrant_client import models as qm

        written = 0
        for start in range(0, len(records), max(1, batch_size)):
            window = records[start : start + batch_size]
            payload = []
            for index, record in enumerate(window):
                vector = vectors[start + index]
                if len(vector) != self.dim:
                    raise VectorStoreError(
                        f"向量维度 {len(vector)} 与集合维度 {self.dim} 不一致"
                    )
                payload.append(
                    qm.PointStruct(
                        id=record.point_id, vector=list(vector), payload=record.payload()
                    )
                )
            try:
                self.client.upsert(
                    collection_name=self.collection, points=payload, wait=True
                )
            except Exception as exc:  # noqa: BLE001
                raise VectorStoreError(f"写入向量失败（批次 {start}）：{exc}") from exc
            written += len(payload)
        return written

    def delete_by_document(self, document_id: str) -> None:
        self._delete_by("document_id", document_id)

    def delete_by_document_key(self, doc_key: str) -> None:
        self._delete_by("document_key", doc_key)

    def _delete_by(self, field: str, value: str) -> None:
        if not value:
            return
        self._ensure_collection()
        from qdrant_client import models as qm

        try:
            self.client.delete(
                collection_name=self.collection,
                points_selector=qm.FilterSelector(
                    filter=qm.Filter(
                        must=[qm.FieldCondition(key=field, match=qm.MatchValue(value=value))]
                    )
                ),
                wait=True,
            )
        except Exception as exc:  # noqa: BLE001
            raise VectorStoreError(f"按 {field}={value} 删除向量失败：{exc}") from exc

    def drop(self, *, allow_any: bool = False) -> None:
        """删除整个集合。

        **破坏性操作，默认只允许删基准测试集合**：
        collection 名必须以 `DROPPABLE_PREFIXES` 中的前缀开头（或显式传 `allow_any=True`）。
        这条护栏是为了避免基准脚本在共享 Qdrant 实例上把业务集合删掉。
        """
        if not allow_any and not self.collection.startswith(DROPPABLE_PREFIXES):
            raise VectorStoreError(
                f"拒绝删除集合 {self.collection!r}：不在允许自动删除的前缀 {DROPPABLE_PREFIXES} 内。"
                "（基准集合请以 bench_/scale_/hybrid_/emb_/pytest_ 开头；业务集合请勿删除）"
            )
        try:
            self.client.delete_collection(self.collection)
        except Exception as exc:  # noqa: BLE001
            raise VectorStoreError(f"删除集合 {self.collection} 失败：{exc}") from exc
        self._ready = False

    # ------------------------------------------------------------------
    # 检索
    # ------------------------------------------------------------------

    def search(
        self,
        vector: Sequence[float],
        *,
        top_k: int = 10,
        scope_id: str = "default",
        knowledge_base_id: str = "default",
        document_ids: Iterable[str] | None = None,
        score_threshold: float | None = None,
    ) -> list[VectorHit]:
        """向量检索。`scope_id` / `knowledge_base_id` 过滤在服务端执行。"""
        if top_k <= 0:
            return []
        self._ensure_collection()
        from qdrant_client import models as qm

        must: list[Any] = [
            qm.FieldCondition(key="scope_id", match=qm.MatchValue(value=scope_id)),
            qm.FieldCondition(
                key="knowledge_base_id", match=qm.MatchValue(value=knowledge_base_id)
            ),
        ]
        wanted = [d for d in (document_ids or []) if d]
        if wanted:
            must.append(
                qm.FieldCondition(key="document_id", match=qm.MatchAny(any=wanted))
            )
        query_filter = qm.Filter(must=must)

        try:
            response = self.client.query_points(
                collection_name=self.collection,
                query=list(vector),
                limit=int(top_k),
                query_filter=query_filter,
                with_payload=True,
                search_params=qm.SearchParams(hnsw_ef=self.ef_search),
                score_threshold=score_threshold,
            )
        except Exception as exc:  # noqa: BLE001
            raise VectorStoreError(self._connect_hint(exc)) from exc

        hits: list[VectorHit] = []
        for point in getattr(response, "points", []) or []:
            payload = dict(point.payload or {})
            hits.append(
                VectorHit(
                    doc_id=str(payload.get("document_id") or ""),
                    doc_name=str(payload.get("doc_name") or ""),
                    page=int(payload.get("page") or 0),
                    text=str(payload.get("content") or ""),
                    score=float(point.score or 0.0),
                    chunk_key=str(payload.get("chunk_key") or ""),
                    source="vector",
                )
            )
        return hits

    # ------------------------------------------------------------------
    # 运维
    # ------------------------------------------------------------------

    def count(self) -> int:
        self._ensure_collection()
        try:
            return int(self.client.count(self.collection, exact=True).count)
        except Exception as exc:  # noqa: BLE001
            raise VectorStoreError(f"统计点数失败：{exc}") from exc

    def info(self) -> dict[str, Any]:
        self._ensure_collection()
        try:
            raw = self.client.get_collection(self.collection)
        except Exception as exc:  # noqa: BLE001
            raise VectorStoreError(f"读取集合信息失败：{exc}") from exc
        config = getattr(raw, "config", None)
        hnsw_m = getattr(getattr(config, "hnsw_config", None), "m", None)
        if hnsw_m is None:
            hnsw_m = getattr(
                getattr(getattr(config, "params", None), "hnsw_config", None), "m", None
            )
        indexed = getattr(raw, "indexed_vectors_count", None)
        payload = {
            "collection": self.collection,
            "points_count": int(getattr(raw, "points_count", 0) or 0),
            "vectors_count": int(getattr(raw, "vectors_count", 0) or 0),
            # 新版本服务端可能不回报该字段（小集合尚未建 HNSW）
            "indexed_vectors_count": int(indexed) if indexed is not None else None,
            "status": str(getattr(raw, "status", "")),
            "dim": self.dim,
            "hnsw_m": hnsw_m,
            "url": self.url,
        }
        # 部分版本会直接给出该集合的磁盘/RAM 占用字节数（无则为 None）
        for field in ("disk_data_size", "ram_data_size", "payload_schema"):
            if field == "payload_schema":
                continue
            value = getattr(raw, field, None)
            if value is not None:
                payload[field] = int(value)
        return payload

    def wait_until_indexed(self, timeout: float = 120.0, interval: float = 0.5) -> bool:
        """等待 HNSW 构建完成。

        注意：点数少于 `indexing_threshold`（默认 2 万）时 Qdrant 用暴力扫描，
        根本不建 HNSW，`indexed_vectors_count` 会保持 0/None。因此这里只在
        "服务端确实报告了 indexed 计数且小于总数"时才等待，否则视为就绪。
        性能测试必须记录真实的 indexed_vectors_count，不能假装已经走 HNSW。
        """
        import time

        deadline = time.time() + timeout
        while time.time() < deadline:
            info = self.info()
            total = info["points_count"]
            indexed = info["indexed_vectors_count"]
            if total == 0:
                return True
            if indexed is None or indexed >= total:
                return True
            if str(info.get("status", "")).lower() == "green" and indexed == 0:
                return True
            time.sleep(interval)
        return False
