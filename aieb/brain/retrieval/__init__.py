"""企业级混合检索：BM25 + Embedding + Qdrant(HNSW) + RRF + Reranker。

模块划分（每一层都可单独测试、单独替换）：

| 模块 | 职责 |
| --- | --- |
| `config` | 环境变量配置，默认 `bm25`，保证改造前行为不变 |
| `embedding` | ONNX Embedding + 磁盘缓存 + 版本标识 |
| `vector_store` | Qdrant 封装：HNSW、幂等 upsert、payload 过滤、批次写入 |
| `index` | SQLite → 向量库的同步/重建（SQLite 是唯一真源） |
| `fusion` | RRF 融合（只用名次，不用分数） |
| `rerank` | cross-encoder 重排，带超时与可追踪降级 |
| `context` | 上下文预算：Retrieved / Selected / Included / Tokens 四层计数 |
| `service` | 统一检索入口：bm25 / vector / hybrid / hybrid_rerank |

**本包的 `__init__` 刻意不做任何 eager 导入**：`BrainService` 与 benchmark
会从各种入口导入子模块，而 `retrieval.service` 又依赖 `knowledge`。
如果在这里 eager import，就会形成 `knowledge → retrieval → service → knowledge`
的循环。下面用 PEP 562 的模块级 `__getattr__` 做惰性导出，既保留
`from aieb.brain.retrieval import RetrievalService` 的用法，又不产生副作用。
"""

from __future__ import annotations

from typing import Any

_LAZY: dict[str, tuple[str, str]] = {
    "RetrievalConfig": ("aieb.brain.retrieval.config", "RetrievalConfig"),
    "MODES": ("aieb.brain.retrieval.config", "MODES"),
    "ContextBundle": ("aieb.brain.retrieval.context", "ContextBundle"),
    "build_budgeted_context": ("aieb.brain.retrieval.context", "build_budgeted_context"),
    "estimate_tokens": ("aieb.brain.retrieval.context", "estimate_tokens"),
    "EmbeddingError": ("aieb.brain.retrieval.embedding", "EmbeddingError"),
    "FastEmbedEmbedder": ("aieb.brain.retrieval.embedding", "FastEmbedEmbedder"),
    "configure_registry": ("aieb.brain.retrieval.embedding", "configure_registry"),
    "get_embedder": ("aieb.brain.retrieval.embedding", "get_embedder"),
    "FusedHit": ("aieb.brain.retrieval.fusion", "FusedHit"),
    "rrf_fuse": ("aieb.brain.retrieval.fusion", "rrf_fuse"),
    "rank_changes": ("aieb.brain.retrieval.fusion", "rank_changes"),
    "SyncStats": ("aieb.brain.retrieval.index", "SyncStats"),
    "VectorIndexer": ("aieb.brain.retrieval.index", "VectorIndexer"),
    "CrossEncoderReranker": ("aieb.brain.retrieval.rerank", "CrossEncoderReranker"),
    "RerankerError": ("aieb.brain.retrieval.rerank", "RerankerError"),
    "RerankResult": ("aieb.brain.retrieval.rerank", "RerankResult"),
    "RetrievalResult": ("aieb.brain.retrieval.service", "RetrievalResult"),
    "RetrievalService": ("aieb.brain.retrieval.service", "RetrievalService"),
    "ChunkRecord": ("aieb.brain.retrieval.vector_store", "ChunkRecord"),
    "QdrantVectorStore": ("aieb.brain.retrieval.vector_store", "QdrantVectorStore"),
    "VectorHit": ("aieb.brain.retrieval.vector_store", "VectorHit"),
    "VectorStoreError": ("aieb.brain.retrieval.vector_store", "VectorStoreError"),
    "point_id_for": ("aieb.brain.retrieval.vector_store", "point_id_for"),
}

__all__ = sorted(_LAZY)


def __getattr__(name: str) -> Any:  # pragma: no cover - 简单惰性转发
    target = _LAZY.get(name)
    if target is None:
        raise AttributeError(f"module 'aieb.brain.retrieval' has no attribute {name!r}")
    import importlib

    module = importlib.import_module(target[0])
    value = getattr(module, target[1])
    globals()[name] = value
    return value


def __dir__() -> list[str]:  # pragma: no cover
    return list(__all__)
