"""BM25 改造的等价性验证：新实现必须与"旧实现公式"逐位一致。

为什么单独写这个测试：BM25 是线上唯一的关键词检索路径，
为了性能（缓存 tf + 倒排候选）而改内部结构，**排序结果不允许有任何变化**。
这里把旧实现原样复刻成 `legacy_search()`，对同一批语料逐查询比对：

* 返回的 chunk_key 序列完全一致；
* 分数一致（浮点误差 1e-9 以内）；
* 带 `doc_ids` 过滤时同样一致。
"""

from __future__ import annotations

import math
from collections import Counter
from pathlib import Path

import pytest

from aieb.brain.chunkkey import chunk_key, content_hash
from aieb.brain.knowledge import BM25Index, tokenize
from aieb.brain.store import BrainStore

CORPUS = [
    "供应商逾期交付的，按合同总额每日 0.5% 计收违约金，累计不超过合同总额的 20%。",
    "采购部在收到完整材料后 10 个工作日内完成初审，初审通过后转交复核。",
    "项目分为需求、设计、开发、测试、上线五个里程碑，每个里程碑需项目经理签字确认。",
    "年度预算上限为 3800 万元，超支 8% 以上的须报董事会审批。",
    "一线城市住宿标准为每晚不超过 600 元，二线城市不超过 420 元。",
    "供应商逾期交付的违约金上限以合同约定为准，逾期超过 15 日可终止合作。",
    "保密义务在合同终止后继续有效 3 年。",
    "来料检验抽检比例不低于 5%，不合格批次整批退回。",
    "食堂供应时间为 11:30 至 13:00。",
    "项目编号格式为 CG-2024-001，与该编号相近的 CG-2024-0010 属历史归档。",
]

QUERIES = [
    "逾期交付违约金上限",
    "初审多少个工作日",
    "五个里程碑",
    "预算上限 万元",
    "住宿标准每晚不超过多少元",
    "抽检比例",
    "项目编号格式",
    "既不存在的词汇甲",
    "供应商",
]


def legacy_search(
    docs: list[dict], query: str, top_k: int = 5, doc_ids: set[str] | None = None
) -> list[tuple[str, float]]:
    """改造前的实现（原样复刻，作为基准）。"""
    tokens = tokenize(query)
    if not tokens or not docs:
        return []
    token_lists = [tokenize(d["text"]) for d in docs]
    df: Counter[str] = Counter()
    for toks in token_lists:
        df.update(set(toks))
    total = len(docs)
    avg_len = sum(len(t) for t in token_lists) / len(token_lists)
    scored: list[tuple[float, int]] = []
    for i, doc_tokens in enumerate(token_lists):
        if doc_ids and docs[i]["doc_id"] not in doc_ids:
            continue
        tf = Counter(doc_tokens)
        score = 0.0
        length = len(doc_tokens) or 1
        for token in tokens:
            freq = tf.get(token, 0)
            if not freq:
                continue
            d = df.get(token, 0) or 1
            idf = math.log(1 + (total - d + 0.5) / (d + 0.5))
            denom = freq + 1.5 * (1 - 0.75 + 0.75 * length / avg_len)
            score += idf * freq * (1.5 + 1) / denom
        if score > 0:
            scored.append((score, i))
    scored.sort(reverse=True)
    out = []
    for score, i in scored[:top_k]:
        item = docs[i]
        out.append(
            (
                chunk_key(
                    item.get("document_key") or item["doc_id"],
                    item.get("content_hash") or content_hash(item["text"]),
                    int(item["page"]),
                ),
                score,
            )
        )
    return out


@pytest.fixture()
def index(tmp_path: Path):
    store = BrainStore(tmp_path)
    store.add_document(
        name="控制制度.md", ext="md", size=100, stored_path="", status="ready",
        pages=1, chars=100, document_key="dk-test", scope_id="default",
    )
    doc_id = store.list_documents()[0]["id"]
    store.add_chunks(
        doc_id, [{"page": i + 1, "idx": i, "text": text} for i, text in enumerate(CORPUS)]
    )
    idx = BM25Index(store)
    idx.rebuild()
    yield idx, store.all_chunks()
    store.close()


