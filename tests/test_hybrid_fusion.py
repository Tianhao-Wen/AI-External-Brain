"""混合检索的确定性单测：RRF 融合、上下文预算、配置校验、内容标识。

这些用例**不依赖模型、不依赖 Qdrant**，所以在任何环境都能跑，
用来锁住"融合逻辑与预算逻辑"这类最容易悄悄写错的部分。
"""

from __future__ import annotations

import pytest

from aieb.brain.chunkkey import chunk_key, content_hash, document_key
from aieb.brain.retrieval.config import RetrievalConfig
from aieb.brain.retrieval.context import build_budgeted_context, estimate_tokens
from aieb.brain.retrieval.fusion import hit_key, rank_changes, rrf_fuse


def _hit(doc_id: str, page: int, text: str, score: float = 1.0) -> dict:
    return {
        "doc_id": doc_id,
        "doc_name": f"{doc_id}.pdf",
        "page": page,
        "text": text,
        "score": score,
        "chunk_key": chunk_key(doc_id, content_hash(text), page),
    }


# ---------------------------------------------------------------------------
# RRF
# ---------------------------------------------------------------------------


def test_rrf_uses_rank_not_raw_scores():
    """RRF 只看名次：把 BM25 分数放大 1000 倍，融合结果不应改变。"""
    a = [_hit("doc1", 1, "甲"), _hit("doc2", 1, "乙")]
    b = [_hit("doc2", 1, "乙"), _hit("doc3", 1, "丙")]

    base = rrf_fuse([("bm25", a), ("vector", b)], k=60)
    scaled = rrf_fuse(
        [("bm25", [dict(h, score=h["score"] * 1000) for h in a]), ("vector", b)], k=60
    )
    assert [h.key for h in base] == [h.key for h in scaled]


def test_rrf_favours_documents_found_by_both_routes():
    bm25 = [_hit("doc1", 1, "甲"), _hit("doc2", 1, "乙")]
    vector = [_hit("doc2", 1, "乙"), _hit("doc3", 1, "丙")]
    fused = rrf_fuse([("bm25", bm25), ("vector", vector)], k=60)
    assert fused[0].doc_id == "doc2", "两路都命中的文档应排第一"
    assert set(fused[0].routes) == {"bm25", "vector"}


def test_rrf_deduplicates_same_chunk_across_routes():
    same = _hit("doc1", 2, "同一段文字")
    fused = rrf_fuse([("bm25", [same]), ("vector", [dict(same)])], k=60)
    assert len(fused) == 1
    assert fused[0].route_ranks == {"bm25": 1, "vector": 1}


def test_rrf_smaller_k_rewards_single_route_top_ranked():
    """k 越小，越奖励"在某一路排第一"的文档；k 越大，越奖励"两路都靠前"。"""
    bm25 = [_hit("doc1", 1, "甲"), _hit("docx", 1, "杂项"), _hit("doc2", 1, "乙")]
    vector = [_hit("docy", 1, "其他"), _hit("docz", 1, "更多"), _hit("doc2", 1, "乙")]
    smooth = rrf_fuse([("bm25", bm25), ("vector", vector)], k=60)
    sharp = rrf_fuse([("bm25", bm25), ("vector", vector)], k=1)
    # k=1：doc1 = 1/1 = 1.0 > doc2 = 1/3 + 1/3 ≈ 0.667
    # k=60：doc1 = 1/61 ≈ 0.0164 < doc2 = 2/63 ≈ 0.0317
    assert sharp[0].doc_id == "doc1"
    assert smooth[0].doc_id == "doc2"


def test_rrf_rejects_invalid_k():
    with pytest.raises(ValueError):
        rrf_fuse([("bm25", [])], k=0)


def test_hit_key_falls_back_to_content_hash():
    bare = {"doc_id": "d1", "page": 3, "text": "没有 chunk_key 的命中"}
    assert hit_key(bare) == hit_key({**bare, "chunk_key": ""})
    assert hit_key(bare) != hit_key({"doc_id": "d1", "page": 4, "text": "没有 chunk_key 的命中"})


