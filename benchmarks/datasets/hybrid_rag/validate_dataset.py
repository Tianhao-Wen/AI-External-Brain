"""独立校验质量评测数据集：答案是否真在标注页上、负例是否真的无答案。

这是"不许编造"的最后一道闸门：它**只读 PDF 与 questions.jsonl**，
不引用任何被测代码的检索结果。

运行：
    python -m benchmarks.datasets.hybrid_rag.validate_dataset
"""

from __future__ import annotations

import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from benchmarks import common
from benchmarks.datasets.hybrid_rag.build_corpus import DOC_DIR, build_documents

DATASET_DIR = common.DATASETS_DIR / "hybrid_rag"
QUESTIONS_PATH = DATASET_DIR / "questions.jsonl"
REPORT_PATH = common.RESULTS_DIR / "hybrid_rag" / "dataset_validation.json"

_STRIP = re.compile(r"[\s\u3000，。、；：！？,.;:!?\"'“”‘’（）()\-—_~·…%]+")


def norm(text: str) -> str:
    return _STRIP.sub("", text or "").lower()


def pdf_pages(path: Path) -> list[str]:
    import pymupdf

    with pymupdf.open(str(path)) as doc:
        return [page.get_text("text") or "" for page in doc]


def validate(
    questions_path: Path | None = None,
    doc_dir: Path | None = None,
    report_path: Path | None = None,
    batch: str = "main",
) -> dict[str, Any]:
    questions_path = questions_path or QUESTIONS_PATH
    doc_dir = doc_dir or DOC_DIR
    report_path = report_path or REPORT_PATH
    specs = build_documents(batch=batch)
    by_id = {s.doc_id: s for s in specs}

    pages: dict[str, list[str]] = {}
    missing_pdf: list[str] = []
    for spec in specs:
        path = Path(doc_dir) / spec.filename
        if not path.is_file():
            missing_pdf.append(spec.filename)
            continue
        pages[spec.doc_id] = pdf_pages(path)

    questions = common.read_jsonl(Path(questions_path))

    errors: list[dict[str, Any]] = []
    key_not_on_gold_page: list[str] = []
    gold_page_mismatch: list[str] = []
    checked_keys = 0

    for q in questions:
        qid = q["question_id"]
        gold_refs = q.get("gold_relevant_pages") or []
        if q.get("answerable"):
            if not gold_refs:
                errors.append({"question_id": qid, "error": "answerable 但没有 gold 页"})
                continue
            # 1) gold 文档与页号必须存在
            for ref in gold_refs:
                doc_id = ref["document_id"]
                if doc_id not in pages:
                    errors.append({"question_id": qid, "error": f"gold 文档不存在：{doc_id}"})
                    continue
                for page in ref["pages"]:
                    if page < 1 or page > len(pages[doc_id]):
                        errors.append(
                            {"question_id": qid, "error": f"{doc_id} 页号越界：p{page}/{len(pages[doc_id])}"}
                        )
            # 2) 每组答案键至少有一个写法出现在**标注的那一页**上
            for group in q.get("answer_keys") or []:
                checked_keys += 1
                hit_pages = []
                for ref in gold_refs:
                    doc_pages = pages.get(ref["document_id"], [])
                    for page in ref["pages"]:
                        if 1 <= page <= len(doc_pages):
                            text = norm(doc_pages[page - 1])
                            if any(norm(alt) in text for alt in group):
                                hit_pages.append(f"{ref['document_id']}:p{page}")
                if not hit_pages:
                    key_not_on_gold_page.append(qid)
                    errors.append(
                        {
                            "question_id": qid,
                            "error": f"答案键未出现在 gold 页：{group}",
                            "gold": [f"{r['document_id']}:p{p}" for r in gold_refs for p in r['pages']],
                        }
                    )
        else:
            if gold_refs:
                errors.append({"question_id": qid, "error": "负例题不应有 gold 页"})
            corpus_text = norm(
                "\n".join("\n".join(v) for v in pages.values())
            )
            for pattern in q.get("forbidden") or []:
                if norm(pattern) and norm(pattern) in corpus_text:
                    errors.append(
                        {"question_id": qid, "error": f"负例禁用词竟然存在于语料：{pattern}"}
                    )

    # 3) gold 页与生成器记录的 fact_pages 必须一致（防止题面与文档漂移）
    for q in questions:
        note = q.get("note") or ""
        if not note.startswith("事实键="):
            continue
        kind = note.split("=", 1)[1]
        doc_id = q["split_basis_document"]
        spec = by_id[doc_id]
        expected = spec.fact_pages.get(kind)
        actual = q["gold_relevant_pages"][0]["pages"][0] if q["gold_relevant_pages"] else None
        if expected != actual:
            gold_page_mismatch.append(f"{q['question_id']}:{doc_id}:{kind}:{expected}!={actual}")

    # 4) 文档互不相同的检查（防止"复制 100 份"）
    digests: dict[int, list[str]] = {}
    for doc_id, doc_pages in pages.items():
        digest = hash(norm("\n".join(doc_pages)))
        digests.setdefault(digest, []).append(doc_id)
    duplicates = [group for group in digests.values() if len(group) > 1]

    stats = {
        "documents": len(specs),
        "documents_with_pdf": len(pages),
        "pages_total": sum(len(v) for v in pages.values()),
        "chars_total": sum(len("".join(v)) for v in pages.values()),
        "questions": len(questions),
        "answerable": sum(1 for q in questions if q.get("answerable")),
        "negatives": sum(1 for q in questions if not q.get("answerable")),
        "checked_answer_key_groups": checked_keys,
        "type_distribution": dict(Counter(q["question_type"] for q in questions)),
        "split_distribution": dict(Counter(q["split"] for q in questions)),
        "difficulty_distribution": dict(Counter(q["difficulty"] for q in questions)),
        "duplicate_document_groups": duplicates,
        "missing_pdf": missing_pdf,
        "errors": errors,
        "key_not_on_gold_page": sorted(set(key_not_on_gold_page)),
        "gold_page_mismatch": gold_page_mismatch,
    }
    stats["ok"] = not any(
        [
            errors,
            missing_pdf,
            duplicates,
            gold_page_mismatch,
        ]
    )
    return stats


