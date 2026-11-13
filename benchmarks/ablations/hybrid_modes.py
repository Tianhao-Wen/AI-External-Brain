"""四组检索模式消融：BM25 / Vector / Hybrid(RRF) / Hybrid+Rerank。

公平性约束（这是消融实验的全部价值所在）：

* **同一批文档**（100 份 / 500 页）、**同一批题**、**同一套 gold 页**；
* **同一份上下文预算**：所有模式都用 `build_budgeted_context`，同样 1800 tokens，
  避免"某模式因为多塞了片段而显得答得好"；
* **同一个 LLM、同一段 prompt**（`ANSWER_SYSTEM` 原样复用）；
* **同一套评测器**（`benchmarks/evaluators/rag.py`，与既有 RAG 套件一致）；
* 每条查询都记录 P50/P95、候选数、返回数、以及"是否降级"。

用法：
    # 只跑检索层（不花钱）
    python -m benchmarks.ablations.hybrid_modes --skip-qa
    # 检索 + 真实问答（默认 dev 切分的前 N 题）
    python -m benchmarks.ablations.hybrid_modes --qa-limit 60 --max-calls 260
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import time
from collections import Counter
from pathlib import Path
from typing import Any

from benchmarks import common
from benchmarks.ablations.topk import mrr_at
from benchmarks.evaluators import rag as rag_eval
from benchmarks.evaluators.metrics import mean

DATASET_DIR = common.DATASETS_DIR / "hybrid_rag"
DOC_DIR = DATASET_DIR / "documents"
QUESTIONS_PATH = DATASET_DIR / "questions.jsonl"
SPLITS_PATH = DATASET_DIR / "splits.json"
HOLDOUT_DIR = DATASET_DIR / "holdout"
HOLDOUT_DOC_DIR = HOLDOUT_DIR / "documents"
HOLDOUT_QUESTIONS_PATH = HOLDOUT_DIR / "questions.jsonl"
HOLDOUT_FREEZE_PATH = HOLDOUT_DIR / "FREEZE.json"
OUT_DIR = common.RESULTS_DIR / "hybrid_rag"

MODES = ("bm25", "vector", "hybrid", "hybrid_rerank")
KS = (1, 3, 5, 10)


# ---------------------------------------------------------------------------
# 数据装载
# ---------------------------------------------------------------------------


def verify_freeze(path: Path = HOLDOUT_QUESTIONS_PATH, freeze_path: Path = HOLDOUT_FREEZE_PATH) -> dict[str, Any]:
    """校验留出集未被改动：sha256 必须与 FREEZE.json 一致。"""
    import hashlib

    freeze = json.loads(freeze_path.read_text(encoding="utf-8"))
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    expected = freeze.get("questions_sha256")
    if digest != expected:
        raise RuntimeError(
            f"留出集已被修改！{path.name} sha256={digest} != 冻结值 {expected}"
        )
    if freeze.get("used_for_tuning") is not False:
        raise RuntimeError("FREEZE.json 声明该集合用于调参，不能当作独立评测集使用")
    return freeze


def load_questions(split: str = "all", limit: int = 0, holdout: bool = False) -> list[dict[str, Any]]:
    if holdout:
        rows = common.read_jsonl(HOLDOUT_QUESTIONS_PATH)
        if split != "all":
            rows = [r for r in rows if r.get("split") == split]
        if limit:
            rows = rows[:limit]
        return rows
    rows = common.read_jsonl(QUESTIONS_PATH)
    if split != "all":
        splits = json.loads(SPLITS_PATH.read_text(encoding="utf-8"))["splits"]
        allowed = set(splits.get(split) or [])
        rows = [r for r in rows if r["question_id"] in allowed]
    if limit:
        rows = rows[:limit]
    return rows


def index_corpus(ctx, service, doc_dir: Path | None = None) -> dict[str, Any]:
    """把 100 份 PDF 通过产品真实上传路径入库。"""
    files = sorted((doc_dir or DOC_DIR).glob("*.pdf"))
    if not files:
        raise RuntimeError(f"缺少数据集 PDF，请先运行 build_corpus：{DOC_DIR}")
    started = time.perf_counter()
    mapping: dict[str, str] = {}
    name_index: dict[str, str] = {}
    for index, path in enumerate(files, start=1):
        doc = service.upload(path.name, path.read_bytes())
        if doc.get("status") != "ready":
            raise RuntimeError(f"{path.name} 入库失败：{doc.get('status')} {doc.get('error')}")
        mapping[doc_id_from_name(path.name)] = doc["id"]
        name_index[rag_eval.name_key(path.name)] = doc["id"]
        if index % 20 == 0:
            print(f"  入库 {index}/{len(files)}", flush=True)
    elapsed = time.perf_counter() - started
    ctx.notes.append(f"文档 {len(files)} 份，入库 {elapsed:.1f}s")
    return {
        "documents": len(files),
        "upload_seconds": round(elapsed, 2),
        "mapping": mapping,
        "name_index": name_index,
    }


def doc_id_from_name(filename: str) -> str:
    return filename.split("_")[0]


def resolve(question: dict[str, Any], mapping: dict[str, str]) -> dict[str, Any]:
    """把 HDOC-xxx 换成运行时 doc_id。"""
    out = dict(question)
    refs = []
    for ref in question.get("gold_relevant_pages") or []:
        item = dict(ref)
        item["document_id"] = mapping.get(ref["document_id"], ref["document_id"])
        refs.append(item)
    out["gold_relevant_pages"] = refs
    out["document_ids"] = [mapping.get(d, d) for d in question.get("document_ids") or []]
    return out


def humanise(retrieval: dict[str, Any], alias: dict[str, str]) -> dict[str, Any]:
    def fix(ref: str) -> str:
        doc_id, _, page = ref.partition(":p")
        return f"{alias.get(doc_id, doc_id)}:p{page}"

    out = dict(retrieval)
    for key in ("gold_pages", "top_pages", "covered_pages", "missing_pages"):
        if isinstance(out.get(key), list):
            out[key] = [fix(item) for item in out[key]]
    return out


# ---------------------------------------------------------------------------
# 检索层
# ---------------------------------------------------------------------------


def run_retrieval(
    service, questions: list[dict[str, Any]], alias: dict[str, str], *, top_k: int = 10
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    timings: dict[str, list[float]] = {mode: [] for mode in MODES}
    for mode in MODES:
        service.retrieval_config.mode = mode
        for raw in questions:
            started = time.perf_counter()
            result = service.retrieval.search(raw["question"], top_k=top_k)
            elapsed = (time.perf_counter() - started) * 1000
            timings[mode].append(elapsed)
            hits = result.hits
            retrieval = humanise(rag_eval.score_retrieval(raw, hits, ks=KS), alias)
            gold = rag_eval.gold_pairs(raw)
            pairs = [(h["doc_id"], int(h["page"])) for h in hits]
            mrr10 = round(mrr_at(pairs, gold, 10), 4) if gold else None
            rows.append(
                {
                    "mode": mode,
                    "question_id": raw["question_id"],
                    "question_type": raw["question_type"],
                    "answerable": bool(raw.get("answerable")),
                    "difficulty": raw.get("difficulty"),
                    **{f"recall@{k}": retrieval.get(f"recall@{k}") for k in KS},
                    **{f"hit@{k}": retrieval.get(f"hit@{k}") for k in KS},
                    "mrr@10": mrr10,
                    "mrr@5": retrieval.get("mrr@5"),
                    "missing_pages": retrieval.get("missing_pages", []),
                    "returned": len(hits),
                    "top1_key": hits[0]["chunk_key"] if hits else "",
                    "routes": hits[0].get("routes", []) if hits else [],
                    "routes_used": ",".join(result.routes_used),
                    "degraded": result.degraded,
                    "degraded_reason": result.degraded_reason,
                    "latency_ms": round(elapsed, 2),
                    "bm25_ms": round(result.timings.get("bm25_ms", 0.0), 2),
                    "vector_ms": round(result.timings.get("vector_ms", 0.0), 2),
                    "rerank_ms": round(result.timings.get("rerank_ms", 0.0), 2),
                }
            )
        print(f"  [{mode}] 检索完成 {len(questions)} 题", flush=True)

    aggregates: dict[str, Any] = {}
    for mode in MODES:
        items = [r for r in rows if r["mode"] == mode]
        positives = [i for i in items if i["answerable"]]
        negatives = [i for i in items if not i["answerable"]]
        latency = timings[mode]
        aggregates[mode] = {
            "questions": len(items),
            "answerable": len(positives),
            "negatives": len(negatives),
            **{f"recall@{k}": mean(i[f"recall@{k}"] for i in positives) for k in KS},
            **{f"hit@{k}": mean(i[f"hit@{k}"] for i in positives) for k in KS},
            "mrr@10": mean(i["mrr@10"] for i in positives),
            "full_evidence@5": mean(1.0 if i["recall@5"] == 1.0 else 0.0 for i in positives),
            "full_evidence@10": mean(1.0 if i["recall@10"] == 1.0 else 0.0 for i in positives),
            "negative_retrieved_any": mean(1.0 if i["returned"] else 0.0 for i in negatives)
            if negatives
            else None,
            "latency_p50_ms": common.percentile(latency, 50),
            "latency_p95_ms": common.percentile(latency, 95),
            "degraded_count": sum(1 for i in items if i["degraded"]),
        }
    by_type: dict[str, Any] = {}
    for mode in MODES:
        bucket: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            if row["mode"] == mode or not row["answerable"]:
                continue
            bucket.setdefault(row["question_type"], []).append(row)
        by_type[mode] = {
            name: {
                "n": len(items),
                "recall@5": mean(i["recall@5"] for i in items),
                "recall@10": mean(i["recall@10"] for i in items),
            }
            for name, items in sorted(bucket.items())
        }
    return rows, {"aggregates": aggregates, "by_type": by_type}


# ---------------------------------------------------------------------------
# 问答层
# ---------------------------------------------------------------------------


async def run_qa(
    service,
    ctx,
    questions: list[dict[str, Any]],
    *,
    top_k: int = 5,
    doc_index: dict[str, str] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    from aieb.brain.retrieval.context import build_budgeted_context

    rows: list[dict[str, Any]] = []
    for mode in MODES:
        service.retrieval_config.mode = mode
        for index, raw in enumerate(questions, start=1):
            trace = service.search_with_trace(raw["question"], top_k=top_k)
            hits = trace["hits"]
            bundle = build_budgeted_context(hits, max_tokens=1800)
            started = time.perf_counter()
            try:
                response = await service.answer(raw["question"], top_k=top_k)
                elapsed = (time.perf_counter() - started) * 1000
                payload = rag_eval.score_answer(
                    raw, response, bundle.text, doc_index=doc_index
                )
            except common.BudgetExceeded:
                raise
            except Exception as exc:  # noqa: BLE001 - 失败样本必须留在分母
                elapsed = (time.perf_counter() - started) * 1000
                payload = {
                    "executed": True,
                    "answer": "",
                    "answer_correct": False,
                    "correct": False,
                    "citation_precision": 0.0,
                    "citation_recall": 0.0,
                    "citation_count": 0,
                    "strict_citation_ok": False,
                    "groundedness": {"rate": 0.0, "total": 0},
                    "refusal": False,
                    "degraded": False,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            rows.append(
                {
                    "mode": mode,
                    "question_id": raw["question_id"],
                    "question_type": raw["question_type"],
                    "answerable": bool(raw.get("answerable")),
                    "difficulty": raw.get("difficulty"),
                    "answer_correct": bool(payload.get("answer_correct")),
                    "correct": bool(payload.get("correct")),
                    "citation_precision": payload.get("citation_precision") or 0.0,
                    "citation_recall": payload.get("citation_recall"),
                    "citation_count": payload.get("citation_count") or 0,
                    "groundedness": (payload.get("groundedness") or {}).get("rate", 0.0),
                    "refusal": bool(payload.get("refusal")),
                    "context_tokens": bundle.estimated_tokens,
                    "included_chunks": bundle.included,
                    "retrieved_chunks": len(hits),
                    "answer_ms": round(elapsed, 1),
                    "answer": common.redact(str(payload.get("answer") or "")),
                    "citations": payload.get("citations") or [],
                    "error": payload.get("error", ""),
                }
            )
            print(
                f"  [{mode}] {index}/{len(questions)} {raw['question_id']} "
                f"{'ok' if payload.get('answer_correct') else 'x'} {elapsed:.0f}ms",
                flush=True,
            )

    aggregates: dict[str, Any] = {}
    for mode in MODES:
        items = [r for r in rows if r["mode"] == mode]
        positives = [i for i in items if i["answerable"]]
        negatives = [i for i in items if not i["answerable"]]
        aggregates[mode] = {
            "executed": len(items),
            "answerable": len(positives),
            "negatives": len(negatives),
            "answer_accuracy": mean(1.0 if i["answer_correct"] else 0.0 for i in positives)
            if positives
            else None,
            "abstention_accuracy": mean(1.0 if i["answer_correct"] else 0.0 for i in negatives)
            if negatives
            else None,
            "end_to_end": mean(1.0 if i["correct"] else 0.0 for i in items) if items else None,
            "citation_precision": mean(i["citation_precision"] for i in positives)
            if positives
            else None,
            "citation_recall": mean(i["citation_recall"] or 0.0 for i in positives)
            if positives
            else None,
            "groundedness": mean(i["groundedness"] for i in items) if items else None,
            "avg_context_tokens": mean(i["context_tokens"] for i in items) if items else None,
            "avg_included_chunks": mean(i["included_chunks"] for i in items) if items else None,
            "answer_p50_ms": common.percentile([i["answer_ms"] for i in items], 50),
            "answer_p95_ms": common.percentile([i["answer_ms"] for i in items], 95),
            "errors": sum(1 for i in items if i["error"]),
        }
    return rows, aggregates


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


async def main_async(args: argparse.Namespace) -> int:
    from aieb.brain.retrieval.config import RetrievalConfig
    from aieb.brain.retrieval.embedding import configure_registry
    from aieb.brain.service import BrainService

    questions = load_questions(args.split, args.limit, holdout=args.holdout)
    if not questions:
        raise RuntimeError("没有匹配的题目")
    freeze: dict[str, Any] = {}
    if args.holdout:
        freeze = verify_freeze()
        print(f"留出集校验通过：{HOLDOUT_QUESTIONS_PATH}")
        print(f"  sha256={freeze['questions_sha256'][:16]}… 冻结于 {freeze['frozen_at']}")
        print(f"  used_for_tuning={freeze['used_for_tuning']}")

    ctx = common.prepare_run(
        suite="hybrid-ablation", mode="live", max_calls=args.max_calls, run_id=args.run_id
    )
    print("=" * 88)
    print("Hybrid RAG 四组消融")
    print("=" * 88)
    print(f"  题目  : {len(questions)} 道（split={args.split}）")
    print(f"  模式  : {', '.join(MODES)}")
    print(f"  模型  : {ctx.model.model}")
    print(f"  预算  : {ctx.budget.max_calls or '不限'} 次调用")
    print("=" * 88)

    work = ctx.workspace("hybrid")
    configure_registry(cache_dir=common.WORK_ROOT / "embedding_cache", batch_size=64)
    config = RetrievalConfig(
        mode="bm25",
        collection=args.collection,
        qdrant_url=args.qdrant_url,
        embedding_model=args.embedding_model,
        context_budget="budgeted",
        context_max_tokens=1800,
    )
    service = BrainService(work_dir=work, provider=ctx.provider, retrieval_config=config)
    mapping: dict[str, str] = {}
    try:
        print(">>> 建索引")
        info = index_corpus(ctx, service, HOLDOUT_DOC_DIR if args.holdout else DOC_DIR)
        mapping = info["mapping"]
        alias = {v: k for k, v in mapping.items()}

        print(">>> 向量化")
        service.retrieval_config.mode = "hybrid"
        try:
            service.retrieval.vector_store.drop()
        except Exception:  # noqa: BLE001
            pass
        sync = service.vector_indexer.sync_all()
        print("    向量同步:", sync.as_dict())

        resolved = [resolve(q, mapping) for q in questions]

        print(">>> 检索层")
        retrieval_rows, retrieval = run_retrieval(service, resolved, alias)

        qa_rows: list[dict[str, Any]] = []
        qa_agg: dict[str, Any] = {}
        if not args.skip_qa:
            qa_questions = _stratified(resolved, args.qa_limit)
            print(f">>> 问答层（{len(qa_questions)} 题 × {len(MODES)} 模式）")
            with common.patch_llm_counter(ctx.budget):
                qa_rows, qa_agg = await run_qa(
                    service, ctx, qa_questions, doc_index=info["name_index"]
                )
    finally:
        service.store.close()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "meta": ctx.meta(),
        "dataset": "holdout" if args.holdout else "main",
        "freeze": freeze,
        "index": {k: v for k, v in info.items() if k not in {"mapping", "name_index"}},
        "vector_sync": sync.as_dict(),
        "questions": len(questions),
        "question_types": dict(Counter(q["question_type"] for q in questions)),
        "retrieval": retrieval,
        "qa": {"aggregates": qa_agg},
    }
    common.write_json(OUT_DIR / f"ablation{args.out_tag}_summary.json", payload)
    common.write_csv(
        OUT_DIR / f"ablation{args.out_tag}_retrieval_cases.csv",
        retrieval_rows,
        [
            "mode", "question_id", "question_type", "answerable", "difficulty",
            "recall@1", "recall@3", "recall@5", "recall@10",
            "hit@1", "hit@3", "hit@5", "hit@10", "mrr@10",
            "returned", "routes_used", "degraded", "latency_ms", "bm25_ms", "vector_ms",
            "rerank_ms", "missing_pages",
        ],
    )
    if qa_rows:
        common.write_csv(
            OUT_DIR / f"ablation{args.out_tag}_qa_cases.csv",
            qa_rows,
            [
                "mode", "question_id", "question_type", "answerable", "answer_correct",
                "correct", "citation_precision", "citation_recall", "groundedness",
                "context_tokens", "included_chunks", "retrieved_chunks", "answer_ms",
                "answer", "citations", "error",
            ],
        )

    print()
    print("=" * 88)
    print("检索层结果")
    print("=" * 88)
    print(f"{'mode':<16}{'R@1':>7}{'R@3':>7}{'R@5':>7}{'R@10':>7}{'MRR@10':>8}"
          f"{'全证据@5':>9}{'P50ms':>8}{'P95ms':>8}")
    for mode in MODES:
        a = retrieval["aggregates"][mode]
        print(
            f"{mode:<16}{a['recall@1']:>7.3f}{a['recall@3']:>7.3f}{a['recall@5']:>7.3f}"
            f"{a['recall@10']:>7.3f}{a['mrr@10']:>8.3f}{a['full_evidence@5']:>9.3f}"
            f"{a['latency_p50_ms']:>8.0f}{a['latency_p95_ms']:>8.0f}"
        )
    if qa_agg:
        print()
        print("=" * 88)
        print("问答层结果")
        print("=" * 88)
        print(f"{'mode':<16}{'答对率':>8}{'端到端':>8}{'引用P':>8}{'引用R':>8}"
              f"{'依据度':>8}{'ctx tok':>9}{'P50ms':>8}")
        for mode in MODES:
            a = qa_agg[mode]
            print(
                f"{mode:<16}{(a['answer_accuracy'] or 0):>8.3f}{(a['end_to_end'] or 0):>8.3f}"
                f"{(a['citation_precision'] or 0):>8.3f}{(a['citation_recall'] or 0):>8.3f}"
                f"{(a['groundedness'] or 0):>8.3f}{(a['avg_context_tokens'] or 0):>9.0f}"
                f"{a['answer_p50_ms']:>8.0f}"
            )
    print(f"\n结果 → {OUT_DIR}")
    print(f"LLM 调用：{ctx.budget.used} / {ctx.budget.max_calls or '不限'}")
    print(f".aieb/ 未被改动：{'是' if common.demo_env_unchanged(ctx.demo_guard) else '否 —— 请排查！'}")
    return 0


def _stratified(questions: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """按题型分层取题，避免 QA 子集全是同一类。"""
    if not limit or limit >= len(questions):
        return questions
    buckets: dict[str, list[dict[str, Any]]] = {}
    for q in questions:
        buckets.setdefault(q["question_type"], []).append(q)
    picked: list[dict[str, Any]] = []
    index = 0
    types = sorted(buckets)
    while len(picked) < limit:
        added = False
        for name in types:
            items = buckets[name]
            if index < len(items):
                picked.append(items[index])
                added = True
                if len(picked) >= limit:
                    break
        if not added:
            break
        index += 1
    return picked


def main() -> int:
    parser = argparse.ArgumentParser(description="Hybrid RAG 四组消融")
    parser.add_argument("--split", default="dev", choices=("dev", "validation", "test", "all"))
    parser.add_argument(
        "--holdout",
        action="store_true",
        help="使用冻结留出集（benchmarks/datasets/hybrid_rag/holdout），并校验 sha256",
    )
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--qa-limit", type=int, default=0, help="问答层的分层抽样题数，0=全部")
    parser.add_argument("--skip-qa", action="store_true")
    parser.add_argument("--max-calls", type=int, default=0, dest="max_calls")
    parser.add_argument("--collection", default="hybrid_ablation")
    parser.add_argument("--qdrant-url", default="http://127.0.0.1:6333")
    parser.add_argument("--embedding-model", default="BAAI/bge-small-zh-v1.5")
    parser.add_argument("--out-tag", default="", dest="out_tag")
    parser.add_argument("--run-id", default="", dest="run_id")
    args = parser.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