def test_rank_changes_reports_actual_movement():
    changes = rank_changes(["a", "b", "c"], ["c", "a", "b"])
    assert changes["top1_changed"] is True
    assert changes["moved"] == 3
    assert changes["before_top1"] == "a" and changes["after_top1"] == "c"


# ---------------------------------------------------------------------------
# 上下文预算
# ---------------------------------------------------------------------------


def test_estimate_tokens_is_monotonic_and_never_zero_for_text():
    assert estimate_tokens("") == 0
    assert estimate_tokens("短") >= 1
    assert estimate_tokens("长" * 100) > estimate_tokens("长" * 10)


def test_budgeted_context_reports_four_layers():
    hits = [_hit("doc1", i, f"第{i}页的内容" * 40) for i in range(1, 6)]
    bundle = build_budgeted_context(hits, max_tokens=200, chars_per_token=1.74)
    assert bundle.retrieved == 5
    assert bundle.selected == 5
    assert bundle.included >= 1
    assert bundle.included <= bundle.selected
    assert bundle.estimated_tokens <= 200
    assert bundle.dropped + bundle.included == bundle.selected


def test_budgeted_context_dedupes_identical_text():
    dup = _hit("doc1", 1, "完全重复的一段话")
    bundle = build_budgeted_context([dup, dict(dup), dup], max_tokens=500)
    assert bundle.retrieved == 3
    assert bundle.deduped == 2
    assert bundle.included == 1


def test_budgeted_context_does_not_early_break_on_long_block():
    """旧实现遇到放不下的块就 break，后面的短块也进不来；新实现会继续尝试。"""
    long_block = _hit("doc1", 1, "长" * 4000)
    short_block = _hit("doc2", 1, "短" * 20)
    bundle = build_budgeted_context(
        [long_block, short_block],
        max_tokens=120,
        chars_per_token=1.74,
        truncate_to_fit=False,
    )
    assert bundle.included == 1
    assert bundle.blocks[0]["doc_name"] == "doc2.pdf", (
        "长块放不下时应跳过并继续放后续短块，而不是直接中止"
    )
    assert any(item["reason"] == "too_long" for item in bundle.skipped)


def test_budgeted_context_respects_char_budget_too():
    hits = [_hit("doc1", i, "内容" * 300) for i in range(1, 4)]
    bundle = build_budgeted_context(hits, max_tokens=5000, max_chars=600)
    assert bundle.chars <= 600


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------


def test_config_defaults_to_bm25(monkeypatch):
    for key in ("AIEB_RETRIEVAL_MODE", "AIEB_CONTEXT_BUDGET"):
        monkeypatch.delenv(key, raising=False)
    cfg = RetrievalConfig.from_env()
    assert cfg.mode == "bm25", "未显式启用时不得改变生产默认行为"
    assert cfg.context_budget == "legacy"


def test_config_rejects_unknown_mode():
    with pytest.raises(ValueError):
        RetrievalConfig(mode="semantic-magic").validate()


def test_config_hides_api_key_in_dict():
    cfg = RetrievalConfig(mode="hybrid", qdrant_api_key="super-secret")
    assert cfg.as_dict()["qdrant_api_key"] == "***"
    assert "super-secret" not in str(cfg.as_dict())


# ---------------------------------------------------------------------------
# 内容标识
# ---------------------------------------------------------------------------


def test_content_hash_is_stable_across_whitespace_noise():
    assert content_hash("第十二条 供应商准入") == content_hash("  第十二条 供应商准入 \n")


def test_content_hash_differs_for_different_text():
    assert content_hash("甲") != content_hash("乙")


def test_document_key_depends_on_bytes_not_filename():
    payload = b"%PDF-1.4 same bytes"
    assert document_key(payload) == document_key(payload)
    assert document_key(payload) != document_key(b"%PDF-1.4 other bytes")


def test_chunk_key_separates_pages():
    h = content_hash("同一条款重复出现在两页")
    assert chunk_key("dk", h, 3) != chunk_key("dk", h, 4)
    assert chunk_key("dk", h, 3) == f"dk:3:{h}"
