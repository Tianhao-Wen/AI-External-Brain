"""RAG 数据集独立校验：确认"标准答案真的写在第 N 页"。

这一步与检索系统无关，只做机械核对：
1. 引用的页码在 PDF 里真实存在；
2. 每个 ``answer_key`` 至少有一个候选表述出现在**标注页**的原文里
   （派生答案题用 ``source_keys`` 检查计算所需的原始数字）；
3. 记录这些表述是否**只**出现在标注页（若也出现在别的页，说明该题不能
   用来区分"引用到错误页码"，会在注解里标出来）；
4. 负例题的 ``forbidden`` 表述确实不在全量文本里。

产物：``annotations.jsonl``（每题一条，可人工复核 / 可复查）。
任何一条 ``status != ok`` 的题目都会让脚本以非零退出码结束，
避免把有问题的题带进正式评测。
"""

from __future__ import annotations

import json
import re
import sys
import unicodedata
from pathlib import Path

HERE = Path(__file__).resolve().parent
QUESTIONS = HERE / "questions.jsonl"
ANNOTATIONS = HERE / "annotations.jsonl"

_STRIP = re.compile(r"[\s\u3000,，·\"'“”‘’]+")


def norm(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "")
    text = text.replace("－", "-").replace("—", "-")
    return _STRIP.sub("", text).lower()


def _key_hit(key: str, haystack: str) -> bool:
    return norm(key) in haystack


def main() -> int:  # noqa: C901 - 校验逻辑直白展开更好读
    from benchmarks.datasets.rag.build_documents import DOCUMENTS, page_texts

    questions = [
        json.loads(line)
        for line in QUESTIONS.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    # 全量页文本（已归一化）
    corpus: dict[str, list[str]] = {}
    for doc_id in DOCUMENTS:
        corpus[doc_id] = [norm(t) for t in page_texts(doc_id)]

    annotations: list[dict] = []
    problems = 0
    for q in questions:
        qid = q["question_id"]
        checks: dict[str, object] = {}   # 硬性检查：不通过必须修数据
        flags: dict[str, object] = {}    # 参考信息：只影响"能不能考察页码精确性"
        notes: list[str] = []

        # 1) 页码存在性
        page_ok = True
        for ref in q.get("gold_relevant_pages") or []:
            doc_id = ref["document_id"]
            if doc_id not in corpus:
                page_ok = False
                notes.append(f"未知文档 {doc_id}")
                continue
            for page in ref["pages"]:
                if not 1 <= page <= len(corpus[doc_id]):
                    page_ok = False
                    notes.append(f"{doc_id} 第 {page} 页不存在")
        checks["pages_exist"] = page_ok

        # 2) 标注页 evidence 是否逐字可查
        evidence_ok = True
        if q["answerable"]:
            gold_text = "".join(
                corpus[ref["document_id"]][page - 1]
                for ref in q["gold_relevant_pages"]
                for page in ref["pages"]
                if 1 <= page <= len(corpus.get(ref["document_id"], []))
            )
            for snippet in q.get("gold_evidence") or []:
                if norm(snippet) not in gold_text:
                    evidence_ok = False
                    notes.append(f"evidence 不在标注页原文中：{snippet[:30]}…")
            checks["evidence_on_gold_pages"] = evidence_ok

            # 3) answer_key 是否可在标注页找到
            keys = q.get("source_keys") or q.get("answer_keys") or []
            keys_ok = True
            exclusive = True
            for group in keys:
                hits_pages = []
                for doc_id, pages in corpus.items():
                    for index, text in enumerate(pages, start=1):
                        if any(_key_hit(alt, text) for alt in group):
                            hits_pages.append((doc_id, index))
                if not hits_pages:
                    keys_ok = False
                    notes.append(f"答案 key 在全文都找不到：{group}")
                    continue
                allowed = {
                    (ref["document_id"], page)
                    for ref in q["gold_relevant_pages"]
                    for page in ref["pages"]
                }
                if not (set(hits_pages) & allowed):
                    keys_ok = False
                    notes.append(f"答案 key 只在非标注页出现：{group} -> {hits_pages}")
                if set(hits_pages) - allowed:
                    exclusive = False
            checks["answer_keys_on_gold_pages"] = keys_ok
            flags["answer_keys_exclusive_to_gold_pages"] = exclusive
            if not exclusive:
                notes.append("该题的答案表述也出现在其它页，无法用于考察页码精确性")
        else:
            # 4) 负例：被禁止的编造内容确实不存在
            full = "".join("".join(pages) for pages in corpus.values())
            forbidden_absent = True
            for pattern in q.get("forbidden") or []:
                if re.search(norm(pattern), full):
                    forbidden_absent = False
                    notes.append(f"负例题的 forbidden 表述在文档中出现了：{pattern}")
            checks["forbidden_absent"] = forbidden_absent
            # 负例要有区分度：问题与语料应存在词面交集（否则必然答对，等于送分题）
            q_norm = norm(q["question"])
            grams = {q_norm[i : i + 2] for i in range(len(q_norm) - 1)}
            overlap = sum(1 for g in grams if g in full)
            ratio = overlap / max(1, len(grams))
            flags["lexical_overlap_with_corpus"] = round(ratio, 3)
            if ratio < 0.25:
                notes.append(
                    f"负例题与语料词面交集仅 {ratio:.0%}，可能过于容易拒答"
                )

        ok = all(bool(v) for v in checks.values())
        problems += 0 if ok else 1
        annotations.append(
            {
                "question_id": qid,
                "question_type": q["question_type"],
                "answerable": q["answerable"],
                "difficulty": q["difficulty"],
                "status": "ok" if ok else "fix",
                "checks": checks,
                "flags": flags,
                "notes": notes,
                "auto_check": "aieb-benchmark validate_dataset.py",
            }
        )

    ANNOTATIONS.write_text(
        "\n".join(json.dumps(a, ensure_ascii=False) for a in annotations) + "\n",
        encoding="utf-8",
    )

    total = len(annotations)
    by_type: dict[str, int] = {}
    for a in annotations:
        by_type[a["question_type"]] = by_type.get(a["question_type"], 0) + 1
    print(f"题目总数 {total}，通过 {total - problems}，需修正 {problems}")
    for name, count in sorted(by_type.items()):
        print(f"  {name}: {count}")
    print(f"注解已写入 {ANNOTATIONS.relative_to(HERE.parents[2])}")
    if problems:
        for a in annotations:
            if a["status"] != "ok":
                print(f"  [!] {a['question_id']}: {'; '.join(a['notes'])}")
    return 0 if problems == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
