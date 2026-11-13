"""向量模型对比：同一语料 / 同一分块 / 同一评分口径，只换 embedding 模型。

公平性约束（与 `hybrid_modes.py` 一致，这里只加不减）：

* 同一批 100 份 PDF、走同一条上传链路，chunk 数与内容完全相同；
* 同一批题（默认 dev 切分）、同一套 gold 页、同一套评测器（`rag_eval.score_retrieval`）；
* top_k / RRF 参数 / 预算全部取默认值，不按模型调参；
* **一个模型一个独立进程**，这样"模型加载时间""峰值内存"才是真实口径；
* 磁盘向量缓存按 `(model_version, content_hash, dim)` 隔离，模型之间不会互相命中。

用法（每个模型跑一次）：
    python -m benchmarks.ablations.embedding_compare --model BAAI/bge-small-zh-v1.5
    python -m benchmarks.ablations.embedding_compare --model Qwen/Qwen3-Embedding-0.6B-Q
"""

from __future__ import annotations

import argparse
import json
import os
import re
import time
from pathlib import Path
from typing import Any

from benchmarks import common
from benchmarks.ablations.topk import mrr_at
from benchmarks.ablations.hybrid_modes import (
    KS,
    humanise,
    index_corpus,
    load_questions,
    resolve,
)
from benchmarks.evaluators import rag as rag_eval
from benchmarks.evaluators.metrics import mean

OUT_DIR = common.RESULTS_DIR / "hybrid_rag"
MODES = ("vector", "hybrid")


class _Ctx:
    """`index_corpus` 只需要一个能记笔记的对象。"""

    def __init__(self) -> None:
        self.notes: list[str] = []


def rss_mb() -> float:
    try:
        import psutil

        return round(psutil.Process(os.getpid()).memory_info().rss / 1024 / 1024, 1)
    except Exception:  # noqa: BLE001
        return -1.0


def peak_mb() -> float:
    try:
        import psutil

        info = psutil.Process(os.getpid()).memory_info()
        value = getattr(info, "peak_wset", None)
        return round(value / 1024 / 1024, 1) if value else rss_mb()
    except Exception:  # noqa: BLE001
        return rss_mb()


