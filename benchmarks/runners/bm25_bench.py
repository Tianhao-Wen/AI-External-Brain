"""BM25 性能 A/B 基准：优化前的全扫描实现 vs 优化后的两条路径。

三份实现同时跑同一批查询，并**逐条比对返回顺序与分数**，
确保性能提升不是靠改变相关性换来的。

用法：
    python -m benchmarks.runners.bm25_bench --db benchmarks/.work/scale_50000/.aieb/brain/brain.sqlite3
"""

from __future__ import annotations

import argparse
import gc
import json
import math
import os
import statistics
import time
from collections import Counter
from pathlib import Path
from typing import Any, Sequence

from benchmarks import common
from aieb.brain.knowledge import BM25Index, tokenize
from aieb.brain.store import BrainStore

OUT_DIR = common.RESULTS_DIR / "hybrid_rag"


def rss_mb() -> float:
    try:
        import psutil

        return round(psutil.Process(os.getpid()).memory_info().rss / 1024 / 1024, 1)
    except Exception:  # noqa: BLE001
        return -1.0


def pct(values: Sequence[float], p: float) -> float:
    if not values:
        return 0.0
    data = sorted(values)
    rank = (len(data) - 1) * (p / 100.0)
    low = int(rank)
    high = min(low + 1, len(data) - 1)
    weight = rank - low
    return round(data[low] * (1 - weight) + data[high] * weight, 2)


def legacy_scores(
    docs: list[dict[str, Any]], token_lists: list[list[str]], df: Counter, avg_len: float,
    query: str, top_k: int,
) -> list[tuple[float, int]]:
    """优化**前**的实现（原样复刻）：doc 外层、token 内层，idf 在循环里重算。"""
    tokens = tokenize(query)
    total = len(docs)
    scored: list[tuple[float, int]] = []
    for i, doc_tokens in enumerate(token_lists):
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
    return scored[:top_k]


