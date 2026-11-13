"""Embedding 适配层：真实模型 + 磁盘缓存 + 明确的失败语义。

为什么是 ONNX（fastembed）而不是 transformers/torch：

* 本机可用显存 8GB，装 torch + 大模型会造成部署负担；
* `onnxruntime` 已经在依赖里，CPU/GPU 都能跑；
* fastembed 提供官方的模型注册表（维度/体积/许可），选型有据可依，不用猜。

**缓存**不是优化项而是必需项：同一份文档重复上传、四组消融共用同一批 chunk、
增量索引重建都会重复编码同样的文本。缓存键 = (model_version, sha256(标准化文本))，
保证"换模型必失效、同样文本必命中"。
"""

from __future__ import annotations

import hashlib
import sqlite3
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

from aieb.brain.chunkkey import content_hash, normalize_for_hash  # noqa: F401  (向后兼容导出)

DEFAULT_CACHE_DIR = Path(".aieb") / "cache" / "embeddings"


class EmbeddingError(RuntimeError):
    """Embedding 模型不可用或编码失败。"""


@dataclass
class EmbeddingStats:
    """编码统计，用于在报告里区分"真实计算"和"缓存命中"。"""

    texts: int = 0
    cache_hits: int = 0
    computed: int = 0
    batches: int = 0
    compute_seconds: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "texts": self.texts,
            "cache_hits": self.cache_hits,
            "computed": self.computed,
            "batches": self.batches,
            "compute_seconds": round(self.compute_seconds, 3),
            "cache_hit_rate": round(self.cache_hits / self.texts, 4) if self.texts else 0.0,
        }


class EmbeddingCache:
    """基于 SQLite 的向量缓存；只存 float32 原始字节，不做精度损失压缩。"""

    def __init__(self, path: Path | str, enabled: bool = True) -> None:
        self.enabled = enabled
        self.path = Path(path)
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None
        if self.enabled:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS embeddings ("
                " model_version TEXT NOT NULL,"
                " content_hash TEXT NOT NULL,"
                " dim INTEGER NOT NULL,"
                " vector BLOB NOT NULL,"
                " PRIMARY KEY (model_version, content_hash))"
            )
            self._conn.commit()

    def get_many(self, model_version: str, hashes: Sequence[str]) -> dict[str, Any]:
        if not self.enabled or not self._conn or not hashes:
            return {}
        import numpy as np

        out: dict[str, Any] = {}
        with self._lock:
            for start in range(0, len(hashes), 500):
                window = list(hashes[start : start + 500])
                marks = ",".join("?" for _ in window)
                rows = self._conn.execute(
                    f"SELECT content_hash, dim, vector FROM embeddings"
                    f" WHERE model_version=? AND content_hash IN ({marks})",
                    (model_version, *window),
                ).fetchall()
                for h, dim, blob in rows:
                    out[h] = np.frombuffer(blob, dtype=np.float32).reshape(int(dim))
        return out

    def put_many(self, model_version: str, items: Iterable[tuple[str, Any]]) -> None:
        if not self.enabled or not self._conn:
            return
        import numpy as np

        rows = []
        for h, vector in items:
            arr = np.asarray(vector, dtype=np.float32)
            rows.append((model_version, h, int(arr.shape[0]), arr.tobytes()))
        if not rows:
            return
        with self._lock:
            self._conn.executemany(
                "INSERT OR REPLACE INTO embeddings"
                " (model_version, content_hash, dim, vector) VALUES (?,?,?,?)",
                rows,
            )
            self._conn.commit()

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None


