"""汇总 BM25 优化前后（before/after）的规模测试对比表。"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path("benchmarks/results/hybrid_rag")
ROWS = [
    ("1.5k", "scale_1000"),
    ("15k", "scale_10000"),
    ("75k", "scale_50000"),
]


def load(name: str) -> dict:
    return json.loads((ROOT / f"{name}.json").read_text(encoding="utf-8"))


def pick(d: dict, mode: str, key: str) -> float:
    return (d.get("query_latency", {}).get(mode) or {}).get(key, 0.0)


def main() -> int:
    print(f"{'规模':<6}{'指标':<22}{'优化前':>12}{'优化后':>12}{'变化':>12}")
    print("-" * 66)
    for label, stem in ROWS:
        before = load(f"{stem}_before_optimization")
        after = load(stem)
        pairs = [
            ("actual_chunks", after["actual_chunks"], before["actual_chunks"], "int"),
            ("BM25 P50 (ms)", pick(after, "bm25", "p50_ms"), pick(before, "bm25", "p50_ms"), "ms"),
            ("BM25 P95 (ms)", pick(after, "bm25", "p95_ms"), pick(before, "bm25", "p95_ms"), "ms"),
            ("Hybrid P50 (ms)", pick(after, "hybrid", "p50_ms"), pick(before, "hybrid", "p50_ms"), "ms"),
            ("Vector P50 (ms)", pick(after, "vector", "p50_ms"), pick(before, "vector", "p50_ms"), "ms"),
            ("QPS (hybrid)", after.get("throughput_qps", 0), before.get("throughput_qps", 0), "x"),
            ("BM25 重建 (s)", after.get("bm25_rebuild_seconds", 0), before.get("bm25_rebuild_seconds", 0), "s"),
            ("RSS@BM25 (MB)", after.get("rss_after_bm25_mb", 0), before.get("rss_after_bm25_mb", 0), "int"),
            ("RSS 峰值 (MB)", after.get("rss_final_mb", 0), before.get("rss_final_mb", 0), "int"),
            ("上传 (s)", after.get("upload_seconds", 0), before.get("upload_seconds", 0), "s"),
        ]
        for name, a, b, unit in pairs:
            if unit == "int":
                delta = f"{a - b:+.0f}" if b else "—"
                print(f"{label:<6}{name:<22}{b:>12.0f}{a:>12.0f}{delta:>12}")
            elif unit == "x":
                ratio = f"{a / b:.2f}x" if b else "—"
                print(f"{label:<6}{name:<22}{b:>12.2f}{a:>12.2f}{ratio:>12}")
            else:
                delta = f"{(a / b):.2f}x" if b else "—"
                print(f"{label:<6}{name:<22}{b:>12.1f}{a:>12.1f}{delta:>12}")
        print("-" * 66)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
