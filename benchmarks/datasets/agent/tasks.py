"""Agent 端到端任务集（25 个任务，覆盖 A–G 七类能力）。

任务全部用**自然语言**描述，不做任何提示词工程之外的引导；断言写在 ``checks`` 里，
由 ``benchmarks/runners/agent_runner.py`` 统一执行。断言只检查"结果对不对"，
不检查模型用了哪条路径（除非任务本身就是考"会不会用某个工具"）。

``checks`` 支持的类型
---------------------
``tool_called_any`` / ``tool_called_all`` / ``tool_not_called``  工具调用路径
``answer_contains`` / ``answer_not_contains``                    回答文本
``no_tool_error``                                               没有任何工具报错
``meeting_created``  ``min_risks`` / ``max_risks``              会议是否入库、风险条数
``todos_at_least``                                              待办条数
``email_draft_exists`` / ``email_not_sent`` / ``email_sent_count``  邮件流程
``pptx_valid``       ``min_slides`` / ``max_slides``            PPT 真实可打开
``max_llm_calls``                                               调用次数上限（考效率）

``repeat`` 为 True 的任务会在 ``--repeat`` 时重复执行，所有轮次都进结果，不允许只报最好的一次。
"""

from __future__ import annotations

TASKS: list[dict] = [
    # ------------------------------------------------------------------
    # A. 知识库问答（PDF 解析 + 检索 + 引用）
    # ------------------------------------------------------------------
    {
        "task_id": "A1",
        "type": "pdf_qa",
        "title": "差旅住宿标准",
        "turns": ["去上海出差，住宿费每晚最多能报多少？直接给结论，并说明出处。"],
        "max_iterations": 8,
        "timeout_s": 180,
        "repeat": True,
        "checks": [
            {"kind": "tool_called_any", "names": ["SearchKnowledge"]},
            {"kind": "answer_contains", "values": ["600"]},
            {"kind": "no_tool_error"},
        ],
    },
    {
        "task_id": "A2",
        "type": "pdf_qa",
        "title": "供应商评分权重",
        "turns": ["供应商综合评分里质量这一项占多少分？"],
        "max_iterations": 8,
        "timeout_s": 180,
        "checks": [
            {"kind": "tool_called_any", "names": ["SearchKnowledge"]},
            {"kind": "answer_contains", "values": ["40"]},
        ],
    },
    {
        "task_id": "A3",
        "type": "pdf_qa",
        "title": "项目总预算",
        "turns": ["星海项目的总预算是多少？请带上引用页码。"],
        "max_iterations": 8,
        "timeout_s": 180,
        "repeat": True,
        "checks": [
            {"kind": "tool_called_any", "names": ["SearchKnowledge"]},
            {"kind": "answer_contains", "values": ["2,480"]},
            {"kind": "no_tool_error"},
        ],
    },
    {
        "task_id": "A4",
        "type": "pdf_qa_negative",
        "title": "知识库里没有的信息",
        "turns": ["公司股票代码是多少？"],
        "max_iterations": 8,
        "timeout_s": 180,
        "checks": [
            {"kind": "tool_called_any", "names": ["SearchKnowledge"]},
            {"kind": "answer_contains_any", "values": ["没有", "未", "查不到", "无法"]},
            {"kind": "answer_not_contains", "values": ["600519", "300750"]},
        ],
    },
    # ------------------------------------------------------------------
    # B. 会议风险分析（文件 → 抽取 → 入库）
    # ------------------------------------------------------------------
    {
        "task_id": "B1",
        "type": "meeting_analysis",
        "title": "分析会议文件中的风险",
        "turns": ["把 会议记录/MEET-001.txt 分析后存到会议中心，告诉我这场会议有哪些风险。"],
        "max_iterations": 10,
        "timeout_s": 240,
        "repeat": True,
        "checks": [
            {"kind": "tool_called_any", "names": ["AnalyzeMeeting"]},
            {"kind": "meeting_created", "min_risks": 1},
            {"kind": "answer_contains", "values": ["联调"]},
        ],
    },
    {
        "task_id": "B2",
        "type": "meeting_analysis",
        "title": "无风险会议（不虚报）",
        "turns": ["把 会议记录/MEET-013.txt 分析后存入会议中心，并告诉我风险情况。"],
        "max_iterations": 10,
        "timeout_s": 240,
        "repeat": True,
        "checks": [
            {"kind": "tool_called_any", "names": ["AnalyzeMeeting"]},
            {"kind": "meeting_created", "min_risks": 0, "max_risks": 0},
        ],
    },
    {
        "task_id": "B3",
        "type": "meeting_analysis",
        "title": "会议待办清单",
        "turns": [
            "把 会议记录/MEET-029.txt 存入会议中心，然后把这场会议的待办事项列给我，"
            "负责人和截止时间按原文写，原文没有就写待确认。"
        ],
        "max_iterations": 12,
        "timeout_s": 300,
        "checks": [
            {"kind": "tool_called_any", "names": ["AnalyzeMeeting"]},
            {"kind": "meeting_created", "min_risks": 1},
            {"kind": "todos_at_least", "count": 3},
        ],
    },
    {
        "task_id": "B4",
        "type": "meeting_analysis",
        "title": "会议决策提取",
        "turns": ["把 会议记录/MEET-004.txt 分析后存入会议中心，这次到底发布不发布？依据是什么？"],
        "max_iterations": 10,
        "timeout_s": 240,
        "checks": [
            {"kind": "tool_called_any", "names": ["AnalyzeMeeting"]},
            {"kind": "meeting_created", "min_risks": 1},
            {"kind": "answer_contains", "values": ["灰度"]},
        ],
    },
    # ------------------------------------------------------------------
    # C. 跨会话长期记忆
    # ------------------------------------------------------------------
    {
        "task_id": "C1",
        "type": "memory_recall",
        "title": "回忆历史会议结论",
        "turns": ["我们上次开星海项目周例会时，定下来的上线时间是哪天？"],
        "max_iterations": 10,
        "timeout_s": 240,
        "repeat": True,
        "checks": [
            {"kind": "tool_called_any", "names": ["RecallMemory", "ListMeetings", "GetMeeting"]},
            {"kind": "answer_contains", "values": ["4月10日"]},
        ],
    },
    {
        "task_id": "C2",
        "type": "memory_recall",
        "title": "回忆责任人",
        "turns": ["支付网关沙箱环境这件事，当时是谁负责跟进？"],
        "max_iterations": 10,
        "timeout_s": 240,
        "checks": [
            {"kind": "tool_called_any", "names": ["RecallMemory", "GetMeeting", "ListMeetings"]},
            {"kind": "answer_contains", "values": ["陈晨"]},
        ],
    },
    {
        "task_id": "C3",
        "type": "memory_recall",
        "title": "回忆供应商议题",
        "turns": ["我们之前讨论过恒安电子吗？讨论结论是什么？"],
        "max_iterations": 10,
        "timeout_s": 240,
        "checks": [
            {"kind": "tool_called_any", "names": ["RecallMemory", "GetMeeting", "ListMeetings"]},
            {"kind": "answer_contains", "values": ["C 级"]},
        ],
    },
    {
        "task_id": "C4",
        "type": "memory_recall_negative",
        "title": "没有记录时如实说没有",
        "turns": ["我们上次关于南京地铁项目的会议讲了什么？"],
        "max_iterations": 10,
        "timeout_s": 240,
        "checks": [
            {"kind": "answer_contains_any", "values": ["没有", "未找到", "查不到", "无相关"]},
            {"kind": "answer_not_contains", "values": ["南京地铁项目定于", "南京地铁已"]},
        ],
    },
    # ------------------------------------------------------------------
    # D. 联网搜索（固定 fixture，考察会不会调用与用不用结果）
    # ------------------------------------------------------------------
    {
        "task_id": "D1",
        "type": "web_search_fixture",
        "title": "LPR 报价",
        "turns": ["帮我查一下最新一期的 LPR 报价是多少。"],
        "max_iterations": 8,
        "timeout_s": 180,
        "checks": [
            {"kind": "tool_called_any", "names": ["WebSearch"]},
            {"kind": "answer_contains", "values": ["3.00"]},
        ],
    },
    {
        "task_id": "D2",
        "type": "web_search_fixture",
        "title": "指数行情",
        "turns": ["搜一下今天的上证指数收盘点位。"],
        "max_iterations": 8,
        "timeout_s": 180,
        "checks": [
            {"kind": "tool_called_any", "names": ["WebSearch"]},
            {"kind": "answer_contains", "values": ["3286.45"]},
        ],
    },
    {
        "task_id": "D3",
        "type": "web_search_fixture",
        "title": "搜不到时不要编",
        "turns": ["查一下「天罡九号卫星发射时间」的准确消息。"],
        "max_iterations": 8,
        "timeout_s": 180,
        "checks": [
            {"kind": "tool_called_any", "names": ["WebSearch"]},
            {"kind": "answer_contains_any", "values": ["未找到", "没有", "查不到"]},
        ],
    },
    # ------------------------------------------------------------------
    # E. 邮件草稿 / 发送（Fake SMTP）
    # ------------------------------------------------------------------
    {
        "task_id": "E1",
        "type": "email_draft",
        "title": "生成会议提醒邮件草稿",
        "turns": [
            "把 会议记录/MEET-001.txt 分析一下，然后给张伟（zhangwei@example.com）"
            "写一封会议提醒邮件草稿，重点提醒风险。不要发送。"
        ],
        "max_iterations": 12,
        "timeout_s": 300,
        "repeat": True,
        "checks": [
            {"kind": "meeting_created", "min_risks": 1},
            {"kind": "email_draft_exists", "min": 1},
            {"kind": "email_not_sent"},
        ],
    },
    {
        "task_id": "E2",
        "type": "email_draft",
        "title": "明确要求不发时不得发送",
        "turns": [
            "分析 会议记录/MEET-002.txt，生成一封给李娜（lina@example.com）的例会纪要邮件草稿，"
            "强调千万不要自己发出去，等我确认。"
        ],
        "max_iterations": 12,
        "timeout_s": 300,
        "checks": [
            {"kind": "email_draft_exists", "min": 1},
            {"kind": "email_not_sent"},
        ],
    },
    {
        "task_id": "E3",
        "type": "email_send",
        "title": "草稿经人工确认后投递",
        "turns": [
            "分析 会议记录/MEET-006.txt，给周凯（zhoukai@example.com）生成一封风险提醒邮件草稿。"
        ],
        "max_iterations": 12,
        "timeout_s": 300,
        "confirm_send": True,
        "checks": [
            {"kind": "email_draft_exists", "min": 1},
            {"kind": "email_sent_count", "count": 1},
        ],
    },
    # ------------------------------------------------------------------
    # F. PPTX 生成
    # ------------------------------------------------------------------
    {
        "task_id": "F1",
        "type": "pptx",
        "title": "8 页风险汇报",
        "turns": ["生成一份「星海项目交付风险汇报」的 PPT，8 页，面向公司管理层。"],
        "max_iterations": 8,
        "timeout_s": 300,
        "repeat": True,
        "checks": [
            {"kind": "tool_called_any", "names": ["CreatePresentation"]},
            {"kind": "pptx_valid", "min_slides": 8, "max_slides": 8},
        ],
    },
    {
        "task_id": "F2",
        "type": "pptx",
        "title": "5 页项目复盘",
        "turns": ["做一份「云图平台季度复盘」的 PPT，5 页，重点讲性能优化。"],
        "max_iterations": 8,
        "timeout_s": 300,
        "checks": [
            {"kind": "tool_called_any", "names": ["CreatePresentation"]},
            {"kind": "pptx_valid", "min_slides": 5, "max_slides": 5},
        ],
    },
    {
        "task_id": "F3",
        "type": "pptx",
        "title": "10 页安全合规汇报",
        "turns": ["帮我把信息安全合规整改做成一份 10 页的汇报 PPT。"],
        "max_iterations": 8,
        "timeout_s": 300,
        "checks": [
            {"kind": "tool_called_any", "names": ["CreatePresentation"]},
            {"kind": "pptx_valid", "min_slides": 10, "max_slides": 10},
            {"kind": "max_llm_calls", "value": 6},
        ],
    },
    # ------------------------------------------------------------------
    # G. 异常处理与多轮上下文
    # ------------------------------------------------------------------
    {
        "task_id": "G1",
        "type": "error_handling",
        "title": "文件不存在时不编造",
        "turns": ["分析 会议记录/不存在的会议.txt 这场会议的风险。"],
        "max_iterations": 8,
        "timeout_s": 180,
        "checks": [
            {"kind": "answer_contains_any", "values": ["找不到", "不存在", "没有找到", "无法"]},
            {"kind": "meeting_created", "min_risks": 0, "max_risks": 0, "expect_none": True},
        ],
    },
    {
        "task_id": "G2",
        "type": "error_handling",
        "title": "不支持的文件格式",
        "turns": ["把 会议记录/数据导出.csv 里的会议记录解析出来并存进会议中心。"],
        "max_iterations": 8,
        "timeout_s": 180,
        "checks": [
            {"kind": "answer_contains_any", "values": ["不支持", "格式", "无法"]},
        ],
    },
    {
        "task_id": "G3",
        "type": "error_handling",
        "title": "无文档时的知识库提问",
        "turns": ["知识库里关于「火星基地建设预算」是怎么规定的？"],
        "max_iterations": 8,
        "timeout_s": 180,
        "checks": [
            {"kind": "answer_contains_any", "values": ["没有", "未", "不存在", "无法"]},
        ],
    },
    {
        "task_id": "G4",
        "type": "multi_turn",
        "title": "同一会话内两轮追问",
        "turns": [
            "把 会议记录/MEET-010.txt 分析后存入会议中心，这场会有什么风险？",
            "刚说的三条风险里，哪一条会直接卡住上线？只回答风险标题。",
        ],
        "max_iterations": 12,
        "timeout_s": 360,
        "checks": [
            {"kind": "meeting_created", "min_risks": 1},
            {"kind": "answer_contains", "values": ["查询", "报表"]},
        ],
    },
]


def by_id() -> dict[str, dict]:
    return {task["task_id"]: task for task in TASKS}


def select(only: list[str] | None = None, limit: int = 0, split: str = "all") -> list[dict]:
    """按切分/任务号挑选任务。``test_unseen`` 读取单独的未暴露任务集。"""
    if split == "test_unseen":
        from benchmarks.datasets.agent.unseen_tasks import TASKS as UNSEEN

        tasks = UNSEEN
    else:
        tasks = TASKS
        if split not in ("", "all"):
            import json
            from pathlib import Path

            path = Path(__file__).resolve().parent / "splits.json"
            if not path.is_file():
                raise FileNotFoundError("缺少 agent/splits.json，请先运行 python -m benchmarks.splits")
            ids = set(json.loads(path.read_text(encoding="utf-8"))["splits"].get(split) or [])
            tasks = [t for t in tasks if t["task_id"] in ids]
    if only:
        wanted = {item.strip() for item in only}
        tasks = [t for t in tasks if t["task_id"] in wanted or t["type"] in wanted]
    if limit:
        tasks = tasks[:limit]
    return tasks
