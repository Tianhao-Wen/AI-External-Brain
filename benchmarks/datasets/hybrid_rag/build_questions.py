"""从语料参数表生成可核验的问答集（约 250 题，覆盖任务书要求的 12 类）。

关键约束：

* **答案不来自被测系统**，而是来自 `build_corpus.py` 生成正文时用的同一份参数表
  （`facts`）。答案与正文同源，但**不是** RAG 的输出。
* 每道题都写明 `gold_relevant_pages`；`answer_keys` 里的字符串必须真的出现在
  标注的那一页上（由 `validate_dataset.py` 独立回读 PDF 校验）。
* 负例题的 `forbidden` 词会被校验为"整个语料里都不存在"，保证它真的是无答案。
* 确定性：不使用随机数，重跑得到完全相同的结果。

运行：
    python -m benchmarks.datasets.hybrid_rag.build_questions
"""

from __future__ import annotations

import itertools
from collections import Counter
from pathlib import Path
from typing import Any

from benchmarks import common
from benchmarks.datasets.hybrid_rag.build_corpus import DocumentSpec, build_documents

DATASET_DIR = common.DATASETS_DIR / "hybrid_rag"
QUESTIONS_PATH = DATASET_DIR / "questions.jsonl"
SPLITS_PATH = DATASET_DIR / "splits.json"

#: 按文档序号划分（1-based）：1-60 dev / 61-80 validation / 81-100 test
SPLIT_RANGES: dict[str, tuple[int, int]] = {
    "dev": (1, 60),
    "validation": (61, 80),
    "test": (81, 100),
}

#: 事实键 -> (提问句式, 额外可接受写法)
#: 多个候选项是必要的：模型答"五个"和"五个里程碑"都对，
#: 只认一种写法会把正确答案判错（上一轮实验已经踩过这个坑）。
FACT_QUESTIONS: dict[str, tuple[str, list[str]]] = {
    "approval": ("单一合同金额超过多少万元需要部门负责人与财务负责人联合审批？", []),
    "payment": ("质保金的比例是多少？", []),
    "late": ("供应商逾期交付的违约金累计上限是合同总额的多少？", []),
    "review": ("收到完整材料后多少个工作日内完成初审？", []),
    "supplier": ("合格供应商的注册资本门槛是多少？", []),
    "sample": ("来料检验的最低抽检比例是多少？", []),
    "milestone": ("项目划分为几个里程碑？", ["五个", "5 个", "5个"]),
    "delay": ("整体进度落后计划多少个工作日以上需要提交纠偏计划？", []),
    "budget": ("年度预算上限是多少？", []),
    "travel": ("一线城市住宿标准为每晚不超过多少元？", []),
    "confidential": ("保密义务在合同终止后继续有效多少年？", []),
    "code": ("本年度新立项应使用的项目编号格式是什么？", []),
}

TYPE_DESCRIPTIONS = {
    "numeric": "单页数值题：一个具体阈值/比例/时限",
    "single_page": "单页条款题：供应商准入类条款",
    "paraphrase": "中文同义改写：提问用词与正文不同",
    "multi_page": "多证据联合：同一文档两页的两个事实",
    "cross_document": "跨文档交叉：采购制度 + 质量制度",
    "product_code": "相似编号：正文含末尾多一个 0 的历史归档编号",
    "long_distractor": "干扰：同部门其它主体措辞相同、数值不同的条款",
    "conflict": "新旧版本冲突：V1/V2 数值不同，须以最新版为准",
    "negative": "无答案负例：语料中不存在相关信息",
}


def _split_for(doc_id: str) -> str:
    seq = int(doc_id.split("-")[1])
    for name, (low, high) in SPLIT_RANGES.items():
        if low <= seq <= high:
            return name
    return "test"


def _ref(doc: DocumentSpec, page: int) -> dict[str, Any]:
    return {"document_id": doc.doc_id, "pages": [int(page)]}


def _row(
    *,
    question: str,
    question_type: str,
    doc: DocumentSpec,
    gold: list[dict[str, Any]],
    answer_keys: list[list[str]],
    gold_answer: str,
    difficulty: str = "medium",
    forbidden: list[str] | None = None,
    answerable: bool = True,
    note: str = "",
) -> dict[str, Any]:
    return {
        "question_id": "",
        "question": question,
        "question_type": question_type,
        "answerable": answerable,
        "difficulty": difficulty,
        "document_ids": sorted({ref["document_id"] for ref in gold}),
        "gold_relevant_pages": gold,
        "answer_keys": answer_keys,
        "gold_answer": gold_answer,
        "forbidden": forbidden or [],
        "split": _split_for(doc.doc_id),
        "split_basis_document": doc.doc_id,
        "note": note,
    }


