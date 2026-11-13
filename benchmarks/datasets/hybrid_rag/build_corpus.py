"""大规模质量评测数据集：100 份企业文档 + 可核验问答。

设计原则（对应任务书第六阶段 A 部分）：

1. **不拿被测系统的输出当标准答案**。每道题的答案都来自文档生成器自己的
   参数表（`facts`），生成完再用独立校验脚本回读 PDF 文本确认"答案确实
   出现在标注的那一页上"。
2. **覆盖面按任务书要求的 12 类**组织，而不是随机出题。
3. **文档之间必须真的不同**：由「部门 × 公司主体 × 版本」三轴组合，
   并让每个主体有自己的阈值/时限/比例，避免"复制 100 份同一文档"。
4. **确定性**：不使用随机数；同一个参数表必然生成同一批文档与题目，
   任何人重跑都得到逐字节相同的结果（便于 diff 与复现）。

产物：

    benchmarks/datasets/hybrid_rag/documents/*.pdf
    benchmarks/datasets/hybrid_rag/questions.jsonl
    benchmarks/datasets/hybrid_rag/corpus_meta.json
    benchmarks/datasets/hybrid_rag/splits.json
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from benchmarks import common

DATASET_DIR = common.DATASETS_DIR / "hybrid_rag"
DOC_DIR = DATASET_DIR / "documents"

#: 10 个部门 × 2 份制度文档 = 20 个制度簇
DEPARTMENTS: tuple[tuple[str, str, str], ...] = (
    ("采购部", "CG", "采购"),
    ("质量部", "ZL", "质量"),
    ("财务部", "CW", "财务"),
    ("项目管理部", "XM", "项目"),
    ("人力资源部", "RL", "人力"),
    ("信息安全部", "AQ", "信息安全"),
    ("法务部", "FW", "法务"),
    ("供应链管理部", "GY", "供应链"),
    ("研发中心", "YF", "研发"),
    ("审计部", "SJ", "审计"),
)

#: 5 个公司主体；每个主体有一组独立的数值参数，保证文档内容真的不同。
COMPANIES: tuple[dict[str, Any], ...] = (
    {
        "name": "启明科技",
        "short": "启明",
        "approval_wan": 120,
        "pay": (30, 50, 20),
        "late_rate": 0.5,
        "late_cap": 20,
        "review_days": 10,
        "capital_wan": 500,
        "sample_ratio": 5,
        "budget_wan": 3800,
        "over_pct": 8,
        "hotel_yuan": 600,
        "conf_years": 3,
    },
    {
        "name": "恒远制造",
        "short": "恒远",
        "approval_wan": 80,
        "pay": (40, 50, 10),
        "late_rate": 0.8,
        "late_cap": 15,
        "review_days": 7,
        "capital_wan": 800,
        "sample_ratio": 8,
        "budget_wan": 5200,
        "over_pct": 5,
        "hotel_yuan": 450,
        "conf_years": 5,
    },
    {
        "name": "海川物流",
        "short": "海川",
        "approval_wan": 200,
        "pay": (20, 60, 20),
        "late_rate": 1.0,
        "late_cap": 25,
        "review_days": 15,
        "capital_wan": 300,
        "sample_ratio": 3,
        "budget_wan": 2600,
        "over_pct": 10,
        "hotel_yuan": 550,
        "conf_years": 2,
    },
    {
        "name": "瑞和医药",
        "short": "瑞和",
        "approval_wan": 150,
        "pay": (30, 40, 30),
        "late_rate": 0.6,
        "late_cap": 18,
        "review_days": 12,
        "capital_wan": 1000,
        "sample_ratio": 10,
        "budget_wan": 6400,
        "over_pct": 6,
        "hotel_yuan": 700,
        "conf_years": 4,
    },
    {
        "name": "云图数据",
        "short": "云图",
        "approval_wan": 60,
        "pay": (50, 40, 10),
        "late_rate": 0.4,
        "late_cap": 12,
        "review_days": 5,
        "capital_wan": 200,
        "sample_ratio": 6,
        "budget_wan": 1900,
        "over_pct": 12,
        "hotel_yuan": 500,
        "conf_years": 3,
    },
)

#: 独立留出集（holdout）的主体：与主数据集的公司、数值、文档号**完全不重叠**，
#: 用于"从未参与任何调参"的最终一次性验证。
#: 刻意让每个主体现有两个新阈值（如 late_cap 与主集不同的量级）。
HOLDOUT_COMPANIES: tuple[dict[str, Any], ...] = (
    {
        "name": "峻岭装备",
        "short": "峻岭",
        "approval_wan": 95,
        "pay": (35, 55, 10),
        "late_rate": 0.7,
        "late_cap": 17,
        "review_days": 9,
        "capital_wan": 650,
        "sample_ratio": 7,
        "budget_wan": 4300,
        "over_pct": 7,
        "hotel_yuan": 580,
        "conf_years": 6,
    },
    {
        "name": "瀚宇能源",
        "short": "瀚宇",
        "approval_wan": 175,
        "pay": (25, 55, 20),
        "late_rate": 0.9,
        "late_cap": 22,
        "review_days": 13,
        "capital_wan": 900,
        "sample_ratio": 4,
        "budget_wan": 5700,
        "over_pct": 9,
        "hotel_yuan": 480,
        "conf_years": 4,
    },
    {
        "name": "青柚医疗",
        "short": "青柚",
        "approval_wan": 130,
        "pay": (30, 45, 25),
        "late_rate": 0.55,
        "late_cap": 19,
        "review_days": 11,
        "capital_wan": 1100,
        "sample_ratio": 9,
        "budget_wan": 6100,
        "over_pct": 6,
        "hotel_yuan": 690,
        "conf_years": 5,
    },
    {
        "name": "启澜环保",
        "short": "启澜",
        "approval_wan": 70,
        "pay": (45, 45, 10),
        "late_rate": 0.45,
        "late_cap": 14,
        "review_days": 6,
        "capital_wan": 350,
        "sample_ratio": 6,
        "budget_wan": 2400,
        "over_pct": 11,
        "hotel_yuan": 520,
        "conf_years": 3,
    },
    {
        "name": "星桥物流",
        "short": "星桥",
        "approval_wan": 210,
        "pay": (20, 65, 15),
        "late_rate": 1.1,
        "late_cap": 24,
        "review_days": 14,
        "capital_wan": 260,
        "sample_ratio": 3,
        "budget_wan": 2100,
        "over_pct": 13,
        "hotel_yuan": 540,
        "conf_years": 2,
    },
    {
        "name": "衡远检测",
        "short": "衡远",
        "approval_wan": 110,
        "pay": (30, 50, 20),
        "late_rate": 0.65,
        "late_cap": 16,
        "review_days": 8,
        "capital_wan": 480,
        "sample_ratio": 8,
        "budget_wan": 3300,
        "over_pct": 8,
        "hotel_yuan": 610,
        "conf_years": 4,
    },
)


@dataclass
class DocumentSpec:
    """一份生成出来的文档及其"可核验事实表"。"""

    doc_id: str
    filename: str
    title: str
    pages: list[str]
    facts: dict[str, Any] = field(default_factory=dict)
    #: 事实键 -> 页码，用于自动生成 gold 页
    fact_pages: dict[str, int] = field(default_factory=dict)
    dept_code: str = ""
    company: str = ""
    version: str = "v1"

    def to_meta(self) -> dict[str, Any]:
        return {
            "doc_id": self.doc_id,
            "filename": self.filename,
            "title": self.title,
            "dept_code": self.dept_code,
            "company": self.company,
            "version": self.version,
            "pages": len(self.pages),
            "chars": sum(len(p) for p in self.pages),
            "facts": self.facts,
            "fact_pages": self.fact_pages,
        }


# ---------------------------------------------------------------------------
# 条款模板：每条都产出一个**可核对的数值事实**
# ---------------------------------------------------------------------------


def _clause_approval(c: dict[str, Any], dept: str) -> tuple[str, str, str]:
    value = f"{c['approval_wan']}万元"
    text = (
        f"第{{art}}条 审批权限。单一合同金额超过 {c['approval_wan']} 万元的，"
        f"须由{dept}负责人与财务负责人联合审批；超过 {c['approval_wan'] * 3} 万元的，"
        f"须提交总经理办公会审议。"
    )
    return "approval", value, text


def _clause_payment(c: dict[str, Any], dept: str) -> tuple[str, str, str]:
    a, b, d = c["pay"]
    text = (
        f"第{{art}}条 付款方式。原则上采用 {a}:{b}:{d} 分期支付，"
        f"即预付款 {a}%、验收款 {b}%、质保金 {d}%。"
        f"预付款须在合同生效后 15 个工作日内支付。"
    )
    return "payment", f"质保金{d}%", text


def _clause_late(c: dict[str, Any], dept: str) -> tuple[str, str, str]:
    text = (
        f"第{{art}}条 逾期责任。供应商逾期交付的，按合同总额每日 {c['late_rate']}% 计收违约金，"
        f"累计不超过合同总额的 {c['late_cap']}%；连续两次逾期超过 15 日的，"
        f"{dept}有权终止合作并列入观察名单。"
    )
    return "late", f"{c['late_cap']}%", text


def _clause_review(c: dict[str, Any], dept: str) -> tuple[str, str, str]:
    text = (
        f"第{{art}}条 办理时限。{dept}在收到完整材料后 {c['review_days']} 个工作日内完成初审，"
        f"初审通过后转交复核，复核时限为 3 个工作日。"
    )
    return "review", f"{c['review_days']}个工作日", text


def _clause_supplier(c: dict[str, Any], dept: str) -> tuple[str, str, str]:
    text = (
        f"第{{art}}条 供应商准入。申请成为合格供应商，须提交营业执照复印件、"
        f"ISO9001 质量管理体系证书、近三年无重大质量事故声明，"
        f"且注册资本不低于 {c['capital_wan']} 万元。"
    )
    return "supplier", f"{c['capital_wan']}万元", text


def _clause_sample(c: dict[str, Any], dept: str) -> tuple[str, str, str]:
    text = (
        f"第{{art}}条 来料检验。每批来料抽检比例不低于 {c['sample_ratio']}%，"
        f"抽检不合格批次整批退回，并由供应商承担复检费用。"
    )
    return "sample", f"{c['sample_ratio']}%", text


def _clause_milestone(c: dict[str, Any], dept: str) -> tuple[str, str, str]:
    text = (
        f"第{{art}}条 里程碑管理。项目划分为需求、设计、开发、测试、上线五个里程碑，"
        f"每个里程碑须由项目经理签字确认，并在评审通过后 2 个工作日内归档。"
    )
    return "milestone", "五个里程碑", text


def _clause_delay(c: dict[str, Any], dept: str) -> tuple[str, str, str]:
    text = (
        f"第{{art}}条 进度纠偏。若整体进度落后计划 {c['review_days'] + 3} 个工作日以上，"
        f"须向交付委员会提交书面纠偏计划，并同步更新风险登记册。"
    )
    return "delay", f"{c['review_days'] + 3}个工作日", text


def _clause_budget(c: dict[str, Any], dept: str) -> tuple[str, str, str]:
    text = (
        f"第{{art}}条 预算控制。年度预算上限为 {c['budget_wan']} 万元，"
        f"超支 {c['over_pct']}% 以上的，须报董事会审批后执行。"
    )
    return "budget", f"{c['budget_wan']}万元", text


def _clause_travel(c: dict[str, Any], dept: str) -> tuple[str, str, str]:
    text = (
        f"第{{art}}条 差旅标准。一线城市住宿标准为每晚不超过 {c['hotel_yuan']} 元，"
        f"二线城市不超过 {int(c['hotel_yuan'] * 0.7)} 元；出差结束后 15 个工作日内提交报销单。"
    )
    return "travel", f"{c['hotel_yuan']}元", text


def _clause_confidential(c: dict[str, Any], dept: str) -> tuple[str, str, str]:
    text = (
        f"第{{art}}条 保密义务。保密义务在合同终止后继续有效 {c['conf_years']} 年，"
        f"违反约定的须承担由此造成的全部直接损失。"
    )
    return "confidential", f"{c['conf_years']}年", text


def _clause_code(c: dict[str, Any], dept: str, seq: int) -> tuple[str, str, str]:
    code = f"{dept}-2024-{seq:03d}"
    near = f"{dept}-2024-{seq:03d}0"
    text = (
        f"第{{art}}条 编号规则。项目编号格式为 {code}，"
        f"与之相近的 {near} 属于历史归档编号，不适用于本年度新立项。"
    )
    return "code", code, text


CLAUSE_BUILDERS: tuple[Callable[..., tuple[str, str, str]], ...] = (
    _clause_approval,
    _clause_payment,
    _clause_late,
    _clause_review,
    _clause_supplier,
    _clause_sample,
    _clause_milestone,
    _clause_delay,
    _clause_budget,
    _clause_travel,
    _clause_confidential,
)

#: 部门 -> 该部门文档里必然出现的条款键（其余条款按序轮转补齐）
DEPT_CLAUSES: dict[str, tuple[str, ...]] = {
    "CG": ("approval", "payment", "late", "supplier", "code"),
    "ZL": ("supplier", "sample", "late", "review", "code"),
    "CW": ("budget", "payment", "travel", "confidential", "code"),
    "XM": ("milestone", "delay", "review", "late", "code"),
    "RL": ("travel", "review", "confidential", "budget", "code"),
    "AQ": ("confidential", "review", "supplier", "code", "budget"),
    "FW": ("confidential", "approval", "late", "code", "payment"),
    "GY": ("supplier", "sample", "payment", "late", "code"),
    "YF": ("milestone", "sample", "confidential", "code", "review"),
    "SJ": ("budget", "approval", "review", "confidential", "code"),
}

BUILDERS_BY_KEY = {
    "approval": _clause_approval,
    "payment": _clause_payment,
    "late": _clause_late,
    "review": _clause_review,
    "supplier": _clause_supplier,
    "sample": _clause_sample,
    "milestone": _clause_milestone,
    "delay": _clause_delay,
    "budget": _clause_budget,
    "travel": _clause_travel,
    "confidential": _clause_confidential,
}


def _interleave_distractors(pages: list[str], company: dict[str, Any]) -> list[str]:
    """插入与业务无关的干扰内容（食堂、班车、办公用品），用于干扰检索。"""
    noise = [
        f"第{{art}}条 后勤保障。{company['short']}园区食堂供应时间为 11:30 至 13:00，"
        f"班车每日 8:00 从地铁站发车。",
        f"第{{art}}条 办公用品。打印纸按月申领，单次申领不超过 5 包。",
    ]
    return pages + noise


def _version_adjust(company: dict[str, Any], version: str) -> dict[str, Any]:
    """V2 版对部分指标做了收紧/放宽，用于构造"新旧文档冲突"类问题。

    只调整**会被写进正文的数值**，不改名、不改结构，保证两版能一一对应，
    同时"以最新版为准"这条判断在数据层面是可核验的。
    """
    tuned = dict(company)
    if version == "v2":
        tuned["late_cap"] = company["late_cap"] + 5
        tuned["review_days"] = company["review_days"] + 2
        tuned["sample_ratio"] = company["sample_ratio"] + 1
        tuned["hotel_yuan"] = company["hotel_yuan"] + 50
    return tuned


def build_documents(batch: str = "main") -> list[DocumentSpec]:
    """按确定性规则生成文档。

    `batch="main"`  → 10 部门 × 5 主体 × 2 版本 = 100 份（主数据集）
    `batch="holdout"` → 独立留出集：**不同主体、不同数值、不同文档号**，
                        用于"从未参与过任何调参"的最终验证。
    """
    companies = HOLDOUT_COMPANIES if batch == "holdout" else COMPANIES
    doc_prefix = "HDOC2" if batch == "holdout" else "HDOC"
    file_suffix = "_HO" if batch == "holdout" else ""
    documents: list[DocumentSpec] = []
    seq = 0
    for dept_name, dept_code, _kw in DEPARTMENTS:
        for company in companies:
            for version in ("v1", "v2"):
                seq += 1
                doc_id = f"{doc_prefix}-{seq:03d}"
                keys = DEPT_CLAUSES[dept_code]
                tuned = _version_adjust(company, version)
                version_label = "V2" if version == "v2" else "V1"

                clauses: list[tuple[str, str, str]] = []
                article = 1
                for key in keys:
                    if key == "code":
                        kind, value, text = _clause_code(tuned, dept_code, seq)
                    else:
                        kind, value, text = BUILDERS_BY_KEY[key](tuned, dept_name)
                    clauses.append((kind, value, text.replace("{art}", str(article))))
                    article += 1

                title = f"{company['name']}{dept_name}管理制度（{version_label}）"
                header = (
                    f"{title}\n发布主体：{company['name']}\n"
                    f"文档编号：{doc_id}\n版本：{version_label}\n生效日期：2024-01-01\n"
                )
                if version == "v2":
                    header += "修订说明：本版本替代 V1 版，凡涉及违约金上限、审核时限、抽检比例与住宿标准的条款，一律以本版本为准。\n"

                page1 = [c[2] for c in clauses[:2]]
                page2 = [c[2] for c in clauses[2:4]]
                page3 = [c[2] for c in clauses[4:]]
                pages = [
                    header
                    + "\n\n第一章 总则\n本制度适用于"
                    + company["name"]
                    + "全体业务单元。\n\n"
                    + "\n\n".join(page1),
                    f"{title}（续）\n第二章 执行要求\n\n" + "\n\n".join(page2),
                    f"{title}（续）\n第三章 附则\n\n"
                    + "\n\n".join(page3)
                    + f"\n\n本制度自 2024-01-01 起施行，由{dept_name}负责解释。",
                ]
                pages = _interleave_distractors(pages, company)

                fact_pages: dict[str, int] = {}
                facts: dict[str, Any] = {}
                for index, page_text in enumerate(pages, start=1):
                    for kind, value, clause_text in clauses:
                        if clause_text in page_text:
                            fact_pages[kind] = index
                            facts[kind] = value

                filename = f"{doc_id}_{company['short']}_{dept_name}制度_{version_label}{file_suffix}.pdf"
                documents.append(
                    DocumentSpec(
                        doc_id=doc_id,
                        filename=filename,
                        title=title,
                        pages=pages,
                        facts=facts,
                        fact_pages=fact_pages,
                        dept_code=dept_code,
                        company=company["name"],
                        version=version,
                    )
                )
    return documents


# ---------------------------------------------------------------------------
# PDF 渲染（真实 PDF：走产品同一条 PyMuPDF 解析链路）
# ---------------------------------------------------------------------------


def render_pdf(spec: DocumentSpec, out_dir: Path) -> Path:
    """渲染真实 PDF。

    与 `benchmarks/datasets/rag/build_documents.py` 保持同一套排布参数：
    固定 A4、内置 CJK 字体、页眉写明页码，版面溢出直接报错 ——
    因为**页码是评测的一部分**，内容溢出导致的分页漂移会让 gold 页全部失效。
    """
    import pymupdf

    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / spec.filename
    doc = pymupdf.open()
    doc.set_metadata(
        {
            "title": spec.title,
            "author": f"{spec.company}（虚构）",
            "subject": f"Hybrid RAG Benchmark {spec.doc_id}",
            "creationDate": "D:20240101000000Z",
            "modDate": "D:20240101000000Z",
            "producer": "aieb-benchmark",
            "creator": "aieb-benchmark",
        }
    )
    total = len(spec.pages)
    for index, body in enumerate(spec.pages, start=1):
        page = doc.new_page(width=595, height=842)
        page.insert_text(
            (56, 50),
            f"{spec.title} — 第 {index} 页 / 共 {total} 页",
            fontname="china-s",
            fontsize=9,
        )
        overflow = page.insert_textbox(
            pymupdf.Rect(56, 70, 539, 790),
            body.strip("\n"),
            fontname="china-s",
            fontsize=10.5,
            lineheight=1.55,
        )
        if overflow < 0:
            raise RuntimeError(
                f"{spec.doc_id} 第 {index} 页溢出 {-overflow:.1f}pt，请精简正文以保证页码稳定"
            )
    doc.save(str(path), deflate=True, garbage=3)
    doc.close()
    return path


def build_all(*, write_pdf: bool = True) -> list[DocumentSpec]:
    specs = build_documents()
    if write_pdf:
        DOC_DIR.mkdir(parents=True, exist_ok=True)
        # 清理过期产物：早期生成规则的文件名不同，留着会让"100 份文档"名不副实
        expected = {spec.filename for spec in specs}
        for stale in DOC_DIR.glob("*.pdf"):
            if stale.name not in expected:
                stale.unlink(missing_ok=True)
        for spec in specs:
            render_pdf(spec, DOC_DIR)
    common.write_json(
        DATASET_DIR / "corpus_meta.json",
        {"documents": [s.to_meta() for s in specs], "count": len(specs)},
    )
    return specs


if __name__ == "__main__":  # pragma: no cover
    built = build_all()
    total_chars = sum(sum(len(p) for p in s.pages) for s in built)
    print(f"生成 {len(built)} 份文档，共 {total_chars} 字符，输出目录 {DOC_DIR}")
