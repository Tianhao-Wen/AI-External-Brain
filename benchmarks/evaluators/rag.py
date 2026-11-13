"""RAG 评分器：检索层（Recall@K / MRR / Hit@K）与回答层（事实/引用/拒答/依据）。

所有判定都是**确定性规则**，并且每条判定都留下可复核的中间量
（命中集合、缺失的 key、被引用的页码），便于人工抽查。刻意没有引入
LLM-as-Judge，原因写在 docs/BENCHMARK_REPORT.md 的局限性一节：
小规模数据集上 LLM 打分的方差大于它能带来的信息量。
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Iterable

from benchmarks.evaluators.metrics import mean, ratio

# 判定"模型承认资料里没有"的表述。只有当回答是**拒答**且没给出编造内容时才算正确。
REFUSAL_MARKERS = (
    "未提及",
    "没有提及",
    "未涉及",
    "未写明",
    "未说明",
    "未包含",
    "不包含",
    "没有相关",
    "未找到",
    "无法确定",
    "无法回答",
    "资料中未",
    "文档中未",
    "材料中未",
    "没有记载",
    "查不到",
)

# 回答里"可被核对的事实片段"：带单位的数字。
FACT_TOKEN_RE = re.compile(
    r"\d+(?:\.\d+)?\s*(?:%|万元|亿元|元|个工作日|工作日|天|周|小时|分钟|个月|年|页|分|次|台|家)"
)

_STRIP = re.compile(r"[\s\u3000,，、·\"'“”‘’]+")

# 产品要求模型在正文里以 ``[来源：文档名 · 第N页]`` 的形式标注出处。
# 引用指标必须从**回答正文**解析，而不能直接用接口返回的 citations 字段——
# 那个字段描述的是"检索上下文包含哪些页"，把它当引用会把检索覆盖率误当成引用准确率。
CITATION_RE = re.compile(r"来源\s*[:：]\s*(?P<name>[^·・|\[\]]+?)\s*[·・|]\s*第\s*(?P<page>\d+)\s*页")


def norm(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "")
    text = text.replace("－", "-").replace("—", "-").replace("–", "-")
    return _STRIP.sub("", text).lower()


def name_key(filename: str) -> str:
    """文档名归一化：兼容模型偶尔在文件名里插入的空格，以及是否带扩展名。"""
    base = unicodedata.normalize("NFKC", filename or "").strip()
    for ext in (".pdf", ".PDF", ".docx", ".txt", ".md"):
        if base.lower().endswith(ext.lower()):
            base = base[: -len(ext)]
            break
    return _STRIP.sub("", base).lower()


def parse_citations(
    answer: str, doc_index: dict[str, str] | None = None
) -> list[tuple[str, int, str]]:
    """从回答正文抽取引用，返回 ``[(doc_id, page, 原始文档名)]``。

    ``doc_index`` 是 ``name_key(文件名) -> doc_id``。解析不到的文档名会原样保留，
    因此"编造了一个不存在的文档"会自然算作引用错误，而不是被静默丢弃。
    """
    index = doc_index or {}
    found: list[tuple[str, int, str]] = []
    seen: set[tuple[str, int]] = set()
    for match in CITATION_RE.finditer(answer or ""):
        raw_name = match.group("name").strip()
        try:
            page = int(match.group("page"))
        except ValueError:
            continue
        doc_id = index.get(name_key(raw_name), raw_name)
        key = (doc_id, page)
        if key in seen:
            continue
        seen.add(key)
        found.append((doc_id, page, raw_name))
    return found



def match_key_groups(text: str, groups: Iterable[Iterable[str]]) -> tuple[bool, list[str]]:
    """每一组 key 至少命中一个候选表述才算通过；返回 (是否全中, 未命中的组)。"""
    haystack = norm(text)
    missing: list[str] = []
    for group in groups:
        alternatives = [alt for alt in group]
        if not any(norm(alt) in haystack for alt in alternatives):
            missing.append(" / ".join(alternatives))
    return (not missing), missing


def find_forbidden(text: str, patterns: Iterable[str]) -> list[str]:
    haystack = norm(text)
    hits: list[str] = []
    for pattern in patterns or []:
        try:
            if re.search(norm(pattern), haystack):
                hits.append(pattern)
        except re.error:  # pragma: no cover - 数据集里不应出现坏正则
            if norm(pattern) in haystack:
                hits.append(pattern)
    return hits


def is_refusal(answer: str) -> bool:
    return any(marker in (answer or "") for marker in REFUSAL_MARKERS)


def fact_tokens(answer: str) -> list[str]:
    """抽出带单位的数字事实，用于判断回答是否被检索上下文支撑。"""
    return [norm(m.group(0)) for m in FACT_TOKEN_RE.finditer(answer or "")]


def groundedness(answer: str, context_text: str) -> dict[str, Any]:
    """回答中的带单位数字是否都能在**检索上下文**里找到出处。

    这是一个偏严格的代理指标：它抓的是"凭空冒出来的数字"，
    不评判语义是否一致。上下文里没有的数字即使正确也算不支撑
    （例如题目要求做除法得到的比例），这一点在报告里如实说明。
    """
    tokens = fact_tokens(answer)
    if not tokens:
        return {"rate": 1.0, "total": 0, "unsupported": []}
    haystack = norm(context_text)
    unsupported = [t for t in tokens if t not in haystack]
    return {
        "rate": round(1 - len(unsupported) / len(tokens), 4),
        "total": len(tokens),
        "unsupported": sorted(set(unsupported))[:10],
    }


# ---------------------------------------------------------------------------
# 检索层
# ---------------------------------------------------------------------------


def gold_pairs(question: dict[str, Any]) -> set[tuple[str, int]]:
    pairs: set[tuple[str, int]] = set()
    for ref in question.get("gold_relevant_pages") or []:
        for page in ref["pages"]:
            pairs.add((ref["document_id"], int(page)))
    return pairs


def _hit_pairs(hits: list[dict[str, Any]]) -> list[tuple[str, int]]:
    return [(h["doc_id"], int(h["page"])) for h in hits]


def score_retrieval(question: dict[str, Any], hits: list[dict[str, Any]], ks=(1, 3, 5)) -> dict[str, Any]:
    """按页计算 Recall@K / Hit@K / MRR@5，以及多证据题的证据覆盖率。

    注意概念区分（报告里也会强调）：
    * ``hit@k``：Top-K 里**至少有一个**正确页就算命中；
    * ``recall@k``：Top-K 覆盖了**全部**标准证据页的比例。
    只有 ``recall`` 才回答"证据是否齐全"。
    """
    gold = gold_pairs(question)
    pairs = _hit_pairs(hits)
    result: dict[str, Any] = {
        "gold_pages": sorted(f"{d}:p{p}" for d, p in gold),
        "top_pages": [f"{d}:p{p}" for d, p in pairs],
    }
    if not gold:
        # 负例题没有标准证据页：只记录"是否仍然召回了片段"作为诊断，
        # 不参与 Recall 的平均（否则分母被污染）。
        result.update(
            {
                "answerable": False,
                "retrieved_any": bool(pairs),
                "top1_score": hits[0]["score"] if hits else 0.0,
                **{f"recall@{k}": None for k in ks},
                **{f"hit@{k}": None for k in ks},
                "mrr@5": None,
            }
        )
        return result

    for k in ks:
        window = pairs[:k]
        covered = gold & set(window)
        result[f"recall@{k}"] = round(len(covered) / len(gold), 4)
        result[f"hit@{k}"] = 1.0 if covered else 0.0

    mrr = 0.0
    for rank, pair in enumerate(pairs[:5], start=1):
        if pair in gold:
            mrr = 1.0 / rank
            break
    result["mrr@5"] = round(mrr, 4)
    result["answerable"] = True
    result["covered_pages"] = sorted(f"{d}:p{p}" for d, p in (gold & set(pairs[:5])))
    result["missing_pages"] = sorted(f"{d}:p{p}" for d, p in (gold - set(pairs[:5])))
    return result


def aggregate_retrieval(cases: list[dict[str, Any]], ks=(1, 3, 5)) -> dict[str, Any]:
    scored = [c["retrieval"] for c in cases if c["retrieval"].get("answerable")]
    negatives = [c for c in cases if not c["retrieval"].get("answerable")]
    agg: dict[str, Any] = {
        "answerable_questions": len(scored),
        "negative_questions": len(negatives),
    }
    for k in ks:
        agg[f"recall@{k}"] = mean(r[f"recall@{k}"] for r in scored)
        agg[f"hit@{k}"] = mean(r[f"hit@{k}"] for r in scored)
    agg["mrr@5"] = mean(r["mrr@5"] for r in scored)
    # 完全命中全部证据页的比例（比 Recall 更严格）
    agg["full_evidence_rate@5"] = mean(1.0 if r["recall@5"] == 1.0 else 0.0 for r in scored)
    agg["multi_evidence_questions"] = sum(1 for r in scored if len(r["gold_pages"]) > 1)
    agg["negative_retrieved_any_rate"] = mean(
        1.0 if n["retrieval"].get("retrieved_any") else 0.0 for n in negatives
    )
    return agg


def aggregate_by_type(cases: list[dict[str, Any]]) -> dict[str, Any]:
    buckets: dict[str, list[dict[str, Any]]] = {}
    for case in cases:
        buckets.setdefault(case["question_type"], []).append(case)
    out: dict[str, Any] = {}
    for name, items in sorted(buckets.items()):
        positives = [c for c in items if c["retrieval"].get("answerable")]
        executed = [c for c in items if c["answer"].get("executed")]
        executed_pos = [c for c in executed if c["answerable"]]
        executed_neg = [c for c in executed if not c["answerable"]]
        out[name] = {
            "count": len(items),
            "answerable_count": len(positives),
            # 负例题没有标准证据页，Recall/MRR 记 None 而不是 0，
            # 否则纯负例题型会在表格里显示成"检索全错"。
            "recall@5": mean(c["retrieval"]["recall@5"] for c in positives) if positives else None,
            "mrr@5": mean(c["retrieval"]["mrr@5"] for c in positives) if positives else None,
            "answer_accuracy": (
                mean(1.0 if c["answer"].get("correct") else 0.0 for c in executed_pos)
                if executed_pos
                else None
            ),
            "abstention_accuracy": (
                mean(1.0 if c["answer"].get("correct") else 0.0 for c in executed_neg)
                if executed_neg
                else None
            ),
            "end_to_end": (
                mean(1.0 if c["answer"].get("correct") else 0.0 for c in executed)
                if executed
                else None
            ),
        }
    return out


# ---------------------------------------------------------------------------
# 回答层
# ---------------------------------------------------------------------------


def score_answer(
    question: dict[str, Any],
    response: dict[str, Any],
    context_text: str,
    doc_index: dict[str, str] | None = None,
) -> dict[str, Any]:
    """对一次真实问答结果打分。

    ``response`` 来自 ``BrainService.answer``：{answer, citations, grounded, degraded}。
    其中 ``citations`` 只是"检索上下文"，仅作诊断；真正的引用指标从回答正文解析。
    """
    answer = response.get("answer") or ""
    context_pages = response.get("citations") or []
    gold = gold_pairs(question)
    parsed = parse_citations(answer, doc_index)
    cited = {(doc_id, page) for doc_id, page, _ in parsed}
    answerable = bool(question.get("answerable"))
    refusal = is_refusal(answer)

    result: dict[str, Any] = {
        "answer": answer,
        "citations": [f"{name} 第{page}页" for _, page, name in parsed],
        "unresolved_citations": [name for doc_id, _, name in parsed if doc_id == name],
        "context_pages": [f"{c['doc_name']} 第{c['page']}页" for c in context_pages],
        "degraded": bool(response.get("degraded")),
        "refusal": refusal,
        "groundedness": groundedness(answer, context_text),
        "citation_precision": round(ratio(len(cited & gold), len(cited)), 4) if cited else 0.0,
        "citation_recall": round(ratio(len(cited & gold), len(gold)), 4) if gold else None,
        "citation_count": len(cited),
    }

    if answerable:
        keys_ok, missing = match_key_groups(answer, question.get("answer_keys") or [])
        forbidden = find_forbidden(answer, question.get("forbidden") or [])
        # 判定标准只看「关键事实是否齐备且无禁用内容」。
        # 不把「提到资料未涉及」当错：模型在给出正确答案后补充一句
        # “手册未说明是向谁打招呼”是严谨表述，不是拒答。
        result["answer_correct"] = bool(keys_ok and not forbidden)
        result["missing_keys"] = missing
        result["forbidden_hits"] = forbidden
        strict = {
            (ref["document_id"], int(page))
            for ref in question.get("strict_citation_pages") or []
            for page in ref["pages"]
        }
        result["strict_citation_ok"] = (strict <= cited) if strict else True
        result["citation_exact"] = bool(cited & gold) and result["citation_precision"] >= 1.0
        result["correct"] = result["answer_correct"]
    else:
        forbidden = find_forbidden(answer, question.get("forbidden") or [])
        result["forbidden_hits"] = forbidden
        result["abstained"] = refusal
        result["answer_correct"] = bool(refusal and not forbidden)
        result["correct"] = result["answer_correct"]
        result["missing_keys"] = []
        result["strict_citation_ok"] = True
        result["citation_exact"] = None

    result["executed"] = True
    return result


def aggregate_answers(cases: list[dict[str, Any]]) -> dict[str, Any]:
    answered = [c for c in cases if c["answer"].get("executed")]
    positives = [c for c in answered if c["answerable"]]
    negatives = [c for c in answered if not c["answerable"]]
    if not answered:
        return {"executed": 0}
    return {
        "executed": len(answered),
        "answerable_executed": len(positives),
        "negative_executed": len(negatives),
        "answer_accuracy": mean(1.0 if c["answer"]["answer_correct"] else 0.0 for c in positives)
        if positives
        else None,
        # 拒答率只在有负例题时才有意义；强行算成 0% 会让人误以为“从不拒答”。
        "abstention_accuracy": mean(
            1.0 if c["answer"]["answer_correct"] else 0.0 for c in negatives
        )
        if negatives
        else None,
        "citation_precision": mean(c["answer"]["citation_precision"] for c in positives)
        if positives
        else None,
        "citation_recall": mean(c["answer"]["citation_recall"] or 0.0 for c in positives)
        if positives
        else None,
        "strict_citation_accuracy": mean(
            1.0 if c["answer"]["strict_citation_ok"] else 0.0 for c in positives
        )
        if positives
        else None,
        "groundedness": mean(c["answer"]["groundedness"]["rate"] for c in answered),
        "no_citation_responses": sum(
            1 for c in positives if not (c["answer"].get("citation_count") or 0)
        ),
        "degraded_responses": sum(1 for c in answered if c["answer"]["degraded"]),
        "end_to_end_success": mean(1.0 if c["answer"]["correct"] else 0.0 for c in answered),
    }