def _keys_for(kind: str, value: str) -> list[str]:
    return [value, *FACT_QUESTIONS.get(kind, ("", []))[1]]


def _fact_question(doc: DocumentSpec, kind: str, *, qtype: str = "numeric") -> dict[str, Any] | None:
    if kind not in doc.facts:
        return None
    value = doc.facts[kind]
    template = FACT_QUESTIONS[kind][0]
    return _row(
        question=f"《{doc.title}》中，{template}",
        question_type=qtype,
        doc=doc,
        gold=[_ref(doc, doc.fact_pages[kind])],
        answer_keys=[_keys_for(kind, value)],
        gold_answer=value,
        difficulty="easy" if qtype == "numeric" else "medium",
        note=f"事实键={kind}",
    )


def _multi_page_question(doc: DocumentSpec) -> dict[str, Any] | None:
    """两页各取一个事实，要求同时回答。"""
    by_page: dict[int, list[str]] = {}
    for kind, page in doc.fact_pages.items():
        by_page.setdefault(page, []).append(kind)
    pages = sorted(by_page)
    if len(pages) < 2:
        return None
    first, second = pages[0], pages[-1]
    kind_a = sorted(by_page[first])[0]
    kind_b = sorted(by_page[second])[0]
    if kind_a == kind_b or kind_a not in FACT_QUESTIONS or kind_b not in FACT_QUESTIONS:
        return None
    return _row(
        question=(
            f"《{doc.title}》中，{FACT_QUESTIONS[kind_a][0]}"
            f"另外，{FACT_QUESTIONS[kind_b][0]}"
        ),
        question_type="multi_page",
        doc=doc,
        gold=[_ref(doc, first), _ref(doc, second)],
        answer_keys=[
            _keys_for(kind_a, doc.facts[kind_a]),
            _keys_for(kind_b, doc.facts[kind_b]),
        ],
        gold_answer=f"{doc.facts[kind_a]}；{doc.facts[kind_b]}",
        difficulty="hard",
        note=f"多证据：第 {first} 页与第 {second} 页",
    )


def _product_code_question(doc: DocumentSpec) -> dict[str, Any] | None:
    if "code" not in doc.facts:
        return None
    value = doc.facts["code"]
    return _row(
        question=f"{doc.company}本年度新立项应使用的项目编号格式是什么？",
        question_type="product_code",
        doc=doc,
        gold=[_ref(doc, doc.fact_pages["code"])],
        answer_keys=[[value]],
        gold_answer=value,
        difficulty="medium",
        forbidden=[f"{value}0"],
        note="相似编号：正文另含一个末尾多 0 的历史归档编号",
    )


def _conflict_question(v1: DocumentSpec, v2: DocumentSpec) -> dict[str, Any] | None:
    if "late" not in v2.facts or v1.facts.get("late") == v2.facts.get("late"):
        return None
    return _row(
        question=(
            f"{v2.company}《{v2.title}》中，供应商逾期交付的违约金累计上限是多少？"
            "请按最新版本回答。"
        ),
        question_type="conflict",
        doc=v2,
        gold=[_ref(v2, v2.fact_pages["late"])],
        answer_keys=[[v2.facts["late"]]],
        gold_answer=v2.facts["late"],
        difficulty="hard",
        forbidden=[f"累计不超过合同总额的{v1.facts['late']}"],
        note=f"新旧冲突：V1={v1.facts['late']}，V2={v2.facts['late']}",
    )


def _cross_document_question(buyer: DocumentSpec, quality: DocumentSpec) -> dict[str, Any] | None:
    if "late" not in buyer.facts or "sample" not in quality.facts:
        return None
    return _row(
        question=(
            f"在{buyer.company}，供应商逾期交付的违约金累计上限是多少？"
            "另外，来料检验的最低抽检比例是多少？"
        ),
        question_type="cross_document",
        doc=buyer,
        gold=[
            _ref(buyer, buyer.fact_pages["late"]),
            _ref(quality, quality.fact_pages["sample"]),
        ],
        answer_keys=[[buyer.facts["late"]], [quality.facts["sample"]]],
        gold_answer=f"{buyer.facts['late']}；{quality.facts['sample']}",
        difficulty="hard",
        note=f"跨文档：{buyer.doc_id} + {quality.doc_id}",
    )


def _negative_question(doc: DocumentSpec) -> dict[str, Any]:
    return _row(
        question=f"《{doc.title}》规定的员工带薪年假天数和年终奖发放比例分别是多少？",
        question_type="negative",
        doc=doc,
        gold=[],
        answer_keys=[],
        gold_answer="（语料中不存在，应明确说明未提及）",
        difficulty="medium",
        answerable=False,
        forbidden=["年终奖发放比例为", "带薪年假为"],
        note="负例：全语料都不涉及年假与年终奖",
    )