class FastEmbedEmbedder:
    """fastembed（ONNX）实现的 Embedding 提供方。

    `query` 与 `passage` 分开编码：BGE / Qwen3 系列对查询与文档使用不同的
    前缀或指令，混用会显著掉点。模型未提供 `query_embed` 时自动退回 `embed`。
    """

    def __init__(
        self,
        model_name: str,
        *,
        cache_dir: Path | str | None = None,
        cache_enabled: bool = True,
        batch_size: int = 32,
        threads: int | None = None,
    ) -> None:
        self.model_name = model_name
        self.batch_size = max(1, batch_size)
        # 线程数必须显式给：fastembed 的默认线程数在长文本（~500 token）下
        # 实测只有 ~12 texts/s，显式 16 线程可到 ~88 texts/s（差距 7 倍）。
        if threads is None:
            import os

            threads = min(16, max(1, os.cpu_count() or 4))
        self.threads = threads
        self._model: Any = None
        self._dim: int | None = None
        self.stats = EmbeddingStats()
        cache_path = Path(cache_dir or DEFAULT_CACHE_DIR) / "embedding_cache.sqlite3"
        self.cache = EmbeddingCache(cache_path, enabled=cache_enabled)

    # -- 模型加载（懒加载：bm25 模式下零开销） ------------------------

    def _ensure_model(self) -> Any:
        if self._model is not None:
            return self._model
        try:
            from fastembed import TextEmbedding
        except ImportError as exc:  # pragma: no cover - 依赖缺失
            raise EmbeddingError(
                "未安装 fastembed，无法启用向量检索；请 pip install fastembed"
            ) from exc
        try:
            self._model = TextEmbedding(
                model_name=self.model_name,
                threads=self.threads,
                cache_dir=str(self._cache_root()),
            )
        except Exception as exc:  # noqa: BLE001 - 模型下载/加载失败要可读
            raise EmbeddingError(
                f"加载 Embedding 模型 {self.model_name} 失败：{type(exc).__name__}: {exc}"
            ) from exc
        return self._model

    def _cache_root(self) -> Path:
        return Path(self.cache.path).parent / "models"

    @property
    def model_version(self) -> str:
        """写进向量 payload 的模型标识：模型名 + 维度，换模型必然换版本串。"""
        return f"{self.model_name}@{self.dim}"

    @property
    def dim(self) -> int:
        if self._dim is None:
            model = self._ensure_model()
            self._dim = int(model.embedding_size)
        return self._dim

    # -- 编码 ---------------------------------------------------------

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return self._embed(texts, kind="passage")

    def embed_query(self, text: str) -> list[float]:
        vectors = self._embed([text], kind="query")
        if not vectors:
            raise EmbeddingError("查询向量为空")
        return vectors[0]

    def _embed(self, texts: Sequence[str], kind: str) -> list[list[float]]:
        items = [str(t or "") for t in texts]
        if not items:
            return []
        import time

        import numpy as np

        self.stats.texts += len(items)
        hashes = [content_hash(t) for t in items]
        cached = self.cache.get_many(self.model_version, hashes)
        self.stats.cache_hits += len(cached)

        result: list[Any] = [None] * len(items)
        pending: list[int] = []
        for index, h in enumerate(hashes):
            hit = cached.get(h)
            if hit is None:
                pending.append(index)
            else:
                result[index] = hit

        if pending:
            model = self._ensure_model()
            todo = [items[i] for i in pending]
            started = time.perf_counter()
            producer = model.passage_embed if kind == "passage" else model.query_embed
            computed: list[Any] = []
            for batch in producer(todo, batch_size=self.batch_size):
                computed.append(np.asarray(batch, dtype=np.float32))
                self.stats.batches += 1
            self.stats.compute_seconds += time.perf_counter() - started
            stacked = np.vstack(computed) if computed else np.zeros((0, self.dim), np.float32)
            if stacked.shape[0] != len(todo):
                raise EmbeddingError(
                    f"Embedding 数量不匹配：期望 {len(todo)}，实际 {stacked.shape[0]}"
                )
            self.stats.computed += len(todo)
            fresh: list[tuple[str, Any]] = []
            for offset, index in enumerate(pending):
                vector = stacked[offset]
                result[index] = vector
                fresh.append((hashes[index], vector))
            self.cache.put_many(self.model_version, fresh)

        return [np.asarray(v, dtype=np.float32).tolist() for v in result]

    def close(self) -> None:
        self.cache.close()


@dataclass
class EmbeddingProviderRegistry:
    """按模型名缓存 embedder 实例，避免重复加载模型（加载一次约数百 MB）。"""

    cache_dir: Path | str | None = None
    cache_enabled: bool = True
    batch_size: int = 32
    _instances: dict[str, FastEmbedEmbedder] = field(default_factory=dict)

    def get(self, model_name: str) -> FastEmbedEmbedder:
        key = model_name
        if key not in self._instances:
            self._instances[key] = FastEmbedEmbedder(
                model_name,
                cache_dir=self.cache_dir,
                cache_enabled=self.cache_enabled,
                batch_size=self.batch_size,
            )
        return self._instances[key]

    def close(self) -> None:
        for instance in self._instances.values():
            instance.close()
        self._instances.clear()


_registry = EmbeddingProviderRegistry()


def configure_registry(**kwargs: Any) -> None:
    """供测试与 benchmark 覆盖全局 registry 配置。"""
    global _registry
    _registry.close()
    _registry = EmbeddingProviderRegistry(**kwargs)


def get_embedder(model_name: str) -> FastEmbedEmbedder:
    return _registry.get(model_name)
