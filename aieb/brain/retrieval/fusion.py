"""RRF（Reciprocal Rank Fusion）融合。

为什么用 RRF 而不是"分数加权和"：

* BM25 分数是无界正数（中文 bigram 下常见 5~40），余弦相似度是 [-1, 1]，
  两者**量纲不同**，直接相加等于让 BM25 单方面决定排序；
* RRF 只使用**名次**，天然免疫分数量纲与分布差异（Cormack et al., 2009）；
* 它只有两个超参：融合常数 `k` 与每路候选数，容易在开发集上验证。

公式：

    RRF(d) = Σ_r  w_r / (k + rank_r(d))

其中 `rank` 从 1 开始。`k` 越大越平滑（弱化头部绝对优势），常用 60。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from aieb.brain.chunkkey import chunk_key, content_hash

DEFAULT_RRF_K = 60


@dataclass
class FusedHit:
    """融合后的统一命中。字段与 BM25 hit 保持兼容，另加融合溯源信息。"""

    key: str
    doc_id: str
    doc_name: str
    page: int
    text: str
    rrf_score: float = 0.0
    routes: list[str] = field(default_factory=list)
    route_ranks: dict[str, int] = field(default_factory=dict)
    scores: dict[str, float] = field(default_factory=dict)

    def as_hit(self) -> dict[str, Any]:
        payload = {
            "doc_id": self.doc_id,
            "doc_name": self.doc_name,
            "page": int(self.page),
            "text": self.text,
            "score": round(self.rrf_score, 6),
            "chunk_key": self.key,
            "routes": list(self.routes),
            "route_ranks": dict(self.route_ranks),
            "source_scores": {k: round(v, 6) for k, v in self.scores.items()},
        }
        return payload


def hit_key(hit: dict[str, Any]) -> str:
    """为任意来源的 hit 计算稳定去重键。

    优先用检索后端给出的 `chunk_key`；BM25 目前不返回它，则用
    `doc_id + page + 内容哈希` 现算 —— 这与向量库的点键在语义上一致
    （同一段文字、同一页），因此跨路去重是可靠的。
    """
    key = str(hit.get("chunk_key") or "")
    if key:
        return key
    doc_id = str(hit.get("doc_id") or "")
    page = int(hit.get("page") or 0)
    return chunk_key(doc_id, content_hash(str(hit.get("text") or "")), page)


def rrf_fuse(
    routes: Sequence[tuple[str, Sequence[dict[str, Any]]]],
    *,
    k: int = DEFAULT_RRF_K,
    weights: dict[str, float] | None = None,
    top_k: int | None = None,
) -> list[FusedHit]:
    """把多路召回按 RRF 融合成一个排序。

    `routes` 是 ``[(route_name, hits), ...]``，每个 hits 已按该路自己的相关性降序。
    """
    if k < 1:
        raise ValueError("RRF 常数 k 必须 >= 1")
    weights = weights or {}
    merged: dict[str, FusedHit] = {}
    seen_route_keys: dict[str, set[str]] = {}

    for route_name, hits in routes:
        weight = float(weights.get(route_name, 1.0))
        if weight <= 0:
            continue
        seen = seen_route_keys.setdefault(route_name, set())
        rank = 0
        for hit in hits:
            key = hit_key(hit)
            # 同一路内部重复（不同后端可能返回同一段文字）只取最好名次
            if key in seen:
                continue
            seen.add(key)
            rank += 1
            item = merged.get(key)
            if item is None:
                item = FusedHit(
                    key=key,
                    doc_id=str(hit.get("doc_id") or ""),
                    doc_name=str(hit.get("doc_name") or ""),
                    page=int(hit.get("page") or 0),
                    text=str(hit.get("text") or ""),
                )
                merged[key] = item
            item.rrf_score += weight / (k + rank)
            item.routes.append(route_name)
            item.route_ranks[route_name] = rank
            item.scores[route_name] = float(hit.get("score") or 0.0)

    ordered = sorted(
        merged.values(),
        key=lambda h: (-h.rrf_score, min(h.route_ranks.values() or [10**9]), h.key),
    )
    return ordered[:top_k] if top_k else ordered


def rank_changes(before: Iterable[str], after: Sequence[str]) -> dict[str, Any]:
    """量化"重排到底改动了多少"，用于报告 Reranker 的真实收益（或没有收益）。"""
    before_list = list(before)
    position_before = {key: i + 1 for i, key in enumerate(before_list)}
    moved = 0
    deltas: list[int] = []
    for index, key in enumerate(after, start=1):
        old = position_before.get(key)
        if old is None:
            continue
        if old != index:
            moved += 1
            deltas.append(old - index)
    return {
        "before_top1": before_list[0] if before_list else None,
        "after_top1": after[0] if after else None,
        "top1_changed": bool(before_list and after and before_list[0] != after[0]),
        "moved": moved,
        "compared": len(after),
        "mean_rank_delta": round(sum(deltas) / len(deltas), 3) if deltas else 0.0,
    }