def build_questions(
    specs: list[DocumentSpec] | None = None,
    *,
    fact_questions: dict[str, tuple[str, list[str]]] | None = None,
    qid_prefix: str = "HQA-",
) -> list[dict[str, Any]]:
    """生成问答集。

    `fact_questions` 允许传入另一套**措辞不同**的提问模板 —— 独立留出集用它，
    避免“题面与开发集一字不差”导致的人为乐观。
    """
    global FACT_QUESTIONS
    templates = fact_questions or FACT_QUESTIONS
    original_templates = FACT_QUESTIONS
    if fact_questions is not None:
        FACT_QUESTIONS = fact_questions
    try:
        return _build_questions_inner(specs, qid_prefix=qid_prefix)
    finally:
        FACT_QUESTIONS = original_templates


def _build_questions_inner(
    specs: list[DocumentSpec] | None = None, *, qid_prefix: str = "HQA-"
) -> list[dict[str, Any]]:
    specs = specs or build_documents()
    by_key: dict[tuple[str, str, str], DocumentSpec] = {
        (s.dept_code, s.company, s.version): s for s in specs
    }
    v1_docs = [s for s in specs if s.version == "v1"]

    rows: list[dict[str, Any]] = []
    counter = itertools.count(1)

    def add(row: dict[str, Any] | None) -> None:
        if row is None:
            return
        row["question_id"] = f"{qid_prefix}{next(counter):03d}"
        rows.append(row)

    for doc_index, doc in enumerate(v1_docs):
        fact_kinds = [k for k in FACT_QUESTIONS if k in doc.facts and k != "code"]
        if fact_kinds:
            add(_fact_question(doc, fact_kinds[0]))
        if doc_index % 3 == 0 and len(fact_kinds) > 1:
            add(_fact_question(doc, fact_kinds[1]))
        add(_fact_question(doc, "supplier", qtype="single_page"))

        paraphrase = _fact_question(doc, "review", qtype="paraphrase")
        if paraphrase is not None:
            paraphrase["question"] = f"材料交齐之后，{doc.company}那边大概要等多久才能拿到初审结果？"
            paraphrase["note"] = "同义改写：正文写作「收到完整材料后 N 个工作日内完成初审」"
            add(paraphrase)

        distractor = _fact_question(doc, "sample", qtype="long_distractor")
        if distractor is not None:
            distractor["question"] = f"{doc.company}对来料检验的最低抽检比例要求是多少？"
            distractor["note"] = "干扰：同一部门其它主体的条款措辞相同、数值不同"
            add(distractor)

        add(_product_code_question(doc))
        add(_multi_page_question(doc))
        if doc_index % 10 == 0:
            add(_negative_question(doc))

    for doc in v1_docs:
        v2 = by_key.get((doc.dept_code, doc.company, "v2"))
        if v2 is None or _split_for(doc.doc_id) != _split_for(v2.doc_id):
            continue
        add(_conflict_question(doc, v2))

    for company in sorted({s.company for s in v1_docs}):
        for buyer_dept in ("CG", "GY"):
            buyer = by_key.get((buyer_dept, company, "v1"))
            quality = by_key.get(("ZL", company, "v1"))
            if not buyer or not quality:
                continue
            if _split_for(buyer.doc_id) != _split_for(quality.doc_id):
                continue
            add(_cross_document_question(buyer, quality))

    return rows


def write_dataset() -> tuple[Path, Path]:
    specs = build_documents()
    rows = build_questions(specs)
    common.write_jsonl(QUESTIONS_PATH, rows)
    splits: dict[str, list[str]] = {"dev": [], "validation": [], "test": []}
    for row in rows:
        splits[row["split"]].append(row["question_id"])
    common.write_json(
        SPLITS_PATH,
        {
            "basis": "按文档序号划分：1-60 dev / 61-80 validation / 81-100 test（题目随其基准文档）",
            "frozen": True,
            "counts": {k: len(v) for k, v in splits.items()},
            "type_descriptions": TYPE_DESCRIPTIONS,
            "splits": splits,
        },
    )
    return QUESTIONS_PATH, SPLITS_PATH


if __name__ == "__main__":  # pragma: no cover
    qpath, spath = write_dataset()
    rows = common.read_jsonl(qpath)
    print(f"题目 {len(rows)} 道 → {qpath}")
    print("题型分布:", dict(Counter(r["question_type"] for r in rows)))
    print("切分:", dict(Counter(r["split"] for r in rows)))
    print(f"切分文件 → {spath}")
