"""RAG Top-K 消融实验：把 K 变成**真实的生产参数**，测量它对检索召回、
回答质量、引用准确性、延迟与 Token 成本的影响。

与 ``benchmarks/runners/rag_runner.py`` 的区别（这是本次实验的立身之本）：

1. ``rag_runner`` 用 ``top_k=5`` 固定值跑一次，只报告 Recall@1/3/5；
   本模块对每个 K 分别调用 ``BrainService.answer(question, top_k=K)``，
   **真正改变送进大模型的分块数量**，而不是只改评测器里的 @K 数字。
2. 同时记录「检索返回了几个分块」与「上下文预算实际放进去几个分块」
   （``build_context`` 有 6000 字符上限，K=10 时会被截断），二者必须分开报告。
3. Token 用 provider 真实回传的 usage（``StreamEnd.input_tokens/output_tokens``），
   不是按字符估算。

用法::

    # 只跑检索层（不花钱、完全可复现）
    python -m benchmarks.ablations.topk --mode fixture --k 1,3,5,10

    # 跑真实 LLM 问答（消耗调用次数）
    python -m benchmarks.ablations.topk --mode live --k 1,3,5,10 --max-calls 300

    # 小样本 Pilot
    python -m benchmarks.ablations.topk --mode live --k 1,3,5,10 --limit 5 --max-calls 20
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from benchmarks import common
from benchmarks.evaluators import rag as rag_eval
from benchmarks.evaluators.metrics import mean, ratio
from benchmarks.runners import rag_runner

#: 与 ``aieb.brain.knowledge.build_context`` 的默认值保持一致。
#: 这里显式写出来，是为了在报告里指出「K 与真正进 prompt 的分块数不是一回事」。
CONTEXT_MAX_CHARS = 6000

#: 数据集选择：name -> (rag_runner split, 说明)
DATASETS: dict[str, tuple[str, str]] = {
    "full": ("all", "全部 50 题（dev 36 + test_exposed 14），文档 DOC-A~DOC-D"),
    "dev": ("dev", "开发集 36 题（DOC-A~DOC-D）"),
    "test_exposed": ("test_exposed", "已暴露测试集 14 题（DOC-A~DOC-D）"),
    "unseen": ("test_unseen", "未暴露测试集 23 题（DOC-E/DOC-F 全新文档）"),
}

DEFAULT_KS = (1, 3, 5, 10)


# ---------------------------------------------------------------------------
# Token 用量采集（真实 usage，不是估算）
# ---------------------------------------------------------------------------


class UsageSink:
    """记录每次 LLM 调用由 provider 回传的 token 用量。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.totals: dict[str, int] = {
            "llm_calls": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_tokens": 0,
        }

    def reset(self) -> None:
        self.calls = []

    def record(self, input_tokens: int, output_tokens: int, cache_read: int = 0) -> None:
        entry = {
            "input_tokens": int(input_tokens or 0),
            "output_tokens": int(output_tokens or 0),
            "cache_read": int(cache_read or 0),
        }
        self.calls.append(entry)
        self.totals["llm_calls"] += 1
        self.totals["input_tokens"] += entry["input_tokens"]
        self.totals["output_tokens"] += entry["output_tokens"]
        self.totals["cache_read_tokens"] += entry["cache_read"]

    def summary(self) -> dict[str, Any]:
        """本次调用（或自上次 reset 以来）的用量。"""
        return {
            "llm_calls": len(self.calls),
            "input_tokens": sum(c["input_tokens"] for c in self.calls),
            "output_tokens": sum(c["output_tokens"] for c in self.calls),
            "cache_read_tokens": sum(c["cache_read"] for c in self.calls),
            # provider 没有回传 usage 时（个别兼容端点）如实标 false，不拿字符数冒充
            "usage_reported": any(c["input_tokens"] or c["cache_read"] for c in self.calls),
        }

    def overall(self) -> dict[str, Any]:
        """整个运行过程的累计用量（不受 reset 影响）。"""
        payload = dict(self.totals)
        payload["prompt_tokens"] = payload["input_tokens"] + payload["cache_read_tokens"]
        payload["usage_reported"] = bool(payload["input_tokens"] or payload["cache_read_tokens"])
        return payload