def build_queries(chunks: list[dict[str, Any]], count: int) -> list[str]:
    seen: list[str] = []
    step = max(1, len(chunks) // max(1, count))
    for chunk in chunks[::step][:count]:
        text = chunk["text"][:60].replace("\n", " ")
        seen.append(text)
    # 再补几条"短查询"，更接近真实用户输入
    seen.extend(
        [
            "预算上限是多少",
            "抽检比例",
            "违约金上限",
            "项目编号格式",
            "审批金额 万元",
        ]
    )
    return seen[: count + 5]


def run(db: Path, query_count: int = 30, top_k: int = 10) -> dict[str, Any]:
    report: dict[str, Any] = {
        "db": str(db),
        "top_k": top_k,
        "rss_start_mb": rss_mb(),
        "cpu_count": os.cpu_count(),
    }
    store = BrainStore(path=db)
    chunks = store.all_chunks()
    report["chunks"] = len(chunks)
    report["chars"] = sum(len(c["text"]) for c in chunks)

    started = time.perf_counter()
    index = BM25Index(store)
    index.rebuild()
    report["rebuild_seconds"] = round(time.perf_counter() - started, 2)
    gc.collect()
    report["rss_after_build_mb"] = rss_mb()

    postings_entries = sum(len(v) for v in index._postings.values())
    arrays_bytes = sum(len(v) for v in index._postings.values()) * 4 + sum(
        len(v) for v in index._post_tf.values()
    ) * 4 + len(index._lengths) * 8
    report["vocab"] = len(index._postings)
    report["postings_entries"] = postings_entries
    report["postings_arrays_mb"] = round(arrays_bytes / 1024 / 1024, 1)
    # 旧结构（list[int] + list[dict]）的估算内存：int 对象 28B + 指针 8B
    report["legacy_structure_estimate_mb"] = round(
        (postings_entries * 36 + len(chunks) * 1400 * 60) / 1024 / 1024, 1
    )

    queries = build_queries(chunks, query_count)
    report["queries"] = len(queries)

    # --- 1) 优化前的实现（DOC 外层全扫描）---
    token_lists = [tokenize(c["text"]) for c in chunks]
    df = Counter()
    for tokens in token_lists:
        df.update(set(tokens))
    avg_len = sum(len(t) for t in token_lists) / max(1, len(token_lists))
    legacy_times: list[float] = []
    legacy_out: list[list[tuple[float, int]]] = []
    for query in queries:
        started = time.perf_counter()
        legacy_out.append(legacy_scores(chunks, token_lists, df, avg_len, query, top_k))
        legacy_times.append((time.perf_counter() - started) * 1000)
    report["legacy_ms"] = {"p50": pct(legacy_times, 50), "p95": pct(legacy_times, 95)}
    del token_lists, df
    gc.collect()

    # --- 2) 优化后：纯 Python 路径 ---
    index.use_numpy = False
    py_times: list[float] = []
    py_out: list[list[tuple[float, int]]] = []
    for query in queries:
        started = time.perf_counter()
        py_out.append(index._score(tokenize(query), top_k, None))
        py_times.append((time.perf_counter() - started) * 1000)
    report["python_path_ms"] = {"p50": pct(py_times, 50), "p95": pct(py_times, 95)}

    # --- 3) 优化后：numpy 路径 ---
    index.use_numpy = True
    np_times: list[float] = []
    np_out: list[list[tuple[float, int]]] = []
    for query in queries:
        started = time.perf_counter()
        np_out.append(index._score(tokenize(query), top_k, None))
        np_times.append((time.perf_counter() - started) * 1000)
    report["numpy_path_ms"] = {"p50": pct(np_times, 50), "p95": pct(np_times, 95)}
    gc.collect()
    report["rss_final_mb"] = rss_mb()

    # --- 4) 等价性：三者必须给出相同顺序与相同分数 ---
    def same(a: Sequence[tuple[float, int]], b: Sequence[tuple[float, int]]) -> bool:
        return len(a) == len(b) and all(
            ai == bi and abs(av - bv) < 1e-12 for (av, ai), (bv, bi) in zip(a, b)
        )

    checks = {
        "legacy_vs_python": [same(l, p) for l, p in zip(legacy_out, py_out)],
        "python_vs_numpy": [same(p, n) for p, n in zip(py_out, np_out)],
        "legacy_vs_numpy": [same(l, n) for l, n in zip(legacy_out, np_out)],
    }
    report["equivalence"] = {
        name: {"cases": len(values), "all_equal": all(values)} for name, values in checks.items()
    }
    report["equivalence"]["all_pass"] = all(v["all_equal"] for v in report["equivalence"].values())

    # --- 5) 吞吐 ---
    repeats = 3
    started = time.perf_counter()
    for _ in range(repeats):
        for query in queries:
            index._score(tokenize(query), top_k, None)
    elapsed = time.perf_counter() - started
    report["numpy_qps"] = round((len(queries) * repeats) / max(1e-6, elapsed), 2)

    # --- 6) top-1 一致性抽样（便于人工核对）---
    report["samples"] = [
        {
            "query": q[:40],
            "legacy_top1": chunks[legacy_out[i][0][1]]["doc_name"] if legacy_out[i] else None,
            "numpy_top1": chunks[np_out[i][0][1]]["doc_name"] if np_out[i] else None,
            "shared_top1": bool(legacy_out[i] and np_out[i] and legacy_out[i][0][1] == np_out[i][0][1]),
        }
        for i, q in enumerate(queries[:10])
    ]
    store.close()
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="BM25 性能 A/B 基准")
    parser.add_argument(
        "--db",
        default="benchmarks/.work/scale_50000/.aieb/brain/brain.sqlite3",
        help="包含分块的 SQLite 路径",
    )
    parser.add_argument("--queries", type=int, default=30)
    parser.add_argument("--top-k", type=int, default=10, dest="top_k")
    args = parser.parse_args()
    db = Path(args.db)
    if not db.is_file():
        print(f"找不到 SQLite：{db}")
        return 1
    report = run(db, args.queries, args.top_k)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / f"bm25_bench_{report['chunks']}.json"
    common.write_json(path, report)
    printable = {k: v for k, v in report.items() if k != "samples"}
    print(json.dumps(printable, ensure_ascii=False, indent=2))
    print(f"\n结果 → {path}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