def main() -> int:
    import sys

    holdout = "--holdout" in sys.argv
    if holdout:
        from benchmarks.datasets.hybrid_rag.build_holdout import (
            HOLDOUT_DIR,
            HOLDOUT_DOCS,
            HOLDOUT_QUESTIONS,
        )

        stats = validate(
            questions_path=HOLDOUT_QUESTIONS,
            doc_dir=HOLDOUT_DOCS,
            report_path=common.RESULTS_DIR / "hybrid_rag" / "holdout_validation.json",
            batch="holdout",
        )
        label = "独立留出集"
    else:
        stats = validate()
        label = "主数据集"
        holdout = False
    if report_path_override := (None if holdout else None):  # pragma: no cover
        _ = report_path_override
    common.write_json(
        (common.RESULTS_DIR / "hybrid_rag" / ("holdout_validation.json" if holdout else "dataset_validation.json")),
        stats,
    )
    print("=" * 76)
    print(f"Hybrid RAG {label}校验")
    print("=" * 76)
    for key in (
        "documents",
        "documents_with_pdf",
        "pages_total",
        "chars_total",
        "questions",
        "answerable",
        "negatives",
        "checked_answer_key_groups",
    ):
        print(f"  {key:<26}: {stats[key]}")
    print(f"  题型分布                : {stats['type_distribution']}")
    print(f"  切分分布                : {stats['split_distribution']}")
    print(f"  难度分布                : {stats['difficulty_distribution']}")
    print(f"  重复文档组              : {len(stats['duplicate_document_groups'])}")
    print(f"  校验错误                : {len(stats['errors'])}")
    if stats["errors"]:
        for item in stats["errors"][:10]:
            print(f"    - {item}")
    print(f"  报告                    : {REPORT_PATH if not holdout else 'holdout_validation.json'}")
    print("=" * 76)
    print("OK" if stats["ok"] else "FAILED")
    return 0 if stats["ok"] else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