class _UsageProxy:
    """包住真实 client，只旁听 ``StreamEnd`` 里的 usage，不改变任何行为。"""

    def __init__(self, inner: Any, sink: UsageSink) -> None:
        self._inner = inner
        self._sink = sink

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def stream(self, *args: Any, **kwargs: Any):
        from aieb.tools.base import StreamEnd

        async for event in self._inner.stream(*args, **kwargs):
            if isinstance(event, StreamEnd):
                self._sink.record(
                    getattr(event, "input_tokens", 0),
                    getattr(event, "output_tokens", 0),
                    getattr(event, "cache_read", 0),
                )
            yield event


@contextmanager
def record_usage(sink: UsageSink) -> Iterator[None]:
    """给 ``aieb.brain.llm.create_client`` 套一层旁路记录器。

    只劫持 client 工厂，不替换 ``llm.complete``，因此被测链路与生产完全一致。
    """
    from aieb.brain import llm

    original = llm.create_client

    def factory(provider: Any) -> Any:
        return _UsageProxy(original(provider), sink)

    llm.create_client = factory  # type: ignore[assignment]
    try:
        yield
    finally:
        llm.create_client = original  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# 检索层
# ---------------------------------------------------------------------------


def context_blocks(hits: list[dict[str, Any]], max_chars: int = CONTEXT_MAX_CHARS) -> list[dict[str, Any]]:
    """复刻 ``build_context`` 的预算逻辑，返回**真正进 prompt** 的分块。

    与生产函数同源同逻辑；用它来回答「检索到 K 个 ≠ 模型看到 K 个」，
    并在 :func:`context_stats` 里与真实输出做一致性校验。
    """
    blocks: list[dict[str, Any]] = []
    used = 0
    for hit in hits:
        header = f"[来源：{hit['doc_name']} · 第{hit['page']}页]"
        block = f"{header}\n{hit['text'][:1200]}"
        if used + len(block) > max_chars:
            break
        blocks.append(hit)
        used += len(block)
    return blocks


def context_stats(hits: list[dict[str, Any]]) -> dict[str, Any]:
    """上下文预算诊断：citations 声称的页 vs 真正进 prompt 的页。"""
    from aieb.brain import knowledge

    text = knowledge.build_context(hits)
    blocks = context_blocks(hits)
    expected = "\n\n".join(
        f"[来源：{h['doc_name']} · 第{h['page']}页]\n{h['text'][:1200]}" for h in blocks
    )
    delivered_pairs = {(h["doc_id"], int(h["page"])) for h in blocks}
    return {
        "retrieved_chunks": len(hits),
        "delivered_chunks": len(blocks),
        "dropped_chunks": len(hits) - len(blocks),
        "context_chars": len(text),
        "delivered_pages": len(delivered_pairs),
        "block_mismatch": text != expected,
    }


def mrr_at(pairs: list[tuple[str, int]], gold: set[tuple[str, int]], k: int) -> float:
    """MRR@K。K 固定取最大检索范围（10），避免不同 K 用不同口径互相比较。"""
    for rank, pair in enumerate(pairs[:k], start=1):
        if pair in gold:
            return 1.0 / rank
    return 0.0


def score_retrieval_at(
    ranked_pairs: list[tuple[str, int]],
    gold: set[tuple[str, int]],
    k: int,
    alias: dict[str, str],
) -> dict[str, Any]:
    window = ranked_pairs[:k]
    covered = gold & set(window)

    def fix(ref: tuple[str, int]) -> str:
        return f"{alias.get(ref[0], ref[0])}:p{ref[1]}"

    return {
        "recall": round(len(covered) / len(gold), 4),
        "hit": 1.0 if covered else 0.0,
        "full_evidence": 1.0 if len(covered) == len(gold) else 0.0,
        "covered_pages": sorted(fix(p) for p in covered),
        "missing_pages": sorted(fix(p) for p in (gold - set(window))),
    }


