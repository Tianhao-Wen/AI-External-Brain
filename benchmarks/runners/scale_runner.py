"""大规模索引与检索性能测试。

产出**真实测量**数据，不做任何推算：跑不到 100k 就如实写跑到多少，
Qdrant 小集合不建 HNSW 也如实写 `indexed_vectors_count`。

用法：
    python -m benchmarks.runners.scale_runner --chunks 1000
    python -m benchmarks.runners.scale_runner --chunks 10000 --collection scale_10k
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import statistics
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Sequence

from benchmarks import common
from benchmarks.datasets.hybrid_rag import build_scale

RESULTS_DIR = common.RESULTS_DIR / "hybrid_rag"


# ---------------------------------------------------------------------------
# 采集工具
# ---------------------------------------------------------------------------


def rss_mb() -> float:
    try:
        import psutil

        return round(psutil.Process(os.getpid()).memory_info().rss / 1024 / 1024, 1)
    except Exception:  # noqa: BLE001
        return -1.0


def dir_size_mb(path: Path) -> float:
    if not path.exists():
        return 0.0
    total = 0
    for file in path.rglob("*"):
        if file.is_file():
            try:
                total += file.stat().st_size
            except OSError:
                continue
    return round(total / 1024 / 1024, 2)


def file_size_mb(path: Path) -> float:
    try:
        return round(path.stat().st_size / 1024 / 1024, 2)
    except OSError:
        return 0.0


def pct(values: Sequence[float], p: float) -> float:
    if not values:
        return 0.0
    data = sorted(values)
    if len(data) == 1:
        return round(float(data[0]), 2)
    rank = (len(data) - 1) * (p / 100.0)
    low = int(rank)
    high = min(low + 1, len(data) - 1)
    weight = rank - low
    return round(float(data[low] * (1 - weight) + data[high] * weight), 2)


def summarize_latencies(values: Sequence[float]) -> dict[str, Any]:
    if not values:
        return {"n": 0}
    return {
        "n": len(values),
        "mean_ms": round(statistics.fmean(values), 2),
        "p50_ms": pct(values, 50),
        "p95_ms": pct(values, 95),
        "max_ms": round(max(values), 2),
    }


def build_queries(docs: list[dict[str, Any]], count: int = 30) -> list[str]:
    """按语料自己的主题/主体拼查询，保证**确实有答案**且互相不同。"""
    queries: list[str] = []
    for doc in docs:
        topic = doc["topic"]
        company = doc["company"]
        base = doc["doc_id"]
        queries.extend(
            [
                f"{company}{topic}管理办法中，材料齐备后多少个工作日内要完成审批",
                f"{base} 的{topic}抽查比例是多少",
                f"{company}{topic}新立项的编号格式是什么",
                f"{topic}违约处理的违约金上限是多少",
            ]
        )
        if len(queries) >= count:
            break
    return queries[:count]


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def run_scale(
    target_chunks: int,
    *,
    collection: str,
    corpus_dir: Path | None = None,
    query_count: int = 30,
    concurrency: int = 4,
    mode: str = "hybrid",
    embedding_model: str = "BAAI/bge-small-zh-v1.5",
    skip_rerank: bool = False,
    qdrant_url: str = "http://127.0.0.1:6333",
    storage_dir: Path | None = None,
) -> dict[str, Any]:
    from aieb.brain.retrieval.config import RetrievalConfig
    from aieb.brain.retrieval.embedding import configure_registry
    from aieb.brain.service import BrainService

    manifest = build_scale.generate(target_chunks, out_dir=corpus_dir)
    corpus_dir = Path(manifest["output_dir"])
    files = build_scale.iter_documents(corpus_dir)

    provider, model_ref = common.resolve_provider(offline=True)
    work = common.WORK_ROOT / f"scale_{target_chunks}"
    import shutil

    if work.exists():
        shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)

    configure_registry(cache_dir=common.WORK_ROOT / "embedding_cache", batch_size=64)
    config = RetrievalConfig(
        # 上传阶段先关掉向量同步：否则"上传耗时"会把 embedding + upsert 算进去，
        # 无法区分"解析/分块/SQLite/BM25"与"向量化"各自花了多少时间。
        mode="bm25",
        collection=collection,
        qdrant_url=qdrant_url,
        embedding_model=embedding_model,
        context_budget="legacy",
    )
    service = BrainService(work_dir=work, provider=provider, retrieval_config=config)
    report: dict[str, Any] = {
        "target_chunks": target_chunks,
        "corpus": manifest,
        "collection": collection,
        "mode": mode,
        "embedding_model": embedding_model,
        "hardware": {
            "platform": platform.platform(),
            "cpu_count": os.cpu_count(),
            "python": platform.python_version(),
            "gpu": _gpu_name(),
        },
        "rss_start_mb": rss_mb(),
    }

    sqlite_path = work / ".aieb" / "brain" / "brain.sqlite3"
    qdrant_storage = storage_dir or Path("tools/qdrant/storage")
    storage_before = dir_size_mb(qdrant_storage)

    try:
        # --- 1. 上传（真实解析 + 分块 + SQLite 写入 + BM25 增量）-------------
        started = time.perf_counter()
        for index, path in enumerate(files, start=1):
            service.upload(path.name, path.read_bytes())
            if index % max(1, len(files) // 10) == 0:
                print(f"  上传 {index}/{len(files)}", flush=True)
        report["upload_seconds"] = round(time.perf_counter() - started, 2)

        chunks = service.store.all_chunks()
        report["actual_chunks"] = len(chunks)
        report["actual_documents"] = len({c["doc_id"] for c in chunks})
        report["chars_total"] = sum(len(c["text"]) for c in chunks)
        report["bm25_docs"] = service.index.size
        report["upload_chunks_per_second"] = round(
            len(chunks) / max(1e-6, report["upload_seconds"]), 2
        )
        report["rss_after_upload_mb"] = rss_mb()

        # --- 2. BM25 全量重建（纯分词+建索引耗时）-----------------------------
        started = time.perf_counter()
        service.index.rebuild()
        report["bm25_rebuild_seconds"] = round(time.perf_counter() - started, 2)
        report["bm25_rebuild_chunks_per_second"] = round(
            len(chunks) / max(1e-6, report["bm25_rebuild_seconds"]), 2
        )
        report["rss_after_bm25_mb"] = rss_mb()

        # --- 3. 向量化 + 写入 Qdrant --------------------------------------
        service.retrieval_config.mode = mode if mode != "bm25" else "hybrid"
        store = service.retrieval.vector_store
        try:
            store.drop()
        except Exception:  # noqa: BLE001
            pass
        started = time.perf_counter()
        stats = service.vector_indexer.sync_all()
        report["vector_sync_seconds"] = round(time.perf_counter() - started, 2)
        report["vector_sync"] = stats.as_dict()
        report["vector_chunks_per_second"] = round(
            stats.written / max(1e-6, report["vector_sync_seconds"]), 2
        )
        report["qdrant_wait_indexed"] = store.wait_until_indexed(timeout=600)
        report["qdrant_info"] = store.info()
        report["rss_after_vector_mb"] = rss_mb()

        # --- 4. 查询延迟 ---------------------------------------------------
        docs = [
            {"doc_id": c["doc_id"], "topic": c["doc_name"].split("_")[2].replace(".md", ""),
             "company": c["doc_name"].split("_")[1]}
            for c in chunks[:: max(1, len(chunks) // 40)]
        ]
        queries = build_queries(docs, query_count)

        # 重排模型可能几十秒才能加载完，不计入推理延迟
        if not skip_rerank:
            try:
                report["rerank_load_seconds"] = round(service.retrieval.reranker.warmup(), 2)
            except Exception as exc:  # noqa: BLE001 - 预热失败不影响其余指标
                report["rerank_load_error"] = f"{type(exc).__name__}: {exc}"

        timings: dict[str, list[float]] = {"bm25": [], "vector": [], "hybrid": [], "rerank": []}
        for query in queries:
            for name in ("bm25", "vector", "hybrid"):
                service.retrieval_config.mode = name
                started = time.perf_counter()
                service.retrieval.search(query, top_k=5)
                timings[name].append((time.perf_counter() - started) * 1000)
            if not skip_rerank:
                service.retrieval_config.mode = "hybrid_rerank"
                started = time.perf_counter()
                result = service.retrieval.search(query, top_k=5)
                timings["rerank"].append((time.perf_counter() - started) * 1000)
                timings.setdefault("rerank_only", []).append(
                    float(result.timings.get("rerank_ms", 0.0))
                )
        service.retrieval_config.mode = mode
        report["query_latency"] = {k: summarize_latencies(v) for k, v in timings.items()}
        report["query_count"] = len(queries)

        # --- 5. 顺序吞吐（QPS）----------------------------------------------
        started = time.perf_counter()
        repeats = 3
        for _ in range(repeats):
            for query in queries:
                service.retrieval.search(query, top_k=5)
        elapsed = time.perf_counter() - started
        report["throughput_qps"] = round((len(queries) * repeats) / max(1e-6, elapsed), 2)
        report["throughput_repeats"] = repeats

        # --- 6. 并发查询 ---------------------------------------------------
        def _one(query: str) -> float:
            begin = time.perf_counter()
            service.retrieval.search(query, top_k=5)
            return (time.perf_counter() - begin) * 1000

        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            started = time.perf_counter()
            latencies = list(pool.map(_one, queries))
            wall = time.perf_counter() - started
        report["concurrency"] = {
            "threads": concurrency,
            "queries": len(queries),
            "wall_seconds": round(wall, 2),
            "qps": round(len(queries) / max(1e-6, wall), 2),
            "latency": summarize_latencies(latencies),
        }

        # --- 7. 增量上传 / 删除 --------------------------------------------
        extra = corpus_dir / "_incremental.md"
        extra.write_text(
            "SCALE-INC 增量测试文档 — 第 1 页\n第1条 增量上传测试。增量文档的审批准入金额为 88 万元。",
            encoding="utf-8",
        )
        started = time.perf_counter()
        new_doc = service.upload(extra.name, extra.read_bytes())
        report["incremental_upload_seconds"] = round(time.perf_counter() - started, 2)
        report["incremental_chunks"] = len(service.store.chunks_of(new_doc["id"]))
        started = time.perf_counter()
        service.delete_document(new_doc["id"])
        report["delete_seconds"] = round(time.perf_counter() - started, 2)
        report["points_after_delete"] = store.count()
        extra.unlink(missing_ok=True)

        # --- 8. 索引重建 ---------------------------------------------------
        started = time.perf_counter()
        service.vector_indexer.rebuild()
        report["vector_rebuild_seconds"] = round(time.perf_counter() - started, 2)

        # --- 9. 体积 -------------------------------------------------------
        report["sizes"] = {
            "sqlite_mb": file_size_mb(sqlite_path),
            "uploads_dir_mb": dir_size_mb(work / ".aieb" / "brain" / "uploads"),
            "embedding_cache_mb": dir_size_mb(common.WORK_ROOT / "embedding_cache"),
            "qdrant_storage_mb_total": dir_size_mb(qdrant_storage),
            "qdrant_storage_mb_delta": round(
                dir_size_mb(qdrant_storage) - storage_before, 2
            ),
        }
        report["qdrant_info_final"] = store.info()
        report["rss_final_mb"] = rss_mb()
        report["status"] = "ok"
    finally:
        service.store.close()

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    path = RESULTS_DIR / f"scale_{target_chunks}.json"
    common.write_json(path, report)
    report["_path"] = str(path)
    return report


def _gpu_name() -> str:
    try:
        import subprocess

        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return out.stdout.strip().splitlines()[0] if out.stdout.strip() else ""
    except Exception:  # noqa: BLE001
        return ""


def main() -> int:
    parser = argparse.ArgumentParser(description="大规模索引与检索性能测试")
    parser.add_argument("--chunks", type=int, default=1000)
    parser.add_argument("--collection", default="")
    parser.add_argument("--queries", type=int, default=30)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--mode", default="hybrid")
    parser.add_argument("--skip-rerank", action="store_true")
    parser.add_argument("--qdrant-url", default="http://127.0.0.1:6333")
    parser.add_argument(
        "--storage-dir",
        default="tools/qdrant/storage",
        help="Qdrant 存储目录（用于统计体积；指向独立实例时才有意义）",
    )
    args = parser.parse_args()
    collection = args.collection or f"scale_{args.chunks}"
    print("=" * 80)
    print(f"规模测试：目标 {args.chunks} chunks，collection={collection}")
    print("=" * 80)
    report = run_scale(
        args.chunks,
        collection=collection,
        query_count=args.queries,
        concurrency=args.concurrency,
        mode=args.mode,
        skip_rerank=args.skip_rerank,
        qdrant_url=args.qdrant_url,
        storage_dir=Path(args.storage_dir),
    )
    printable = {k: v for k, v in report.items() if k not in {"corpus"}}
    print(json.dumps(printable, ensure_ascii=False, indent=2)[:4000])
    print(f"\n结果 → {report['_path']}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
