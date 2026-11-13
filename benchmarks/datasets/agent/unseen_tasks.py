"""未暴露 Agent 任务集（6 个），用于验证优化是否泛化到没见过的任务表述。

与开发集任务的区别：新的业务域（设备维保、电商大促、医药合规）、新的会议文件、
新的提问方式，且**从未用于任何 Prompt 调整**。
"""

from __future__ import annotations

TASKS: list[dict] = [
    {
        "task_id": "UA1",
        "type": "pdf_qa",
        "title": "设备大修周期（未暴露文档）",
        "turns": ["数控加工设备的大修周期是几年一次？请给出依据页码。"],
        "max_iterations": 8,
        "timeout_s": 180,
        "checks": [
            {"kind": "tool_called_any", "names": ["SearchKnowledge"]},
            {"kind": "answer_contains", "values": ["5 年"]},
            {"kind": "no_tool_error"},
        ],
    },
    {
        "task_id": "UA2",
        "type": "meeting_analysis",
        "title": "大促容量评审风险（未暴露会议）",
        "turns": ["把 会议记录/MEET-U03.txt 分析后存入会议中心，并告诉我最严重的性能问题是什么。"],
        "max_iterations": 10,
        "timeout_s": 240,
        "checks": [
            {"kind": "tool_called_any", "names": ["AnalyzeMeeting"]},
            {"kind": "meeting_created", "min_risks": 1},
            {"kind": "answer_contains", "values": ["响应", "QPS", "2.6"]},
        ],
    },
    {
        "task_id": "UA3",
        "type": "memory_recall",
        "title": "回忆未暴露会议结论",
        "turns": ["海川物流 TMS 接口对接那件事，最后是怎么定的？"],
        "max_iterations": 10,
        "timeout_s": 240,
        "checks": [
            {"kind": "tool_called_any", "names": ["RecallMemory", "ListMeetings", "GetMeeting"]},
            {"kind": "answer_contains_any", "values": ["邮件", "固化", "字段"]},
        ],
    },
    {
        "task_id": "UA4",
        "type": "pptx",
        "title": "6 页合规整改汇报",
        "turns": ["做一份 6 页的 GSP 合规整改进展汇报 PPT。"],
        "max_iterations": 8,
        "timeout_s": 300,
        "checks": [
            {"kind": "tool_called_any", "names": ["CreatePresentation"]},
            {"kind": "pptx_valid", "min_slides": 6, "max_slides": 6},
        ],
    },
    {
        "task_id": "UA5",
        "type": "email_draft",
        "title": "未暴露会议的提醒邮件",
        "turns": [
            "分析 会议记录/MEET-U02.txt，然后给沈悦（shenyue@example.com）写一封合规检查提醒邮件草稿。"
            "只生成草稿，不要发送。"
        ],
        "max_iterations": 12,
        "timeout_s": 300,
        "checks": [
            {"kind": "meeting_created", "min_risks": 1},
            {"kind": "email_draft_exists", "min": 1},
            {"kind": "email_not_sent"},
        ],
    },
    {
        "task_id": "UA6",
        "type": "error_handling",
        "title": "文件不存在时如实说明",
        "turns": ["分析 会议记录/MEET-U99.txt 里的风险。"],
        "max_iterations": 8,
        "timeout_s": 180,
        "checks": [
            {"kind": "answer_contains_any", "values": ["找不到", "不存在", "没有找到", "无法"]},
            {"kind": "meeting_created", "min_risks": 0, "max_risks": 0, "expect_none": True},
        ],
    },
]