def index_audit(service) -> dict[str, Any]:
    """索引结构审计：分块粒度、同页多块分布（回答"返回 chunk 还是 page"）。"""
    chunks = service.store.all_chunks()
    per_page: dict[tuple[str, int], int] = {}
    per_doc: dict[str, dict[str, Any]] = {}
    for chunk in chunks:
        key = (chunk["doc_id"], int(chunk["page"]))
        per_page[key] = per_page.get(key, 0) + 1
        doc = per_doc.setdefault(
            chunk["doc_id"],
            {"doc_name": chunk["doc_name"], "chunks": 0, "pages": set(), "chars": 0},
        )
        doc["chunks"] += 1
        doc["pages"].add(int(chunk["page"]))
        doc["chars"] += len(chunk["text"])

    histogram: dict[str, int] = {}
    for count in per_page.values():
        bucket = str(count) if count <= 3 else "4+"
        histogram[bucket] = histogram.get(bucket, 0) + 1

    return {
        "total_chunks": len(chunks),
        "total_pages_with_text": len(per_page),
        "chunks_per_page_max": max(per_page.values()) if per_page else 0,
        "chunks_per_page_histogram": dict(sorted(histogram.items())),
        "avg_chunk_chars": round(
            sum(len(c["text"]) for c in chunks) / len(chunks), 1
        )
        if chunks
        else 0.0,
        "documents": {
            doc_id: {
                "doc_name": info["doc_name"],
                "chunks": info["chunks"],
                "pages": len(info["pages"]),
                "chars": info["chars"],
            }
            for doc_id, info in sorted(per_doc.items())
        },
    }


