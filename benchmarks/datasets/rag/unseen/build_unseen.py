"""未暴露 RAG 测试集：2 份全新文档（DOC-E / DOC-F）+ 18 道题。

与开发集的 4 份文档完全不同的业务域（设备维保制度 / 客户服务 SLA），
且从未用于任何检索参数或 Prompt 调参。运行::

    python -m benchmarks.datasets.rag.unseen.build_unseen

产物::

    benchmarks/datasets/rag/unseen/documents/DOC-E-设备维保管理制度.pdf
    benchmarks/datasets/rag/unseen/documents/DOC-F-客户服务响应时限规定.pdf
    benchmarks/datasets/rag/unseen/questions.jsonl
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pymupdf

OUT = Path(__file__).resolve().parent
DOC_DIR = OUT / "documents"

DOCS: dict[str, dict] = {
    "DOC-E": {
        "filename": "DOC-E-设备维保管理制度.pdf",
        "title": "恒远装备制造有限公司 设备维保管理制度 HY-EQP-2025",
        "pages": [
            """恒远装备制造有限公司
设备维保管理制度
文件编号：HY-EQP-2025    版本：V2.0

第一章 总则
1.1 本制度适用于公司全部生产设备、检测设备与公用动力设施。
1.2 设备维保分为日常点检、一级保养、二级保养与大修四类。
1.3 设备台账由设备部统一维护，新增设备须在验收后 3 个工作日内入账。
1.4 未入账设备不得投入使用，也不得申请备件。

第二章 点检与保养周期
第三章 故障报修与响应
第四章 备件管理
第五章 委外维修与外协单位管理""",
            """第二章 点检与保养周期

2.1 日常点检由操作工在每班开机前完成，点检表当日交班组长签字。
2.2 一级保养每 500 运行小时或每月一次，以先到者为准，由操作工执行、班组长确认。
2.3 二级保养每 2000 运行小时或每季度一次，由设备部维修班组执行，须停机进行。
2.4 大修按设备类别确定周期：数控加工设备每 5 年一次，注塑设备每 4 年一次，动力设备每 6 年一次。
2.5 连续两个周期未按计划完成保养的设备，系统自动将其状态置为"受限"，只能安排非关键工序。
2.6 保养记录须在完成后 24 小时内录入设备管理系统，逾期记为未完成。""",
            """第三章 故障报修与响应

3.1 设备故障分为三级：
    I 级（停机故障）：导致整线停产，响应时限 15 分钟到场；
    II 级（局部故障）：影响单台设备或单工序，响应时限 30 分钟到场；
    III 级（轻微故障）：不影响生产，响应时限 4 小时到场。
3.2 报修须通过设备管理系统提交，电话或口头报修不作为考核依据。
3.3 I 级故障超过 2 小时未修复的，须由设备部经理向生产副总报告。
3.4 同一设备 30 天内发生 3 次及以上同类故障的，判定为重复故障，须启动根因分析（RCA），并在 5 个工作日内提交报告。
3.5 因操作不当造成的故障，维修工时费按 100% 计入责任部门成本。""",
            """第四章 备件管理与第五章 委外维修

4.1 备件分为关键备件与通用备件。关键备件指停机后无替代、采购周期超过 30 天的备件。
4.2 关键备件的安全库存不得低于 1 套，低于安全库存时系统自动触发补货申请。
4.3 关键备件的补货申请须在 2 个工作日内审批完成。
4.4 备件领用须填写用途与设备编号，旧件须交回备件库统一处置。
4.5 通用备件库存周转天数不得超过 90 天，超期备件每季度盘点一次并列出处置建议。

5.1 委外维修单位须在合格供方名录内，名录每年评审一次。
5.2 单次委外维修金额超过 10 万元的，须签订书面合同并约定质保期不少于 6 个月。
5.3 委外维修完成后，由设备部与使用部门共同验收，验收不合格的不予结算。
5.4 委外单位连续两次验收不合格的，暂停其委外资格 12 个月。""",
        ],
    },
    "DOC-F": {
        "filename": "DOC-F-客户服务响应时限规定.pdf",
        "title": "恒远装备制造有限公司 客户服务响应时限规定 HY-SVC-2025",
        "pages": [
            """恒远装备制造有限公司
客户服务响应时限规定
文件编号：HY-SVC-2025    版本：V1.3

第一章 适用范围
1.1 本规定适用于售后技术支持、备件供应与现场服务三类客户请求。
1.2 客户等级分为 A、B、C 三级，由销售部每年评定一次。
1.3 等级评定依据：年度采购额、设备保有量、战略合作属性。""",
            """第二章 客户等级与响应时限

2.1 A 级客户：年度采购额 500 万元以上。
    电话响应 30 分钟内，远程接入 2 小时内，现场到场 24 小时内。