def slug(model: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", model.lower()).strip("_")


def main() -> int:
    from aieb.brain.retrieval.config import RetrievalConfig
    from aieb.brain.retrieval.embedding import configure_registry, get_embedder
    from aieb.brain.service import BrainService

    parser = argparse.ArgumentParser(description="向量模型对比（单一模型）")
    parser.add_argument("--model", required=True)
    parser.add_argument("--split", default="dev")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--top-k", type=int, default=10, dest="top_k")
    parser.add_argument("--collection", default="")
    parser.add_argument("--qdrant-url", default="http://127.0.0.1:6335")
    parser.add_argument("--skip-upload", action="store_true", help="复用已存在的 SQLite（跳过 PDF 解析）")
    parser.add_argument(
        "--cache-dir",
        default="",
        help="向量磁盘缓存目录（默认为 benchmarks/.work/embedding_cache）",
    )
    parser.add_argument(
        "--fresh-cache",
        action="store_true",
        help="运行前清空 --cache-dir，用于测“冷启动”真实吞吐（默认复用旧缓存会失真）",
    )
    parser.add_argument(
        "--load-only",
        action="store_true",
        help="只测模型加载与首次推理耗时（配合 HF_HUB_OFFLINE=1 隔离网络影响）",
    )
    args = parser.parse_args()

    name = slug(args.model)
    collection = args.collection or f"emb_{name}"[:60]
    report: dict[str, Any] = {
        "model": args.model,
        "split": args.split,
        "top_k": args.top_k,
        "collection": collection,
        "rss_start_mb": rss_mb(),
    }

    cache_dir = Path(args.cache_dir) if args.cache_dir else common.WORK_ROOT / "embedding_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    if args.fresh_cache:
        # 只清向量结果缓存（SQLite），**保留** fastembed 的模型文件目录 models/：
        # 否则"模型加载时间"会把几 GB 的重新下载算进去，指标失真。
        for stale in cache_dir.glob("embedding_cache.sqlite3*"):
            stale.unlink(missing_ok=True)
    report["cache_dir"] = str(cache_dir)
    report["fresh_cache"] = bool(args.fresh_cache)
    configure_registry(cache_dir=cache_dir, batch_size=64)
    work = common.WORK_ROOT / "emb_compare" / name
    if not args.skip_upload:
        import shutil

        shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)

    config = RetrievalConfig(
        mode="bm25",  # 上传阶段不算向量，向量化单独计时
        collection=collection,
        qdrant_url=args.qdrant_url,
        embedding_model=args.model,
        context_budget="legacy",
    )
    provider, _ = common.resolve_provider(offline=True)
    service = BrainService(work_dir=work, provider=provider, retrieval_config=config)

    # --- 1. 模型冷加载（第一次 embed 才真正建 session）----------------------
    started = time.perf_counter()
    embedder = get_embedder(args.model)
    try:
        embedder.embed_query("预热")
        report["model_load_seconds"] = round(time.perf_counter() - started, 2)
    except Exception as exc:  # noqa: BLE001
        report["model_load_seconds"] = None
        report["error"] = f"{type(exc).__name__}: {exc}"
        common.write_json(OUT_DIR / f"emb_compare_{name}.json", report)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 1
    report["dim"] = getattr(embedder, "dim", None)
    report["model_version"] = getattr(embedder, "model_version", None)
    report["rss_after_load_mb"] = rss_mb()
    if args.load_only:
        report["hf_hub_offline"] = os.environ.get("HF_HUB_OFFLINE", "0")
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        common.write_json(OUT_DIR / f"emb_load_{name}.json", report)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    try:
        # --- 2. 入库（PDF → 分块 → SQLite），与模型无关 -------------------
        if not args.skip_upload:
            info = index_corpus(_Ctx(), service)
        else:
            mapping = {}
            for doc in service.store.list_documents():
                mapping[doc["id"].split("_")[0]] = doc["id"]
            info = {"documents": len(mapping), "upload_seconds": None, "mapping": mapping}
            # 数据集里的 doc_id 形如 HDOC-01，文件名前缀也是 HDOC-01
        mapping = info["mapping"]
        alias = {v: k for k, v in mapping.items()}
        chunks = service.store.all_chunks()
        report["documents"] = info.get("documents")
        report["chunks"] = len(chunks)
        report["upload_seconds"] = info.get("upload_seconds")

        # --- 3. 向量化 + 写入 Qdrant --------------------------------------
        service.retrieval_config.mode = "hybrid"
        try:
            service.retrieval.vector_store.drop()
        except Exception:  # noqa: BLE001
            pass
        started = time.perf_counter()
        sync = service.vector_indexer.sync_all()
        report["vector_sync_seconds"] = round(time.perf_counter() - started, 2)
        report["vector_sync"] = sync.as_dict()
        report["embed_texts_per_second"] = round(
            (sync.written or 0) / max(1e-6, report["vector_sync_seconds"]), 2
        )
        report["rss_after_index_mb"] = rss_mb()

        # --- 4. 检索质量与延迟 --------------------------------------------
        questions = load_questions(args.split, args.limit)
        resolved = [resolve(q, mapping) for q in questions]
        rows: list[dict[str, Any]] = []
        timings: dict[str, list[float]] = {m: [] for m in MODES}
        embed_times: dict[str, list[float]] = {m: [] for m in MODES}
        for mode in MODES:
            service.retrieval_config.mode = mode
            for raw in resolved:
                started = time.perf_counter()
                result = service.retrieval.search(raw["question"], top_k=args.top_k)
                elapsed = (time.perf_counter() - started) * 1000
                timings[mode].append(elapsed)
                embed_times[mode].append(result.timings.get("embed_query_ms", 0.0))
                retrieval = humanise(rag_eval.score_retrieval(raw, result.hits, ks=KS), alias)
                gold = rag_eval.gold_pairs(raw)
                pairs = [(h["doc_id"], int(h["page"])) for h in result.hits]
                rows.append(
                    {
                        "mode": mode,
                        "question_id": raw["question_id"],
                        "answerable": bool(raw.get("answerable")),
                        **{f"recall@{k}": retrieval.get(f"recall@{k}") for k in KS},
                        "mrr@10": round(mrr_at(pairs, gold, 10), 4) if gold else None,
                        "degraded": result.degraded,
                        "latency_ms": round(elapsed, 2),
                    }
                )
            print(f"  [{mode}] {len(resolved)} 题完成", flush=True)

        aggregates: dict[str, Any] = {}
        for mode in MODES:
            positives = [r for r in rows if r["mode"] == mode and r["answerable"]]
            aggregates[mode] = {
                "questions": len(positives),
                **{f"recall@{k}": mean(r[f"recall@{k}"] for r in positives) for k in KS},
                "mrr@10": mean(r["mrr@10"] for r in positives),
                "full_evidence@5": mean(1.0 if r["recall@5"] == 1.0 else 0.0 for r in positives),
                "full_evidence@10": mean(1.0 if r["recall@10"] == 1.0 else 0.0 for r in positives),
                "latency_p50_ms": common.percentile(timings[mode], 50),
                "latency_p95_ms": common.percentile(timings[mode], 95),
                "query_embed_p50_ms": common.percentile(embed_times[mode], 50),
                "degraded": sum(1 for r in rows if r["mode"] == mode and r["degraded"]),
            }
        report["quality"] = aggregates
        report["questions"] = len(questions)
        report["rss_final_mb"] = rss_mb()
        report["peak_wset_mb"] = peak_mb()
        report["errors"] = 0
    finally:
        service.store.close()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / f"emb_compare_{name}.json"
    common.write_json(path, report)
    printable = {k: v for k, v in report.items() if k != "vector_sync"}
    print(json.dumps(printable, ensure_ascii=False, indent=2))
    print(f"\n结果 → {path}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