def run_retrieval(
    service,
    questions: list[dict[str, Any]],
    mapping: dict[str, str],
    ks: list[int],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """检索层：一次取 ``max(ks)`` 个分块，再按 K 切片评估（同一排序，公平对比）。"""
    alias = {v: k for k, v in mapping.items()}
    max_k = max(ks)
    rows: list[dict[str, Any]] = []
    determinism_ok = True

    for raw in questions:
        question = rag_runner._resolve_question(raw, mapping)
        gold = rag_eval.gold_pairs(question)
        started = time.perf_counter()
        ranked = service.search(question["question"], top_k=max_k)
        search_ms = (time.perf_counter() - started) * 1000

        # 一致性校验：小 K 的结果必须是大 K 结果的前缀，否则 K 之间的对比不成立
        if max_k > 5:
            probe = service.search(question["question"], top_k=5)
            if [(h["doc_id"], h["page"]) for h in probe] != [
                (h["doc_id"], h["page"]) for h in ranked[:5]
            ]:
                determinism_ok = False

        ranked_pairs = [(h["doc_id"], int(h["page"])) for h in ranked]
        for k in ks:
            hits_k = ranked[:k]
            base = {
                "question_id": question["question_id"],
                "question_type": question["question_type"],
                "answerable": bool(question.get("answerable")),
                "difficulty": question["difficulty"],
                "k": k,
                "gold_pages": sorted(f"{alias.get(d, d)}:p{p}" for d, p in gold),
                "retrieved_chunks": len(hits_k),
                "retrieved_distinct_pages": len(set(ranked_pairs[:k])),
                "search_ms": round(search_ms, 2),
            }
            stats = context_stats(hits_k)
            base.update({f"ctx_{key}": value for key, value in stats.items()})
            if gold:
                base.update(score_retrieval_at(ranked_pairs, gold, k, alias))
                base["mrr@10"] = round(mrr_at(ranked_pairs, gold, max_k), 4)
            else:
                base.update(
                    {
                        "recall": None,
                        "hit": None,
                        "full_evidence": None,
                        "mrr@10": None,
                        "covered_pages": [],
                        "missing_pages": [],
                        "negative_retrieved_any": bool(ranked_pairs[:k]),
                    }
                )
            rows.append(base)

    aggregate = aggregate_retrieval(rows, ks)
    aggregate["ranking_deterministic"] = determinism_ok
    return rows, aggregate


def aggregate_retrieval(rows: list[dict[str, Any]], ks: list[int]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k in ks:
        items = [r for r in rows if r["k"] == k]
        positives = [r for r in items if r["answerable"]]
        negatives = [r for r in items if not r["answerable"]]
        out[str(k)] = {
            "questions": len(items),
            "answerable_questions": len(positives),
            "negative_questions": len(negatives),
            "recall": mean(r["recall"] for r in positives),
            "hit": mean(r["hit"] for r in positives),
            "full_evidence_rate": mean(r["full_evidence"] for r in positives),
            "mrr@10": mean(r["mrr@10"] for r in positives),
            "multi_evidence_questions": sum(1 for r in positives if len(r["gold_pages"]) > 1),
            "avg_retrieved_chunks": mean(r["retrieved_chunks"] for r in items),
            "avg_delivered_chunks": mean(r["ctx_delivered_chunks"] for r in items),
            "avg_context_chars": mean(r["ctx_context_chars"] for r in items),
            "truncated_questions": sum(1 for r in items if r["ctx_dropped_chunks"] > 0),
            "avg_search_ms": mean(r["search_ms"] for r in items),
            "negative_retrieved_any_rate": mean(
                1.0 if r.get("negative_retrieved_any") else 0.0 for r in negatives
            )
            if negatives
            else None,
        }
    return out


# ---------------------------------------------------------------------------
# 问答层
# ---------------------------------------------------------------------------


async def run_qa(
    service,
    questions: list[dict[str, Any]],
    mapping: dict[str, str],
    ks: list[int],
    *,
    split: str = "all",
    repeat: int = 1,
    sink: UsageSink | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    from aieb.brain import knowledge

    specs, _doc_dir = rag_runner.document_specs(split)
    alias = {v: k for k, v in mapping.items()}
    name_index = {
        rag_eval.name_key(spec["filename"]): mapping[doc_id]
        for doc_id, spec in specs.items()
        if doc_id in mapping
    }
    sink = sink or UsageSink()
    rows: list[dict[str, Any]] = []
    max_k = max(ks)
    total = len(questions) * len(ks) * max(1, repeat)
    done = 0

    for raw in questions:
        question = rag_runner._resolve_question(raw, mapping)
        ranked = service.search(question["question"], top_k=max_k)
        for k in ks:
            hits_k = ranked[:k]
            stats = context_stats(hits_k)
            context_text = knowledge.build_context(hits_k)
            for round_index in range(1, max(1, repeat) + 1):
                done += 1
                sink.reset()
                started = time.perf_counter()
                error = ""
                try:
                    response = await service.answer(question["question"], top_k=k)
                    payload = rag_eval.score_answer(
                        question, response, context_text, doc_index=name_index
                    )
                except common.BudgetExceeded as exc:
                    rows.append(
                        {
                            "question_id": question["question_id"],
                            "k": k,
                            "round": round_index,
                            "executed": False,
                            "error": f"budget: {exc}",
                            "answerable": bool(question.get("answerable")),
                            "question_type": question["question_type"],
                        }
                    )
                    raise
                except Exception as exc:  # noqa: BLE001 - 失败样本留在分母里
                    elapsed = (time.perf_counter() - started) * 1000
                    error = f"{type(exc).__name__}: {exc}"
                    payload = {
                        "executed": True,
                        "answer": "",
                        "citations": [],
                        "answer_correct": False,
                        "correct": False,
                        "citation_precision": 0.0,
                        "citation_recall": 0.0,
                        "citation_count": 0,
                        "strict_citation_ok": False,
                        "groundedness": {"rate": 0.0, "total": 0, "unsupported": []},
                        "refusal": False,
                        "degraded": False,
                        "missing_keys": [],
                        "forbidden_hits": [],
                        "error": error,
                    }
                    usage = sink.summary()
                    rows.append(
                        _qa_row(question, k, round_index, stats, payload, usage, elapsed, error)
                    )
                    continue
                elapsed = (time.perf_counter() - started) * 1000
                rows.append(
                    _qa_row(
                        question,
                        k,
                        round_index,
                        stats,
                        payload,
                        sink.summary(),
                        elapsed,
                        error,
                    )
                )
                mark = "ok" if payload.get("answer_correct") else "x"
                print(
                    f"  [{done}/{total}] {question['question_id']} K={k:<2} "
                    f"r{round_index} ctx={stats['delivered_chunks']}/{stats['retrieved_chunks']}块 "
                    f"in={sink.summary()['input_tokens']}tok "
                    f"{elapsed:.0f}ms ans={mark}",
                    flush=True,
                )
    return rows, aggregate_qa(rows, ks)


def _qa_row(
    question: dict[str, Any],
    k: int,
    round_index: int,
    stats: dict[str, Any],
    payload: dict[str, Any],
    usage: dict[str, Any],
    elapsed_ms: float,
    error: str,
) -> dict[str, Any]:
    return {
        "question_id": question["question_id"],
        "question_type": question["question_type"],
        "answerable": bool(question.get("answerable")),
        "difficulty": question["difficulty"],
        "k": k,
        "round": round_index,
        "executed": bool(payload.get("executed")),
        "retrieved_chunks": stats["retrieved_chunks"],
        "delivered_chunks": stats["delivered_chunks"],
        "dropped_chunks": stats["dropped_chunks"],
        "context_chars": stats["context_chars"],
        "answer_correct": bool(payload.get("answer_correct")),
        "correct": bool(payload.get("correct")),
        "citation_precision": payload.get("citation_precision") or 0.0,
        "citation_recall": payload.get("citation_recall"),
        "citation_count": payload.get("citation_count") or 0,
        "strict_citation_ok": bool(payload.get("strict_citation_ok")),
        "groundedness": (payload.get("groundedness") or {}).get("rate", 0.0),
        "refusal": bool(payload.get("refusal")),
        "degraded": bool(payload.get("degraded")),
        "missing_keys": payload.get("missing_keys") or [],
        "forbidden_hits": payload.get("forbidden_hits") or [],
        "answer": common.redact(str(payload.get("answer") or "")),
        "citations": payload.get("citations") or [],
        "input_tokens": usage["input_tokens"],
        "cache_read_tokens": usage.get("cache_read_tokens", 0),
        # provider 上报的口径：prompt_tokens 包含缓存命中部分，
        # 而 StreamEnd.input_tokens 已减掉缓存；两者相加才是真实上下文规模。
        "prompt_tokens": usage["input_tokens"] + usage.get("cache_read_tokens", 0),
        "output_tokens": usage["output_tokens"],
        "usage_reported": usage["usage_reported"],
        "answer_ms": round(elapsed_ms, 1),
        "error": error,
    }


def aggregate_qa(rows: list[dict[str, Any]], ks: list[int]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k in ks:
        items = [r for r in rows if r["k"] == k and r.get("executed")]
        positives = [r for r in items if r["answerable"]]
        negatives = [r for r in items if not r["answerable"]]
        out[str(k)] = {
            "executed": len(items),
            "answerable_executed": len(positives),
            "negative_executed": len(negatives),
            "answer_accuracy": mean(1.0 if r["answer_correct"] else 0.0 for r in positives)
            if positives
            else None,
            "abstention_accuracy": mean(1.0 if r["answer_correct"] else 0.0 for r in negatives)
            if negatives
            else None,
            "end_to_end": mean(1.0 if r["correct"] else 0.0 for r in items) if items else None,
            "citation_precision": mean(r["citation_precision"] for r in positives)
            if positives
            else None,
            "citation_recall": mean(r["citation_recall"] or 0.0 for r in positives)
            if positives
            else None,
            "strict_citation_accuracy": mean(
                1.0 if r["strict_citation_ok"] else 0.0 for r in positives
            )
            if positives
            else None,
            "citation_missing_rate": mean(
                1.0 if (r["citation_count"] or 0) == 0 else 0.0 for r in positives
            )
            if positives
            else None,
            "groundedness": mean(r["groundedness"] for r in items) if items else None,
            "avg_input_tokens": mean(r["input_tokens"] for r in items) if items else None,
            "avg_prompt_tokens": mean(r.get("prompt_tokens", 0) for r in items) if items else None,
            "avg_output_tokens": mean(r["output_tokens"] for r in items) if items else None,
            "avg_cache_read_tokens": mean(r.get("cache_read_tokens", 0) for r in items)
            if items
            else None,
            "total_input_tokens": sum(r["input_tokens"] for r in items),
            "total_prompt_tokens": sum(r.get("prompt_tokens", 0) for r in items),
            "total_cache_read_tokens": sum(r.get("cache_read_tokens", 0) for r in items),
            "avg_delivered_chunks": mean(r["delivered_chunks"] for r in items) if items else None,
            "avg_context_chars": mean(r["context_chars"] for r in items) if items else None,
            "answer_p50_ms": common.percentile([r["answer_ms"] for r in items], 50) if items else None,
            "answer_p95_ms": common.percentile([r["answer_ms"] for r in items], 95) if items else None,
            "errors": sum(1 for r in items if r.get("error")),
            "degraded": sum(1 for r in items if r["degraded"]),
        }
    return out


def by_type(rows: list[dict[str, Any]], ks: list[int]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k in ks:
        bucket: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            if row["k"] != k or not row.get("executed"):
                continue
            bucket.setdefault(row["question_type"], []).append(row)
        out[str(k)] = {
            name: {
                "executed": len(items),
                "answer_accuracy": mean(
                    1.0 if r["answer_correct"] else 0.0 for r in items if r["answerable"]
                )
                if any(r["answerable"] for r in items)
                else None,
                "end_to_end": mean(1.0 if r["correct"] else 0.0 for r in items),
            }
            for name, items in sorted(bucket.items())
        }
    return out


def stability(rows: list[dict[str, Any]], ks: list[int]) -> dict[str, Any]:
    """多轮重复下的波动：报告每轮数值与极差，不挑最好的一轮。"""
    out: dict[str, Any] = {}
    rounds = sorted({r.get("round", 1) for r in rows})
    for k in ks:
        per_round: list[dict[str, Any]] = []
        for round_index in rounds:
            items = [
                r
                for r in rows
                if r["k"] == k and r.get("round", 1) == round_index and r.get("executed")
            ]
            positives = [r for r in items if r["answerable"]]
            if not items:
                continue
            per_round.append(
                {
                    "round": round_index,
                    "answer_accuracy": mean(
                        1.0 if r["answer_correct"] else 0.0 for r in positives
                    )
                    if positives
                    else None,
                    "end_to_end": mean(1.0 if r["correct"] else 0.0 for r in items),
                    "citation_precision": mean(r["citation_precision"] for r in positives)
                    if positives
                    else None,
                }
            )
        values = [p["answer_accuracy"] for p in per_round if p["answer_accuracy"] is not None]
        out[str(k)] = {
            "rounds": len(per_round),
            "per_round": per_round,
            "answer_accuracy_range": round(max(values) - min(values), 4) if len(values) > 1 else 0.0,
            "answer_accuracy_mean": round(sum(values) / len(values), 4) if values else None,
        }
    return out


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

RETRIEVAL_COLUMNS = [
    "question_id",
    "question_type",
    "difficulty",
    "answerable",
    "k",
    "gold_pages",
    "recall",
    "hit",
    "full_evidence",
    "mrr@10",
    "retrieved_chunks",
    "retrieved_distinct_pages",
    "ctx_delivered_chunks",
    "ctx_dropped_chunks",
    "ctx_context_chars",
    "covered_pages",
    "missing_pages",
    "search_ms",
]

QA_COLUMNS = [
    "question_id",
    "question_type",
    "difficulty",
    "answerable",
    "k",
    "round",
    "answer_correct",
    "correct",
    "citation_precision",
    "citation_recall",
    "citation_count",
    "strict_citation_ok",
    "groundedness",
    "refusal",
    "delivered_chunks",
    "context_chars",
    "input_tokens",
    "cache_read_tokens",
    "prompt_tokens",
    "output_tokens",
    "answer_ms",
    "missing_keys",
    "forbidden_hits",
    "answer",
    "citations",
    "error",
]


async def run(
    ctx: common.RunContext,
    *,
    dataset: str = "full",
    ks: list[int] | None = None,
    limit: int = 0,
    only: list[str] | None = None,
    repeat: int = 1,
    tag: str = "",
    sink: UsageSink | None = None,
) -> dict[str, Any]:
    from aieb.brain.service import BrainService

    ks = sorted(set(ks or DEFAULT_KS))
    split = DATASETS[dataset][0]
    questions = rag_runner.load_questions(limit=limit, only=only, split=split)
    if not questions:
        raise RuntimeError(f"dataset={dataset} 没有匹配题目")

    workspace = ctx.workspace("topk")
    service = BrainService(work_dir=workspace, provider=ctx.provider)
    try:
        mapping = rag_runner._index_documents(service, ctx, split)
        audit = index_audit(service)

        print(f"  索引：{audit['total_chunks']} 个分块 / {audit['total_pages_with_text']} 页，"
              f"单页最多 {audit['chunks_per_page_max']} 块，平均 {audit['avg_chunk_chars']} 字符/块")
        print(f"  题目：{len(questions)} 条，K={ks}，重复 {repeat} 轮，split={split}")

        retrieval_rows, retrieval_agg = run_retrieval(service, questions, mapping, ks)

        qa_rows: list[dict[str, Any]] = []
        qa_agg: dict[str, Any] = {}
        type_agg: dict[str, Any] = {}
        stab: dict[str, Any] = {}
        if ctx.mode == "live":
            qa_rows, qa_agg = await run_qa(
                service, questions, mapping, ks, split=split, repeat=repeat, sink=sink
            )
            type_agg = by_type(qa_rows, ks)
            if repeat > 1:
                stab = stability(qa_rows, ks)
    finally:
        service.store.close()

    return {
        "dataset": dataset,
        "tag": tag or dataset,
        "split": split,
        "ks": ks,
        "repeat": repeat,
        "question_count": len(questions),
        "question_ids": [q["question_id"] for q in questions],
        "index_audit": audit,
        "retrieval": {"rows": retrieval_rows, "aggregates": retrieval_agg},
        "qa": {"rows": qa_rows, "aggregates": qa_agg, "by_type": type_agg, "stability": stab},
        "mode": ctx.mode,
    }


def _print_table(result: dict[str, Any]) -> None:
    ks = result["ks"]
    print()
    print("=" * 96)
    print(f"检索层（dataset={result['dataset']}，n={result['question_count']}）")
    print("=" * 96)
    head = f"{'K':>3} {'Recall':>7} {'Hit':>6} {'全证据':>7} {'MRR@10':>7} " \
           f"{'进prompt块':>10} {'上下文字符':>10} {'截断题数':>8} {'检索ms':>7}"
    print(head)
    for k in ks:
        row = result["retrieval"]["aggregates"][str(k)]
        print(
            f"{k:>3} {row['recall']:>7.3f} {row['hit']:>6.3f} {row['full_evidence_rate']:>7.3f} "
            f"{row['mrr@10']:>7.3f} {row['avg_delivered_chunks']:>10.1f} "
            f"{row['avg_context_chars']:>10.0f} {row['truncated_questions']:>8} "
            f"{row['avg_search_ms']:>7.1f}"
        )

    qa = result["qa"]["aggregates"]
    if not qa:
        print("\n（fixture 模式：不跑问答层）")
        return
    print()
    print("=" * 96)
    print("问答层（真实 LLM）")
    print("=" * 96)
    print(
        f"{'K':>3} {'答对率':>7} {'拒答率':>7} {'端到端':>7} {'引用P':>7} {'引用R':>7} "
        f"{'依据度':>7} {'prompttok':>9} {'未缓存':>7} {'输出tok':>8} {'P50ms':>7} {'P95ms':>7}"
    )
    for k in ks:
        row = qa[str(k)]
        if not row.get("executed"):
            print(f"{k:>3} （未执行）")
            continue

        def fmt(value: Any, digits: int = 3) -> str:
            return f"{value:.{digits}f}" if isinstance(value, (int, float)) else "  -  "

        print(
            f"{k:>3} {fmt(row['answer_accuracy'])} {fmt(row['abstention_accuracy']):>7} "
            f"{fmt(row['end_to_end'])} {fmt(row['citation_precision'])} "
            f"{fmt(row['citation_recall'])} {fmt(row['groundedness'])} "
            f"{row['avg_prompt_tokens']:>9.0f} {row['avg_input_tokens']:>7.0f} "
            f"{row['avg_output_tokens']:>8.0f} "
            f"{row['answer_p50_ms']:>7.0f} {row['answer_p95_ms']:>7.0f}"
        )


def write_outputs(result: dict[str, Any], ctx: common.RunContext, out_dir: Path) -> dict[str, Path]:
    tag = result.get("tag") or result["dataset"]
    paths: dict[str, Path] = {}
    retrieval_rows = result["retrieval"]["rows"]
    paths["retrieval_csv"] = common.write_csv(
        out_dir / f"topk_{tag}_retrieval_cases.csv", retrieval_rows, RETRIEVAL_COLUMNS
    )
    paths["retrieval_json"] = common.write_json(
        out_dir / f"topk_{tag}_retrieval_summary.json",
        {
            "meta": ctx.meta(),
            "dataset": result["dataset"],
            "split": result["split"],
            "ks": result["ks"],
            "question_count": result["question_count"],
            "question_ids": result["question_ids"],
            "index_audit": result["index_audit"],
            "aggregates": result["retrieval"]["aggregates"],
        },
    )
    if result["qa"]["rows"]:
        paths["qa_csv"] = common.write_csv(
            out_dir / f"topk_{tag}_qa_cases.csv", result["qa"]["rows"], QA_COLUMNS
        )
        paths["qa_json"] = common.write_json(
            out_dir / f"topk_{tag}_qa_summary.json",
            {
                "meta": ctx.meta(),
                "dataset": result["dataset"],
                "split": result["split"],
                "ks": result["ks"],
                "repeat": result["repeat"],
                "question_count": result["question_count"],
                "question_ids": result["question_ids"],
                "index_audit": result["index_audit"],
                "aggregates": result["qa"]["aggregates"],
                "by_type": result["qa"]["by_type"],
                "stability": result["qa"]["stability"],
            },
        )
    return paths


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m benchmarks.ablations.topk",
        description="RAG Top-K 消融实验（真实改变送进模型的分块数量）",
    )
    parser.add_argument("--mode", default="live", choices=("fixture", "live"))
    parser.add_argument("--dataset", default="full", choices=tuple(DATASETS))
    parser.add_argument("--k", default="1,3,5,10", help="逗号分隔的 K 列表")
    parser.add_argument("--max-calls", type=int, default=0, dest="max_calls")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--only", default="")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--output", default="")
    parser.add_argument("--tag", default="", help="结果文件前缀（默认用 --dataset）")
    parser.add_argument("--run-id", default="", dest="run_id")
    return parser


async def main_async(args: argparse.Namespace) -> int:
    ks = [int(x) for x in str(args.k).replace(" ", "").split(",") if x]
    out_dir = Path(args.output).resolve() if args.output else (common.RESULTS_DIR / "topk")
    out_dir.mkdir(parents=True, exist_ok=True)

    ctx = common.prepare_run(
        suite=f"topk-{args.dataset}",
        mode=args.mode,
        max_calls=args.max_calls,
        run_id=args.run_id,
    )
    sink = UsageSink()
    print("=" * 96)
    print("RAG Top-K 消融实验")
    print("=" * 96)
    print(f"  运行 ID : {ctx.run_id}")
    print(f"  数据集  : {args.dataset} —— {DATASETS[args.dataset][1]}")
    print(f"  K 取值  : {ks}")
    print(f"  模式    : {ctx.mode}   模型：{ctx.model.model}")
    print(f"  Git     : {ctx.git.get('commit_short')}{' (dirty)' if ctx.git.get('dirty') else ''}")
    print(f"  调用上限: {ctx.budget.max_calls or '不限'}")
    print(f"  .aieb/  : {ctx.demo_guard['files']} 个文件，指纹 {ctx.demo_guard['digest']}")
    print("=" * 96)

    exit_code = 0
    result: dict[str, Any] = {}
    try:
        with common.patch_llm_counter(ctx.budget), record_usage(sink):
            result = await run(
                ctx,
                dataset=args.dataset,
                ks=ks,
                limit=args.limit,
                only=[x.strip() for x in args.only.split(",") if x.strip()] or None,
                repeat=max(1, args.repeat),
                tag=args.tag,
                sink=sink,
            )
    except common.BudgetExceeded as exc:
        print(f"  ! 预算耗尽：{exc}")
        exit_code = 1

    if result:
        _print_table(result)
        paths = write_outputs(result, ctx, out_dir)
        for kind, path in paths.items():
            print(f"  {kind}: {path}")

    print()
    print("=" * 96)
    print(f"  LLM 调用：{ctx.budget.used} / {ctx.budget.max_calls or '不限'}（失败 {ctx.budget.failures}）")
    usage = sink.overall()
    print(f"  Token   : prompt {usage['prompt_tokens']}（其中缓存命中 "
          f"{usage['cache_read_tokens']}）/ 输出 {usage['output_tokens']}"
          f"（provider 回传 usage：{'是' if usage['usage_reported'] else '否'}）")
    unchanged = common.demo_env_unchanged(ctx.demo_guard)
    print(f"  正式 .aieb/ 未被改动：{'是' if unchanged else '否 —— 请立即排查！'}")
    if not unchanged:
        exit_code = 2
    manifest = out_dir / f"topk_{args.tag or args.dataset}_manifest.json"
    common.write_json(
        manifest,
        {
            "meta": ctx.meta(),
            "dataset": args.dataset,
            "ks": ks,
            "repeat": max(1, args.repeat),
            "mode": args.mode,
            "budget": ctx.budget.as_dict(),
            "usage": sink.summary(),
            "demo_env_unchanged": unchanged,
            "status": "ok" if exit_code == 0 else "error",
        },
    )
    print(f"  manifest: {manifest}")
    print("=" * 96)
    return exit_code


def main() -> int:
    args = build_parser().parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
