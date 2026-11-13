"""通用指标计算：Precision / Recall / F1、分位数、均值。"""

from __future__ import annotations

from typing import Iterable


def ratio(numerator: float, denominator: float) -> float:
    return float(numerator) / float(denominator) if denominator else 0.0


def prf(tp: int, fp: int, fn: int) -> dict[str, float | int]:
    """返回 precision / recall / f1 以及原始计数（计数必须一起报告）。"""
    precision = ratio(tp, tp + fp)
    recall = ratio(tp, tp + fn)
    f1 = ratio(2 * precision * recall, precision + recall)
    return {
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
    }


def mean(values: Iterable[float]) -> float:
    data = [float(v) for v in values]
    return round(sum(data) / len(data), 4) if data else 0.0


def percent(value: float) -> str:
    return f"{value * 100:.1f}%"
