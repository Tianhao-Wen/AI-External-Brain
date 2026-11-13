"""RAG 套件运行器：真实上传 → 真实 BM25 检索 → 真实 LLM 问答 → 规则评分。

fixture 模式只跑检索层（不需要模型，结果可完全复现）；
live 模式在此之上再跑真实问答链路，回答质量指标只在 live 模式产出。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from benchmarks import common
from benchmarks.evaluators import rag as rag_eval
from benchmarks.evaluators.metrics import percent

RAG_DIR = common.DATASETS_DIR / "rag"
DOC_DIR = RAG_DIR / "documents"
UNSEEN_DIR = RAG_DIR / "unseen"


def load_split_ids(split: str) -> set[str] | None:
    """返回切分对应的题号集合；``all`` 返回 None 表示不过滤。"""
    if split in ("", "all"):
        return None
    path = RAG_DIR / "splits.json"
    if not path.is_file():
        raise FileNotFoundError("缺少 rag/splits.json，请先运行 python -m benchmarks.splits")
    payload = json.loads(path.read_text(encoding="utf-8"))
    return set(payload["splits"].get(split) or [])


def load_questions(
    limit: int = 0, only: list[str] | None = None, split: str = "all"
) -> list[dict[str, Any]]:
    path = UNSEEN_DIR / "questions.jsonl" if split == "test_unseen" else RAG_DIR / "questions.jsonl"
    if not path.is_file():
        raise FileNotFoundError(f"缺少题目文件 {path}")
    questions = common.read_jsonl(path)
    ids = load_split_ids(split)
    if ids is not None and split != "test_unseen":
        questions = [q for q in questions if q["question_id"] in ids]
    if only:
        wanted = {q.strip() for q in only}
        questions = [q for q in questions if q["question_id"] in wanted]
    if limit:
        questions = questions[:limit]
    return questions


def document_specs(split: str) -> tuple[dict[str, dict], Path]:
    """返回 (文档定义, 文档目录)。未暴露集用全新文档，其它切分用开发集文档。"""
    if split == "test_unseen":
        from benchmarks.datasets.rag.unseen.build_unseen import DOCS

        return DOCS, UNSEEN_DIR / "documents"
    from benchmarks.datasets.rag.build_documents import DOCUMENTS

    return DOCUMENTS, DOC_DIR


def _index_documents(service, ctx, split: str) -> dict[str, str]:
    """把数据集 PDF 通过产品真实的 ``BrainService.upload`` 入库。"""
    specs, doc_dir = document_specs(split)
    mapping: dict[str, str] = {}
    for doc_id, spec in specs.items():
        path = doc_dir / spec["filename"]
        if not path.is_file():
            raise FileNotFoundError(
                f"缺少数据集文档 {path}，请先运行对应的 build 脚本生成"
            )
        record = service.upload(path.name, path.read_bytes())
        if record.get("status") != "ready":
            raise RuntimeError(f"{path.name} 入库失败：{record.get('status')} {record.get('error')}")
        mapping[doc_id] = record["id"]
        ctx.notes.append(f"{doc_id} -> {record['id']} ({record.get('pages')}页)")
    return mapping


def _resolve_question(question: dict[str, Any], mapping: dict[str, str]) -> dict[str, Any]:
    """把数据集里的 ``DOC-A`` 别名换成产品真实分配的 ``doc_xxx`` 存储 ID。

    数据集与运行时 ID 解耦，保证同一个数据集可以被反复上传到新库而不失效。
    """
    resolved = dict(question)
    refs = []
    for ref in question.get("gold_relevant_pages") or []:
        item = dict(ref)
        item["document_id"] = mapping.get(ref["document_id"], ref["document_id"])
        refs.append(item)
    resolved["gold_relevant_pages"] = refs
    resolved["document_ids"] = [mapping.get(d, d) for d in question.get("document_ids") or []]
    resolved["_alias"] = {v: k for k, v in mapping.items()}
    return resolved


def _humanise(retrieval: dict[str, Any], alias: dict[str, str]) -> dict[str, Any]:
    """把打分结果里的 ``doc_xxx:p3`` 还原为 ``DOC-A:p3``，方便人读。"""

    def fix(ref: str) -> str:
        doc_id, _, page = ref.partition(":")
        return f"{alias.get(doc_id, doc_id)}:{page}"

    out = dict(retrieval)
    for key in ("gold_pages", "top_pages", "covered_pages", "missing_pages"):
        if isinstance(out.get(key), list):
            out[key] = [fix(item) for item in out[key]]
    return out


async def run(
    ctx,
    *,
    limit: int = 0,
    only: list[str] | None = None,
    top_k: int = 5,
    retrieval_k: int = 8,
    split: str = "all",
) -> dict[str, Any]:
    from aieb.brain import knowledge
    from aieb.brain.service import BrainService

    questions = load_questions(limit=limit, only=only, split=split)
    if not questions:
        raise RuntimeError(f"split={split} 没有匹配题目")
    workspace = ctx.workspace("rag")
    service = BrainService(work_dir=workspace, provider=ctx.provider)
    try:
        mapping = _index_documents(service, ctx, split)
        alias = {v: k for k, v in mapping.items()}
        specs, _doc_dir = document_specs(split)

        # 回答正文里写的是文件名，需要反查到存储 doc_id 才能与标准证据页比对。
        name_index = {
            rag_eval.name_key(spec["filename"]): mapping[doc_id]
            for doc_id, spec in specs.items()
            if doc_id in mapping
        }
        cases: list[dict[str, Any]] = []
        run_answers = ctx.mode == "live"

        for index, raw_question in enumerate(questions, start=1):
            question = _resolve_question(raw_question, mapping)
            started = time.perf_counter()
            hits = service.search(question["question"], top_k=max(retrieval_k, top_k))
            retrieval_ms = (time.perf_counter() - started) * 1000
            retrieval = _humanise(rag_eval.score_retrieval(question, hits), alias)

            answer_payload: dict[str, Any] = {"executed": False}
            answer_ms = 0.0
            context_text = ""
            if run_answers:
                started = time.perf_counter()
                try:
                    context_text = knowledge.build_context(hits[:top_k])
                    response = await service.answer(question["question"], top_k=top_k)
                    answer_ms = (time.perf_counter() - started) * 1000
                    answer_payload = rag_eval.score_answer(
                        question, response, context_text, doc_index=name_index
                    )
                    answer_payload["context_chars"] = len(context_text)
                except common.BudgetExceeded as exc:
                    answer_payload = {"executed": False, "error": f"budget: {exc}"}
                except Exception as exc:  # noqa: BLE001 - 失败样本必须保留在分母里
                    answer_ms = (time.perf_counter() - started) * 1000
                    answer_payload = {
                        "executed": True,
                        "error": f"{type(exc).__name__}: {exc}",
                        "answer": "",
                        "answer_correct": False,
                        "correct": False,
                        "citation_precision": 0.0,
                        "citation_recall": 0.0,
                        "citation_count": 0,
                        "citations": [],
                        "strict_citation_ok": False,
                        "groundedness": {"rate": 0.0, "total": 0, "unsupported": []},
                        "refusal": False,
                        "degraded": False,
                        "missing_keys": [],
                        "forbidden_hits": [],
                    }

            cases.append(
                {
                    "question_id": question["question_id"],
                    "question": question["question"],
                    "question_type": question["question_type"],
                    "answerable": bool(question["answerable"]),
                    "difficulty": question["difficulty"],
                    "document_ids": raw_question.get("document_ids")
                    or sorted({ref["document_id"] for ref in raw_question.get("gold_relevant_pages") or []}),
                    "gold_answer": question["gold_answer"],
                    "gold_pages": retrieval["gold_pages"],
                    "top_hits": [
                        {
                            "doc_id": alias.get(h["doc_id"], h["doc_id"]),
                            "doc_name": h["doc_name"],
                            "page": h["page"],
                            "score": h["score"],
                        }
                        for h in hits[:retrieval_k]
                    ],
                    "retrieval": retrieval,
                    "answer": answer_payload,
                    "latency": {
                        "retrieval_ms": round(retrieval_ms, 1),
                        "answer_ms": round(answer_ms, 1),
                    },
                    "index": index,
                }
            )
            print(
                f"  [{index}/{len(questions)}] {question['question_id']} "
                f"{question['question_type']:<18} recall@5={retrieval.get('recall@5')} "
                f"{'answer=' + ('ok' if answer_payload.get('answer_correct') else 'x') if run_answers else ''}",
                flush=True,
            )

        aggregates = {
            "retrieval": rag_eval.aggregate_retrieval(cases),
            "answer": rag_eval.aggregate_answers(cases),
            "by_type": rag_eval.aggregate_by_type(cases),
            "latency": {
                "retrieval_p50_ms": common.percentile(
                    [c["latency"]["retrieval_ms"] for c in cases], 50
                ),
                "retrieval_p95_ms": common.percentile(
                    [c["latency"]["retrieval_ms"] for c in cases], 95
                ),
                "answer_p50_ms": common.percentile(
                    [c["latency"]["answer_ms"] for c in cases if c["answer"].get("executed")], 50
                ),
                "answer_p95_ms": common.percentile(
                    [c["latency"]["answer_ms"] for c in cases if c["answer"].get("executed")], 95
                ),
            },
        }
        return {"cases": cases, "aggregates": aggregates, "mode": ctx.mode}
    finally:
        service.store.close()


# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------

CSV_COLUMNS = [
    "question_id",
    "question_type",
    "difficulty",
    "answerable",
    "question",
    "gold_answer",
    "gold_pages",
    "top1",
    "recall@1",
    "recall@3",
    "recall@5",
    "hit@5",
    "mrr@5",
    "missing_pages",
    "answer_correct",
    "model_answer",
    "citations",
    "citation_precision",
    "strict_citation_ok",
    "groundedness",
    "refusal",
    "missing_keys",
    "error",
]


def to_csv_rows(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for case in cases:
        ret = case["retrieval"]
        ans = case.get("answer") or {}
        top1 = (case["top_hits"][0] if case["top_hits"] else None)
        rows.append(
            {
                "question_id": case["question_id"],
                "question_type": case["question_type"],
                "difficulty": case["difficulty"],
                "answerable": case["answerable"],
                "question": case["question"],
                "gold_answer": case["gold_answer"],
                "gold_pages": ret["gold_pages"],
                "top1": f"{top1['doc_name']} p{top1['page']} ({top1['score']})" if top1 else "",
                "recall@1": ret.get("recall@1"),
                "recall@3": ret.get("recall@3"),
                "recall@5": ret.get("recall@5"),
                "hit@5": ret.get("hit@5"),
                "mrr@5": ret.get("mrr@5"),
                "missing_pages": ret.get("missing_pages"),
                "answer_correct": ans.get("answer_correct"),
                "model_answer": common.redact(str(ans.get("answer") or ""))[:800],
                "citations": ans.get("citations"),
                "citation_precision": ans.get("citation_precision"),
                "strict_citation_ok": ans.get("strict_citation_ok"),
                "groundedness": (ans.get("groundedness") or {}).get("rate"),
                "refusal": ans.get("refusal"),
                "missing_keys": ans.get("missing_keys"),
                "error": ans.get("error", ""),
            }
        )
    return rows


def summary_markdown(result: dict[str, Any], ctx) -> str:
    agg = result["aggregates"]
    ret = agg["retrieval"]
    ans = agg["answer"]
    lines = [
        "# RAG 评测结果摘要",
        "",
        f"- 运行 ID：`{ctx.run_id}`",
        f"- 模式：`{ctx.mode}`（fixture = 仅检索层，无模型调用；live = 含真实模型问答）",
        f"- 模型：`{ctx.model.model}`（provider `{ctx.model.name}`）",
        f"- Git commit：`{ctx.git.get('commit_short')}`",
        f"- 数据集版本：`{common.DATASET_VERSION}`",
        "",
        "## 检索层",
        "",
        f"- 可回答问题数：{ret['answerable_questions']}；负例题数：{ret['negative_questions']}",
        f"- Recall@1：{percent(ret['recall@1'])}　Recall@3：{percent(ret['recall@3'])}　Recall@5：{percent(ret['recall@5'])}",
        f"- Hit@5：{percent(ret['hit@5'])}　MRR@5：{ret['mrr@5']:.4f}",
        f"- 全部证据页都命中（FullEvidence@5）：{percent(ret['full_evidence_rate@5'])}"
        f"（其中多证据题 {ret['multi_evidence_questions']} 道）",
        f"- 负例题仍召回到片段的比例：{percent(ret['negative_retrieved_any_rate'])}"
        "（BM25 无相似度阈值，总会返回 Top-K，此项仅作诊断，不计入 Recall）",
        "",
        "> Hit@5 与 Recall@5 不是一回事：Hit@5 只看「有没有命中一个正确页」，"
        "Recall@5 看「标准证据是否被全部覆盖」。多证据题只命中一个证据不算完整成功。",
        "",
    ]
    if ans.get("executed"):
        def show(value, fmt=percent):
            return "—" if value is None else fmt(value)

        lines += [
            "## 回答层（真实模型）",
            "",
            f"- 已执行问答：{ans['executed']} 题"
            f"（可回答 {ans.get('answerable_executed')} / 负例 {ans.get('negative_executed')}）",
            f"- 答案事实正确率：{show(ans['answer_accuracy'])}",
            f"- 无答案题正确拒答率：{show(ans['abstention_accuracy'])}",
            f"- 引用准确率（回答引用页中确为标准页的占比）：{show(ans['citation_precision'])}",
            f"- 引用覆盖率：{show(ans['citation_recall'])}",
            f"- 强制页码引用题正确率：{show(ans['strict_citation_accuracy'])}",
            f"- Groundedness（带单位数字能在原文找到的比例）：{show(ans['groundedness'])}",
            f"- 端到端正确率（事实 + 拒答 综合）：{show(ans['end_to_end_success'])}",
            f"- 未给出任何出处的回答：{ans.get('no_citation_responses', 0)} 题",
            f"- 降级回答（模型不可用转原文摘录）：{ans['degraded_responses']} 题",
            "",
            "> 引用指标从回答正文的 `[来源：文档名 · 第N页]` 解析，"
            "不使用接口返回的检索上下文页，避免把“检索到了”误算成“引用对了”。",
            "",
        ]
    else:
        lines += [
            "## 回答层",
            "",
            "本次为 fixture 模式，**未调用模型**，因此不产出答案正确率 / 引用正确率 / 拒答率。",
            "这些指标只有 live 模式才有意义；fixture 结果不能当作模型能力。",
            "",
        ]
    lines += [
        "## 按题型",
        "",
        "| 题型 | 题数 | 可回答数 | Recall@5 | MRR@5 | 答案正确率 | 正确拒答率 | 综合正确率 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for name, item in agg["by_type"].items():
        recall = "—" if item["recall@5"] is None else percent(item["recall@5"])
        mrr = "—" if item["mrr@5"] is None else f"{item['mrr@5']:.4f}"
        acc = "—" if item.get("answer_accuracy") is None else percent(item["answer_accuracy"])
        abst = "—" if item.get("abstention_accuracy") is None else percent(item["abstention_accuracy"])
        e2e = "—" if item.get("end_to_end") is None else percent(item["end_to_end"])
        lines.append(
            f"| {name} | {item['count']} | {item.get('answerable_count', item['count'])} | "
            f"{recall} | {mrr} | {acc} | {abst} | {e2e} |"
        )
    lines += [
        "",
        "## 失败案例（Recall@5 未满分）",
        "",
    ]
    failures = [c for c in result["cases"] if c["retrieval"].get("answerable") and (c["retrieval"]["recall@5"] or 0) < 1.0]
    if not failures:
        lines.append("无。")
    for case in failures[:20]:
        lines.append(
            f"- `{case['question_id']}`（{case['question_type']}）缺失证据页："
            f"{'、'.join(case['retrieval'].get('missing_pages') or [])}；"
            f"Top1：{case['top_hits'][0]['doc_name'] + ' 第' + str(case['top_hits'][0]['page']) + '页' if case['top_hits'] else '无'}"
        )
    lines.append("")
    return "\n".join(lines)


def write_outputs(result: dict[str, Any], ctx) -> dict[str, Path]:
    payload = {
        "meta": ctx.meta(),
        "aggregates": result["aggregates"],
        "cases": result["cases"],
    }
    return {
        "json": common.write_json(common.RESULTS_DIR / "rag_results.json", payload),
        "csv": common.write_csv(
            common.RESULTS_DIR / "rag_cases.csv", to_csv_rows(result["cases"]), CSV_COLUMNS
        ),
        "summary": common.write_markdown(
            common.RESULTS_DIR / "rag_summary.md", summary_markdown(result, ctx)
        ),
    }
