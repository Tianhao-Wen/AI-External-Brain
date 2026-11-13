"""Reranker：真实 cross-encoder 重排，带超时与可追踪的降级。

约束（来自任务书）：

* 必须是**真实模型**，不能用关键词规则冒充；
* 支持批量、可配置候选上限、明确超时；
* 资源不足时回退到 RRF 结果，且**必须标注**发生了回退；
* 重排后仍保留 document_id / page / chunk_key；
* 不得把 rerank 分数解释成概率（这里只当作**相对排序分**）。

实现选择 fastembed 的 `TextCrossEncoder`（ONNX）。它在 CPU 上即可运行，
模型清单里有 apache-2.0 / MIT 许可的多语版本，`cc-by-nc` 的 jina 系列被排除。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Sequence

from aieb.brain.retrieval.fusion import rank_changes

DEFAULT_RERANK_MODEL = "BAAI/bge-reranker-base"


class RerankerError(RuntimeError):
    """重排不可用或超时。调用方应降级到融合结果并标注 `degraded_reason`。"""


@dataclass
class RerankResult:
    hits: list[dict[str, Any]]
    latency_ms: float
    applied: bool
    model: str = ""
    changes: dict[str, Any] = field(default_factory=dict)
    degraded_reason: str = ""
    scored: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "applied": self.applied,
            "model": self.model,
            "latency_ms": round(self.latency_ms, 2),
            "scored": self.scored,
            "changes": self.changes,
            "degraded_reason": self.degraded_reason,
        }


class CrossEncoderReranker:
    """fastembed cross-encoder 封装（懒加载，首次调用才下载/加载模型）。"""

    def __init__(
        self,
        model_name: str = DEFAULT_RERANK_MODEL,
        *,
        timeout: float = 30.0,
        batch_size: int = 16,
        threads: int | None = None,
        cache_dir: str | None = None,
        max_passage_chars: int = 256,
    ) -> None:
        self.model_name = model_name
        self.timeout = float(timeout)
        self.batch_size = max(1, batch_size)
        # 线程数取实测最优：本机 32 核，8/16 线程明显快于 4 与 32
        # （32 线程反而因调度开销慢 2.5 倍，见 docs/HYBRID_RAG_BENCHMARK.md）。
        if threads is None:
            import os

            threads = min(16, max(1, (os.cpu_count() or 4)))
        self.threads = threads
        self.cache_dir = cache_dir
        # 送进 cross-encoder 的片段长度：实测长分块（1400 字符）会让单次重排
        # 从 ~80ms 膨胀到 20s+；截断到 512 字符是业界常规做法，
        # 代价只是丢掉句尾，而句尾很少是判断相关性的关键。
        self.max_passage_chars = max(64, int(max_passage_chars))
        self._model: Any = None
        self._load_seconds: float = 0.0

    def _ensure_model(self) -> Any:
        if self._model is not None:
            return self._model
        try:
            from fastembed.rerank.cross_encoder import TextCrossEncoder
        except ImportError as exc:  # pragma: no cover
            raise RerankerError(
                "未安装 fastembed 的 rerank 组件，无法启用重排；检索会降级为 RRF 结果"
            ) from exc
        started = time.perf_counter()
        try:
            kwargs: dict[str, Any] = {"model_name": self.model_name, "threads": self.threads}
            if self.cache_dir:
                kwargs["cache_dir"] = str(self.cache_dir)
            self._model = TextCrossEncoder(**kwargs)
        except Exception as exc:  # noqa: BLE001
            raise RerankerError(
                f"加载 Reranker 模型 {self.model_name} 失败：{type(exc).__name__}: {exc}"
            ) from exc
        self._load_seconds = time.perf_counter() - started
        return self._model

    def warmup(self) -> float:
        """预热：把模型加载与 ONNX 会话初始化提前做完。

        这一步必须与"推理超时"分开，否则首次调用会把 30~120s 的模型加载
        算进推理耗时，误触发降级。返回值是加载耗时（秒）。
        """
        self._ensure_model()
        return self._load_seconds

    # ------------------------------------------------------------------

    def _score(self, query: str, documents: Sequence[str]) -> list[float]:
        model = self._ensure_model()
        scores: list[float] = []
        for score in model.rerank(query, list(documents), batch_size=self.batch_size):
            scores.append(float(score))
        return scores

    def rerank(
        self, query: str, hits: Sequence[dict[str, Any]], *, top_k: int | None = None
    ) -> RerankResult:
        """对候选重新打分并排序。失败/超时抛出 :class:`RerankerError`。"""
        candidates = [dict(h) for h in hits]
        if not candidates:
            return RerankResult(hits=[], latency_ms=0.0, applied=False, model=self.model_name)

        documents = [str(h.get("text") or "")[: self.max_passage_chars] for h in candidates]
        before = [str(h.get("chunk_key") or "") for h in candidates]
        before_positions = {key: index + 1 for index, key in enumerate(before) if key}

        # 先加载模型再计时：模型加载不是推理，不能把加载耗时计入超时判定
        self._ensure_model()

        import concurrent.futures

        started = time.perf_counter()
        # 用独立线程跑推理，主线程只等 timeout：fastembed 同步 API 无法中途取消，
        # 但至少可以保证调用方不会被无限期阻塞。
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(self._score, query, documents)
            try:
                scores = future.result(timeout=self.timeout)
            except concurrent.futures.TimeoutError as exc:
                future.cancel()
                raise RerankerError(
                    f"Reranker 推理超过 {self.timeout:.1f}s，已降级为融合结果"
                ) from exc
            except Exception as exc:  # noqa: BLE001
                raise RerankerError(
                    f"Reranker 推理失败：{type(exc).__name__}: {exc}"
                ) from exc
        latency_ms = (time.perf_counter() - started) * 1000

        if len(scores) != len(candidates):
            raise RerankerError(
                f"Reranker 返回 {len(scores)} 个分数，与候选数 {len(candidates)} 不一致"
            )

        for hit, score in zip(candidates, scores):
            hit["rerank_score"] = score
            hit["pre_rerank_rank"] = before_positions.get(str(hit.get("chunk_key") or ""))
            hit["source"] = hit.get("source") or "hybrid"

        ranked = sorted(
            candidates,
            key=lambda h: (-float(h.get("rerank_score") or 0.0), str(h.get("chunk_key") or "")),
        )
        if top_k:
            ranked = ranked[:top_k]
        after = [str(h.get("chunk_key") or "") for h in ranked]
        return RerankResult(
            hits=ranked,
            latency_ms=latency_ms,
            applied=True,
            model=self.model_name,
            changes=rank_changes(before, after),
            scored=len(candidates),
        )
