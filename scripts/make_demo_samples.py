"""生成「AI 外脑 Demo」演示用企业背景 PDF（虚构数据）。

用法::

    python scripts/make_demo_samples.py

输出：``samples/demo_company_background.pdf``（5 页）。

内容全部为**虚构演示数据**：公司、人物、金额、日期均与真实企业无关，
仅用于演示"知识库检索 + 页码引用 + 与会议记录交叉验证"的能力。
"""

from __future__ import annotations

from pathlib import Path

try:
    import pymupdf as fitz
except ImportError:  # pragma: no cover - 老版本 PyMuPDF
    import fitz

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "samples" / "demo_company_background.pdf"

DISCLAIMER = "【虚构演示数据】本文件中的公司、人物、金额、日期均为示例，与任何真实企业无关。"

PAGES: list[tuple[str, list[str]]] = [
    (
        "1. 公司概况（虚构）",
        [
            DISCLAIMER,
            "公司名称：蓝海智造股份有限公司（下称「公司」），成立于 2011 年 4 月，总部位于杭州。",
            "主营业务：工业传感器与控制器的研发、制造与运维服务。",
            "2025 年营业收入 8.6 亿元，员工 1200 人，其中研发人员 310 人。",
            "组织架构：公司下设三个事业部——传感器事业部、控制器事业部、运维服务事业部；"
            "另设供应链中心、质量中心、财务中心与研发中心四个职能部门。",
            "核心管理层：首席执行官（CEO）陈明；首席运营官（COO）赵磊；供应链总监孙倩；"
            "质量总监周涛；财务总监何敏；研发总监林舟。",
            "采购执行层：采购经理吴凡，负责 A 类供应商日常商务与订单执行。",
        ],
    ),
    (
        "2. 价格与合同审批规则",
        [
            DISCLAIMER,
            "2.1 供应商单价上调实行分级审批，审批层级按上调幅度确定：",
            "（1）上调幅度不超过 3%：由采购经理吴凡审批；",
            "（2）上调幅度大于 3% 且不超过 8%：由供应链总监孙倩审批；",
            "（3）上调幅度大于 8%：必须由 CEO 陈明与财务总监何敏联合审批，缺一不可。",
            "2.2 处于「暂停新增订单」状态的供应商，任何单价上调申请一律不予审批。"
            "整改验证通过后，方可重新受理其价格申请。",
            "2.3 单一来源供应商的更换，须由供应链中心提交替代方案评估报告，"
            "经 COO 赵磊批准后方可执行。",
            "2.4 任何口头承诺不构成价格变更依据，必须以书面审批单为准。",
        ],
    ),
    (
        "3. 供应商分级与质量条款",
        [
            DISCLAIMER,
            "3.1 供应商按年采购额与可替代性分为 A、B、C 三级。"
            "A 类供应商指年采购额超过 2000 万元或属于单一来源的供应商，当前共 6 家。",
            "3.2 A 类供应商执行月度评审，评审维度包括来料不良率、交付准时率、"
            "质量响应速度与整改闭环率。",
            "3.3 质量红线：A 类供应商连续两批来料检验不合格，即触发「暂停新增订单」，"
            "整改期最长 30 天；整改期届满仍未通过验证的，启动替代供应商程序。",
            "3.4 关键供应商说明：A-07 华瑞精密为传感器外壳的单一来源供应商，"
            "2025 年采购额 3200 万元，占传感器事业部物料成本的 18%。",
            "3.5 来料不良率公司级上限为 0.8%，超过上限即计入质量成本考核。",
        ],
    ),
    (
        "4. 2025 年度预算与成本控制",
        [
            DISCLAIMER,
            "4.1 2025 年度采购预算总额为 4.2 亿元，年度降本目标为 3.5%。",
            "4.2 质量成本（含返工、报废与客户索赔）年度上限为 1200 万元，"
            "由质量中心按月向财务中心报送。",
            "4.3 单一供应商年度采购额超过 3000 万元的，须在年度评审中提交"
            "第二来源可行性说明。",
            "4.4 预算内支出由部门负责人审批；超预算支出须由财务总监何敏审批。",
        ],
    ),
    (
        "5. 重点项目与考核指标",
        [
            DISCLAIMER,
            "5.1 「阿尔法」新一代工业传感器项目（下称阿尔法项目）是公司 2025—2026 年"
            "的一号工程，项目负责人为研发总监林舟。",
            "5.2 里程碑计划：M1 样机完成 2025 年 11 月 15 日；"
            "M2 小批量试产 2026 年 1 月 20 日；M3 量产交付 2026 年 3 月 30 日。",
            "5.3 项目物料中外壳由 A-07 华瑞精密独家供应，样机阶段需求量为 500 套。",
            "5.4 公司级考核指标：订单准时交付率不低于 95%；来料不良率不超过 0.8%；"
            "A 类供应商整改闭环率 100%。",
            "5.5 任何影响里程碑的变更，须由项目负责人提出书面变更申请，"
            "经 COO 赵磊审批后生效。",
        ],
    ),
]


_CJK_FONT_CANDIDATES = (
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/simhei.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/System/Library/Fonts/PingFang.ttc",
)


def _resolve_font() -> tuple[str, str | None]:
    """优先用真实中文 TrueType 字体；找不到时退回 PyMuPDF 内置的 china-s。

    内置 china-s 会给每个汉字额外加字距，排版很难看，所以只在没有系统字体时兜底。
    """
    for candidate in _CJK_FONT_CANDIDATES:
        if Path(candidate).exists():
            return "cjk", candidate
    return "china-s", None


def build() -> Path:
    doc = fitz.open()
    fontname, fontfile = _resolve_font()
    extra = {"fontfile": fontfile} if fontfile else {}
    for index, (title, lines) in enumerate(PAGES, start=1):
        page = doc.new_page()
        page.insert_text((60, 80), title, fontsize=17, fontname=fontname, **extra)
        y = 122.0
        for line in lines:
            # 让 PyMuPDF 自己算"这段实际占多高"：剩余高度越多说明用得越少
            rect = fitz.Rect(60, y, 535, 780)
            leftover = page.insert_textbox(rect, line, fontsize=10.5, fontname=fontname,
                                           lineheight=1.55, **extra)
            if leftover < 0:  # 这一段放不下（正常内容不会触发）
                raise RuntimeError(f"第 {index} 页内容溢出：{line[:20]}…")
            y += (rect.height - leftover) + 10
        page.insert_text((60, 800), f"第 {index} 页 / 共 {len(PAGES)} 页", fontsize=9,
                         fontname=fontname, **extra)
    doc.set_metadata({"title": "蓝海智造 公司背景资料（虚构演示数据）"})
    OUT.parent.mkdir(parents=True, exist_ok=True)
    # 真实中文字体（如 msyh.ttc）整包嵌入会让 PDF 膨胀到 20MB，必须做字体子集化
    try:
        doc.subset_fonts()
    except Exception as exc:  # noqa: BLE001 - 老版本 PyMuPDF 没有该能力
        print(f"提示：字体子集化失败（{exc}），文件可能偏大")
    doc.save(str(OUT), garbage=4, deflate=True)
    doc.close()
    return OUT


if __name__ == "__main__":
    path = build()
    try:
        import pymupdf
        with pymupdf.open(path) as d:
            pages = d.page_count
            chars = sum(len(p.get_text("text")) for p in d)
    except Exception:  # noqa: BLE001
        pages, chars = -1, -1
    print(f"已生成：{path}\n页数：{pages} / 提取字符数：{chars}")