2.2 B 级客户：年度采购额 100 万元（含）至 500 万元。
    电话响应 2 小时内，远程接入 8 小时内，现场到场 48 小时内。
2.3 C 级客户：年度采购额 100 万元以下。
    电话响应 8 小时内，远程接入 24 小时内，现场到场 72 小时内。
2.4 响应时限自客户请求在服务系统登记之时起算，不以电话沟通时间为准。
2.5 现场到场时限不含客户原因导致的等待时间，但须在系统内注明等待起止时间。""",
            """第三章 升级与考核

3.1 超时未响应的请求，系统在超时后自动升级一级：A 级升至服务经理，B 级升至区域主管，C 级升至服务专员组长。
3.2 同一客户单月出现 3 次超时的，须由服务经理在 5 个工作日内回访并输出改进措施。
3.3 服务响应及时率按月度统计，计算公式为：按时响应请求数 ÷ 应响应请求总数。
3.4 A 级客户的服务响应及时率考核目标为 98%，B 级为 95%，C 级为 90%。
3.5 连续两个月未达到考核目标的区域，扣减该区域当月绩效的 10%。""",
            """第四章 备件供应与现场服务

4.1 常用备件的发货时限：A 级客户 24 小时内发货，B 级客户 48 小时内，C 级客户 72 小时内。
4.2 非库存备件的供货周期以采购确认为准，销售须在报价时向客户书面说明。
4.3 现场服务工程师出发前须完成远程诊断，未完成远程诊断不得直接派工，紧急情况除外。
4.4 现场服务完成后 3 个工作日内须提交服务报告，报告须包含故障现象、处理过程、更换备件与客户签字。
4.5 客户投诉在 5 个工作日内未闭环的，自动升级至质量管理部。
4.6 年度客户满意度调查覆盖 A 级客户 100%、B 级客户不低于 60%、C 级客户抽样不低于 20%。""",
        ],
    },
}

QUESTIONS: list[dict] = [
    # single_page（3）
    {"question_id": "RAGU-001", "question_type": "single_page", "difficulty": "easy",
     "question": "数控加工设备的大修周期是多久？", "answerable": True,
     "gold_relevant_pages": [{"document_id": "DOC-E", "pages": [2]}],
     "gold_answer": "数控加工设备每 5 年一次。",
     "answer_keys": [["5 年"]]},
    {"question_id": "RAGU-002", "question_type": "single_page", "difficulty": "easy",
     "question": "A 级客户的电话响应时限是多少？", "answerable": True,
     "gold_relevant_pages": [{"document_id": "DOC-F", "pages": [2]}],
     "gold_answer": "30 分钟内。",
     "answer_keys": [["30 分钟"]]},
    {"question_id": "RAGU-003", "question_type": "single_page", "difficulty": "easy",
     "question": "关键备件的安全库存不得低于多少？", "answerable": True,
     "gold_relevant_pages": [{"document_id": "DOC-E", "pages": [4]}],
     "gold_answer": "不得低于 1 套。",
     "answer_keys": [["1 套"]]},
    # numeric（3）
    {"question_id": "RAGU-004", "question_type": "numeric", "difficulty": "medium",
     "question": "通用备件的库存周转天数上限是多少？", "answerable": True,
     "gold_relevant_pages": [{"document_id": "DOC-E", "pages": [4]}],
     "gold_answer": "不得超过 90 天。",
     "answer_keys": [["90 天"]]},
    {"question_id": "RAGU-005", "question_type": "numeric", "difficulty": "medium",
     "question": "A 级客户的服务响应及时率考核目标是多少？", "answerable": True,
     "gold_relevant_pages": [{"document_id": "DOC-F", "pages": [3]}],
     "gold_answer": "98%。",
     "answer_keys": [["98%"]]},
    {"question_id": "RAGU-006", "question_type": "numeric", "difficulty": "medium",
     "question": "单次委外维修金额超过多少需要签订书面合同？", "answerable": True,
     "gold_relevant_pages": [{"document_id": "DOC-E", "pages": [4]}],
     "gold_answer": "超过 10 万元。",
     "answer_keys": [["10 万"]]},
    # multi_condition（3）
    {"question_id": "RAGU-007", "question_type": "multi_condition", "difficulty": "hard",
     "question": "一台注塑设备连续两个季度没做二级保养，会被怎么处理？还可以接什么活？",
     "answerable": True,
     "gold_relevant_pages": [{"document_id": "DOC-E", "pages": [2]}],
     "gold_answer": "系统自动将设备状态置为「受限」，只能安排非关键工序。",
     "answer_keys": [["受限"], ["非关键工序"]]},
    {"question_id": "RAGU-008", "question_type": "multi_condition", "difficulty": "hard",
     "question": "一台设备 30 天内第四次出现同一类故障，按制度要走什么流程、几个工作日内交什么？",
     "answerable": True,
     "gold_relevant_pages": [{"document_id": "DOC-E", "pages": [3]}],
     "gold_answer": "判定为重复故障，启动根因分析（RCA），5 个工作日内提交报告。",
     "answer_keys": [["重复故障"], ["根因分析", "RCA"], ["5 个工作日"]]},
    {"question_id": "RAGU-009", "question_type": "multi_condition", "difficulty": "hard",
     "question": "B 级客户提出远程接入，几小时内要响应？如果单月超时三次会有什么后果？",
     "answerable": True,
     "gold_relevant_pages": [{"document_id": "DOC-F", "pages": [2, 3]}],
     "gold_answer": "8 小时内远程接入；单月 3 次超时须由服务经理 5 个工作日内回访并输出改进措施。",
     "answer_keys": [["8 小时"], ["回访"], ["改进措施"]]},
    # multi_page（2）
    {"question_id": "RAGU-010", "question_type": "multi_page", "difficulty": "hard",
     "question": "一件关键备件从低于安全库存到补货审批完成，制度上有哪些时限要求？",
     "answerable": True,
     "gold_relevant_pages": [{"document_id": "DOC-E", "pages": [4]}],
     "gold_answer": "低于安全库存时自动触发补货申请，补货申请须在 2 个工作日内审批完成。",
     "answer_keys": [["自动触发"], ["2 个工作日"]]},
    {"question_id": "RAGU-011", "question_type": "multi_page", "difficulty": "hard",
     "question": "现场服务工程师派工前有什么硬性要求？服务完成后要交什么、多久内交？",
     "answerable": True,
     "gold_relevant_pages": [{"document_id": "DOC-F", "pages": [4]}],
     "gold_answer": "出发前须完成远程诊断；服务完成后 3 个工作日内提交服务报告，含故障现象、处理过程、更换备件与客户签字。",
     "answer_keys": [["远程诊断"], ["3 个工作日"], ["服务报告"]]},
    # cross_document（2）
    {"question_id": "RAGU-012", "question_type": "cross_document", "difficulty": "hard",
     "question": "A 级客户现场到场要求 24 小时内，如果工程师需要从备件库领关键备件，领用要满足什么条件？",
     "answerable": True,
     "gold_relevant_pages": [{"document_id": "DOC-F", "pages": [2]},
                             {"document_id": "DOC-E", "pages": [4]}],
     "gold_answer": "现场到场 24 小时内；备件领用须填写用途与设备编号，旧件须交回备件库。",
     "answer_keys": [["24 小时"], ["用途", "设备编号"]]},
    {"question_id": "RAGU-013", "question_type": "cross_document", "difficulty": "hard",
     "question": "委外维修单位验收不合格两次会怎样？客户投诉长时间未闭环又会升级到哪？",
     "answerable": True,
     "gold_relevant_pages": [{"document_id": "DOC-E", "pages": [4]},
                             {"document_id": "DOC-F", "pages": [4]}],
     "gold_answer": "委外单位暂停资格 12 个月；客户投诉 5 个工作日未闭环自动升级至质量管理部。",
     "answer_keys": [["12 个月"], ["质量管理部"]]},
    # paraphrase（2）
    {"question_id": "RAGU-014", "question_type": "paraphrase", "difficulty": "medium",
     "question": "新买的设备还没登记到台账里，能直接开起来干活吗？", "answerable": True,
     "gold_relevant_pages": [{"document_id": "DOC-E", "pages": [1]}],
     "gold_answer": "不能。未入账设备不得投入使用，也不得申请备件。",
     "answer_keys": [["不得投入使用"]]},
    {"question_id": "RAGU-015", "question_type": "paraphrase", "difficulty": "medium",
     "question": "答应客户的时间是从什么时候开始算的？", "answerable": True,
     "gold_relevant_pages": [{"document_id": "DOC-F", "pages": [2]}],
     "gold_answer": "自客户请求在服务系统登记之时起算，不以电话沟通时间为准。",
     "answer_keys": [["服务系统登记"]]},
    # long_distractor（1）
    {"question_id": "RAGU-016", "question_type": "long_distractor", "difficulty": "hard",
     "question": "设备维保分哪几类？二级保养多久做一次、由谁执行？", "answerable": True,
     "gold_relevant_pages": [{"document_id": "DOC-E", "pages": [1, 2]}],
     "gold_answer": "分日常点检、一级保养、二级保养与大修四类；二级保养每 2000 运行小时或每季度一次，由设备部维修班组执行。",
     "answer_keys": [["日常点检"], ["2000 运行小时", "每季度"], ["维修班组"]]},
    # page_citation_trap（1）
    {"question_id": "RAGU-017", "question_type": "page_citation_trap", "difficulty": "hard",
     "question": "二级保养和一级保养分别的周期是什么？请分别给出页码。", "answerable": True,
     "gold_relevant_pages": [{"document_id": "DOC-E", "pages": [2]}],
     "gold_answer": "一级保养每 500 运行小时或每月一次；二级保养每 2000 运行小时或每季度一次（均在第 2 页）。",
     "answer_keys": [["500 运行小时", "每月"], ["2000 运行小时", "每季度"]],
     "strict_citation_pages": [{"document_id": "DOC-E", "pages": [2]}]},
    # negative（1）
    {"question_id": "RAGU-018", "question_type": "negative", "difficulty": "medium",
     "question": "公司对供应商的账期政策是多久？", "answerable": False,
     "gold_relevant_pages": [],
     "gold_answer": "两份文档都没有规定供应商账期。",
     "answer_keys": [],
     "forbidden": ["60 天", "90 天"]},
    # 以下 5 题为后加的困难题，用来提高未暴露集对"引用质量改进"的区分度
    {"question_id": "RAGU-019", "question_type": "multi_condition", "difficulty": "hard",
     "question": "一次 8 万元的委外维修，和一次 12 万元的委外维修，制度要求上有什么区别？",
     "answerable": True,
     "gold_relevant_pages": [{"document_id": "DOC-E", "pages": [4]}],
     "gold_answer": "8 万元未超过 10 万元门槛，无需书面合同；12 万元超过 10 万元，须签订书面合同并约定质保期不少于 6 个月。",
     "answer_keys": [["10 万元"], ["6 个月"]]},
    {"question_id": "RAGU-020", "question_type": "cross_document", "difficulty": "hard",
     "question": "A 级客户现场服务要求 24 小时到场，工程师出发前必须先做什么？",
     "answerable": True,
     "gold_relevant_pages": [{"document_id": "DOC-F", "pages": [2, 4]}],
     "gold_answer": "现场到场 24 小时内；出发前须完成远程诊断，未完成不得直接派工。",
     "answer_keys": [["24 小时"], ["远程诊断"]]},
    {"question_id": "RAGU-021", "question_type": "multi_page", "difficulty": "hard",
     "question": "客户投诉超时未闭环会升级到哪？服务响应及时率是怎么算的？",
     "answerable": True,
     "gold_relevant_pages": [{"document_id": "DOC-F", "pages": [3, 4]}],
     "gold_answer": "5 个工作日未闭环自动升级至质量管理部；及时率 = 按时响应请求数 ÷ 应响应请求总数。",
     "answer_keys": [["质量管理部"], ["按时响应"]]},
    {"question_id": "RAGU-022", "question_type": "negative", "difficulty": "medium",
     "question": "员工每年有几天年假？", "answerable": False,
     "gold_relevant_pages": [],
     "gold_answer": "两份文档都没有规定年假。",
     "answer_keys": [],
     "forbidden": ["5 天", "10 天", "15 天"]},
    {"question_id": "RAGU-023", "question_type": "page_citation_trap", "difficulty": "hard",
     "question": "一级保养和二级保养的周期分别是多少？两项规定都在文档的哪一页？",
     "answerable": True,
     "gold_relevant_pages": [{"document_id": "DOC-E", "pages": [2]}],
     "gold_answer": "一级保养每 500 运行小时或每月一次；二级保养每 2000 运行小时或每季度一次；均在《设备维保管理制度》第 2 页。",
     "answer_keys": [["500 运行小时", "每月"], ["2000 运行小时", "每季度"]],
     "strict_citation_pages": [{"document_id": "DOC-E", "pages": [2]}]},
]


def build() -> int:
    DOC_DIR.mkdir(parents=True, exist_ok=True)
    for doc_id, spec in DOCS.items():
        path = DOC_DIR / spec["filename"]
        doc = pymupdf.open()
        font = "china-s"
        for text in spec["pages"]:
            page = doc.new_page(width=595, height=842)
            rect = pymupdf.Rect(56, 56, 539, 786)
            page.insert_textbox(rect, text.strip(), fontname=font, fontsize=10.5, lineheight=1.5)
        doc.save(str(path))
        doc.close()
        print(f"  {doc_id}: {path.name} ({len(spec['pages'])} 页)")

    (OUT / "questions.jsonl").write_text(
        "\n".join(json.dumps(q, ensure_ascii=False) for q in QUESTIONS) + "\n", encoding="utf-8"
    )
    print(f"未暴露 RAG 题 {len(QUESTIONS)} 道 -> {OUT / 'questions.jsonl'}")
    return 0


if __name__ == "__main__":
    sys.exit(build())