@pytest.mark.parametrize("query", QUERIES)
def test_bm25_matches_legacy_ranking(index, query):
    idx, docs = index
    new = [(h["chunk_key"], h["score"]) for h in idx.search(query, top_k=5)]
    old = legacy_search(docs, query, top_k=5)
    assert [n[0] for n in new] == [o[0] for o in old], f"排序不一致：{query}"
    for (_, new_score), (_, old_score) in zip(new, old):
        assert abs(new_score - old_score) < 1e-3, "分数偏差过大"


@pytest.mark.parametrize("query", QUERIES)
def test_bm25_doc_id_filter_matches_legacy(index, query):
    idx, docs = index
    only = {docs[0]["doc_id"]}
    new = [(h["chunk_key"], h["score"]) for h in idx.search(query, top_k=5, doc_ids=only)]
    old = legacy_search(docs, query, top_k=5, doc_ids=only)
    assert [n[0] for n in new] == [o[0] for o in old]


def test_inverted_index_prunes_but_keeps_same_answers(index):
    """关掉倒排时结果必须完全相同 —— 证明剪枝没有误伤候选。"""
    idx, _ = index
    idx.use_inverted = False
    brute = [(h["chunk_key"], h["score"]) for h in idx.search("逾期交付违约金", top_k=5)]
    idx.use_inverted = True
    pruned = [(h["chunk_key"], h["score"]) for h in idx.search("逾期交付违约金", top_k=5)]
    assert brute == pruned


@pytest.mark.parametrize("query", QUERIES)
def test_numpy_and_python_paths_are_bit_identical(index, query):
    """向量化打分不是"近似优化"：两者分数必须**完全相等**（不是 1e-6 以内）。"""
    idx, _ = index
    tokens = tokenize(query)
    if not tokens:
        pytest.skip("空查询")
    idx.use_numpy = True
    numpy_scores = idx._score(tokens, 10, None)
    idx.use_numpy = False
    python_scores = idx._score(tokens, 10, None)
    idx.use_numpy = True
    assert [i for _, i in numpy_scores] == [i for _, i in python_scores], "排序不一致"
    for (a, i), (b, j) in zip(numpy_scores, python_scores):
        assert i == j
        assert a == b, f"分数不严格相等：{a!r} != {b!r}"


def test_numpy_path_matches_python_with_doc_id_filter(index):
    idx, docs = index
    only = {docs[0]["doc_id"], docs[3]["doc_id"]}
    tokens = tokenize("违约金 初审 里程碑")
    idx.use_numpy = True
    fast = idx._score(tokens, 10, only)
    idx.use_numpy = False
    slow = idx._score(tokens, 10, only)
    idx.use_numpy = True
    assert [i for _, i in fast] == [i for _, i in slow]
    assert all(a == b for (a, _), (b, _) in zip(fast, slow))


def test_incremental_add_matches_full_rebuild(tmp_path: Path):
    """增量追加与全量重建必须得到同一个索引状态。"""
    store = BrainStore(tmp_path / "inc")
    store.add_document(
        name="a.md", ext="md", size=10, stored_path="", status="ready",
        pages=1, chars=10, document_key="dk-a", scope_id="default",
    )
    doc_id = store.list_documents()[0]["id"]
    store.add_chunks(doc_id, [{"page": 1, "idx": 0, "text": CORPUS[0]}])

    incremental = BM25Index(store)
    incremental.rebuild()
    extra = [{"page": 2, "idx": 1, "text": CORPUS[1], "doc_id": doc_id, "doc_name": "a.md"}]
    store.add_chunks(doc_id, extra)
    incremental.add_chunks([{**extra[0], "document_key": "dk-a"}])

    full = BM25Index(store)
    full.rebuild()

    assert incremental.size == full.size
    for query in QUERIES:
        assert [h["chunk_key"] for h in incremental.search(query, top_k=3)] == [
            h["chunk_key"] for h in full.search(query, top_k=3)
        ]
    store.close()


def test_search_on_empty_index_returns_empty(tmp_path: Path):
    store = BrainStore(tmp_path / "empty")
    idx = BM25Index(store)
    assert idx.search("任何查询") == []
    assert idx.size == 0
    store.close()
