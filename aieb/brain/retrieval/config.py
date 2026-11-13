"""检索层配置：环境变量驱动，默认值保持与改造前一致。

设计原则（对应任务书"最高优先级原则 11"）：

* **默认模式仍是 `bm25`**，未验证的混合检索不会悄悄变成生产行为；
* 所有开关都能用环境变量覆盖，便于 A/B 与故障回退；
* 依赖缺失时不是 import 崩溃，而是**在真正调用时**抛出可读错误。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

#: 检索模式。`bm25` 是改造前的唯一行为，保持默认。
MODES = ("bm25", "vector", "hybrid", "hybrid_rerank")

DEFAULT_EMBEDDING_MODEL = "BAAI/bge-small-zh-v1.5"
DEFAULT_RERANK_MODEL = "BAAI/bge-reranker-base"


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = _env(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name).lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return default


@dataclass
class RetrievalConfig:
    """一次检索会话的全部可调参数。"""

    mode: str = "bm25"

    # --- 向量库 ---
    qdrant_url: str = "http://127.0.0.1:6333"
    qdrant_api_key: str = ""
    collection: str = "aieb_chunks"
    qdrant_timeout: float = 10.0

    # --- Embedding ---
    embedding_model: str = DEFAULT_EMBEDDING_MODEL
    embedding_batch_size: int = 32
    embedding_cache_dir: str = ""
    embedding_cache_enabled: bool = True

    # --- 融合与重排 ---
    rrf_k: int = 60
    candidates: int = 30  # 每路召回条数
    rerank_model: str = DEFAULT_RERANK_MODEL
    rerank_candidates: int = 10
    rerank_timeout: float = 30.0
    rerank_max_passage_chars: int = 256

    # --- 上下文预算 ---
    context_budget: str = "legacy"  # legacy | budgeted
    context_max_chars: int = 6000
    context_max_tokens: int = 1800
    chars_per_token: float = 1.74  # 实测中文口径，见 docs/HYBRID_RAG_ARCHITECTURE.md

    # --- 作用域 ---
    scope: str = "default"
    knowledge_base_id: str = "default"

    extra: dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------

    @classmethod
    def from_env(cls, **overrides: Any) -> "RetrievalConfig":
        cfg = cls(
            mode=_env("AIEB_RETRIEVAL_MODE", "bm25").lower(),
            qdrant_url=_env("AIEB_QDRANT_URL", "http://127.0.0.1:6333"),
            qdrant_api_key=_env("AIEB_QDRANT_API_KEY"),
            collection=_env("AIEB_QDRANT_COLLECTION", "aieb_chunks"),
            qdrant_timeout=_env_float("AIEB_QDRANT_TIMEOUT", 10.0),
            embedding_model=_env("AIEB_EMBEDDING_MODEL", DEFAULT_EMBEDDING_MODEL),
            embedding_batch_size=_env_int("AIEB_EMBEDDING_BATCH", 32),
            embedding_cache_dir=_env("AIEB_EMBEDDING_CACHE_DIR"),
            embedding_cache_enabled=_env_bool("AIEB_EMBEDDING_CACHE", True),
            rrf_k=_env_int("AIEB_RRF_K", 60),
            candidates=_env_int("AIEB_RETRIEVAL_CANDIDATES", 30),
            rerank_model=_env("AIEB_RERANK_MODEL", DEFAULT_RERANK_MODEL),
            rerank_candidates=_env_int("AIEB_RERANK_CANDIDATES", 10),
            rerank_timeout=_env_float("AIEB_RERANK_TIMEOUT", 30.0),
            rerank_max_passage_chars=_env_int("AIEB_RERANK_MAX_CHARS", 256),
            context_budget=_env("AIEB_CONTEXT_BUDGET", "legacy").lower(),
            context_max_chars=_env_int("AIEB_CONTEXT_MAX_CHARS", 6000),
            context_max_tokens=_env_int("AIEB_CONTEXT_MAX_TOKENS", 1800),
            chars_per_token=_env_float("AIEB_CHARS_PER_TOKEN", 1.74),
            scope=_env("AIEB_RETRIEVAL_SCOPE", "default"),
            knowledge_base_id=_env("AIEB_KNOWLEDGE_BASE_ID", "default"),
        )
        for key, value in overrides.items():
            if not hasattr(cfg, key):
                raise ValueError(f"未知检索配置项：{key}")
            setattr(cfg, key, value)
        cfg.validate()
        return cfg

    def validate(self) -> None:
        if self.mode not in MODES:
            raise ValueError(
                f"AIEB_RETRIEVAL_MODE={self.mode!r} 不支持，可选：{', '.join(MODES)}"
            )
        if self.context_budget not in {"legacy", "budgeted"}:
            raise ValueError(
                f"AIEB_CONTEXT_BUDGET={self.context_budget!r} 不支持，可选：legacy, budgeted"
            )
        if self.rrf_k < 1:
            raise ValueError("AIEB_RRF_K 必须 >= 1")
        if self.candidates < 1:
            raise ValueError("AIEB_RETRIEVAL_CANDIDATES 必须 >= 1")
        if self.chars_per_token <= 0:
            raise ValueError("AIEB_CHARS_PER_TOKEN 必须 > 0")

    # ------------------------------------------------------------------

    @property
    def uses_vector(self) -> bool:
        return self.mode in {"vector", "hybrid", "hybrid_rerank"}

    @property
    def uses_rerank(self) -> bool:
        return self.mode == "hybrid_rerank"

    def as_dict(self) -> dict[str, Any]:
        """用于日志与结果落盘。**不含 api key**。"""
        payload = {k: v for k, v in self.__dict__.items() if k != "extra"}
        payload["qdrant_api_key"] = "***" if self.qdrant_api_key else ""
        return payload
