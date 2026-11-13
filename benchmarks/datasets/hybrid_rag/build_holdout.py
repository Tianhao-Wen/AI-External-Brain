"""构造**独立留出集**（holdout）：用于最终一次性验证。

为什么需要它：任务一要求"如 test 集已暴露，应如实标记并建立新的独立评测集"。
审计结果（`benchmarks/results/hybrid_rag/split_exposure_audit.json`）显示
主数据集的 49 道 test 题**已经**在检索层被四种模式各跑过一次，
因此它们不再是干净的测试集。

本模块生成：
* 120 份全新 PDF（6 个与主集**完全不同**的公司主体 × 10 部门 × V1/V2）
* 一套**措辞不同于开发集**的题目（题号前缀 `HQB-`）
* `FREEZE.json`：记录哈希、生成时间、`used_for_tuning=false`，用于事后自证

运行（只跑一次，跑完即冻结）：
    python -m benchmarks.datasets.hybrid_rag.build_holdout
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

from benchmarks import common
from benchmarks.datasets.hybrid_rag.build_corpus import DocumentSpec, build_documents, render_pdf
from benchmarks.datasets.hybrid_rag.build_questions import build_questions

HOLDOUT_DIR = common.DATASETS_DIR / "hybrid_rag" / "holdout"
HOLDOUT_DOCS = HOLDOUT_DIR / "documents"
HOLDOUT_QUESTIONS = HOLDOUT_DIR / "questions.jsonl"
FREEZE_PATH = HOLDOUT_DIR / "FREEZE.json"

#: 与开发集**不同措辞**的提问模板：避免"题面一字不差"带来的乐观偏差。
HOLDOUT_FACT_QUESTIONS: dict[str, tuple[str, list[str]]] = {
    "approval": ("超过多少万元的单一合同需要部门负责人与财务负责人共同签批？", []),
    "payment": ("验收款之外的质保金占合同金额的比例是多少？", []),
    "late": ("交付逾期时，违约金的累计封顶比例是多少？", []),
    "review": ("材料齐全后，初审环节的办理时限是几个工作日？", []),
    "supplier": ("成为合格供应商所需的注册资本下限是多少？", []),
    "sample": ("来料检验环节规定的最低抽样比例是多少？", []),
    "milestone": ("项目一共拆成几个里程碑节点？", ["五个", "5 个", "5个"]),
    "delay": ("进度落后多少个工作日以上就必须补交纠偏计划？", []),
    "budget": ("该主体年度预算的封顶金额是多少？", []),
    "travel": ("一线城市出差住宿的每晚限额是多少元？", []),
    "confidential": ("合同结束之后保密义务还要持续多少年？", []),
    "code": ("当年度新立项应采用的编号形式是什么？", []),
}


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_holdout() -> dict[str, Any]:
    specs: list[DocumentSpec] = build_documents(batch="holdout")
    HOLDOUT_DOCS.mkdir(parents=True, exist_ok=True)
    expected = {spec.filename for spec in specs}
    for stale in HOLDOUT_DOCS.glob("*.pdf"):
        if stale.name not in expected:
            stale.unlink(missing_ok=True)
    for spec in specs:
        render_pdf(spec, HOLDOUT_DOCS)

    rows = build_questions(specs, fact_questions=HOLDOUT_FACT_QUESTIONS, qid_prefix="HQB-")
    common.write_jsonl(HOLDOUT_QUESTIONS, rows)

    meta = {
        "documents": [s.to_meta() for s in specs],
        "count": len(specs),
        "questions": len(rows),
        "note": "独立留出集：公司主体/数值/文档号均与主数据集不重叠，题面措辞亦不同",
    }
    common.write_json(HOLDOUT_DIR / "corpus_meta.json", meta)

    freeze = {
        "frozen_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "used_for_tuning": False,
        "purpose": "最终一次性验证（四模式对比 + 嵌入模型验证）；冻结后不得用于调参",
        "documents": len(specs),
        "questions": len(rows),
        "questions_sha256": sha256_file(HOLDOUT_QUESTIONS),
        "corpus_meta_sha256": sha256_file(HOLDOUT_DIR / "corpus_meta.json"),
        "pdf_files": len(list(HOLDOUT_DOCS.glob("*.pdf"))),
        "pdf_bytes": sum(p.stat().st_size for p in HOLDOUT_DOCS.glob("*.pdf")),
    }
    common.write_json(FREEZE_PATH, freeze)
    return freeze


if __name__ == "__main__":  # pragma: no cover
    info = build_holdout()
    print(json.dumps(info, ensure_ascii=False, indent=2))
