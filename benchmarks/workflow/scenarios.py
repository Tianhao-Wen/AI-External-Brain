"""工作流评测场景：40 个确定性场景（含故障注入与审批分支）。

全部使用**虚构**的会议与邮件内容，不含任何真实企业数据。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Scenario:
    scenario_id: str
    name: str
    text: str
    #: 期望的业务结果
    expected_risks: int
    expected_todos: int
    #: 审批分支：approve / edit / reject / none（不审批=流程停在等待）
    decision: str = "approve"
    #: 故障注入（作用于 fake 世界）
    analysis_failures: int = 0
    draft_failures: int = 0
    send_raises: int = 0
    send_fails: int = 0
    send_uncertain: int = 0
    #: 是否在"审批前"模拟服务重启（验证恢复能力）
    restart_before_approval: bool = False
    #: 是否重复提交审批（模拟重复点击）
    duplicate_confirm: bool = False
    #: 期望的终态
    expect_status: str = "completed"
    #: 期望成功投递次数（发送类故障场景为 0）
    expected_sends: int | None = None
    tags: list[str] = field(default_factory=list)


_BASE = (
    "项目周会纪要。参会人：张伟、李娜。张伟说：上线日期已经延期两周，"
    "原因是接口联调一直不通过，这个风险还没有解决。"
    "李娜负责在3月15日前补齐测试用例。会议决定：本周五前完成联调。"
)

_MULTI = (
    "供应商评审会。参会人：王强、赵敏、陈磊。王强说：A 供应商的资质证书已经过期三个月，"
    "目前仍在供货，存在合规风险。赵敏说：B 供应商的报价比上季度上涨了 18%，成本压力很大。"
    "陈磊说：上周的质量抽检发现批次 Q-2201 的外观不良率达 6%，已通知返工。"
    "会议决定：暂停 A 供应商新增订单，由赵敏在一周内完成重新评审。"
)

_NO_RISK = (
    "部门例会。参会人：刘海、周静。刘海汇报：上季度各项指标均达成，客户满意度 92 分。"
    "周静补充：团队已完成年度培训计划，无遗留问题。会议决定：下季度继续保持现有节奏。"
)

_NO_OWNER = (
    "临时协调会。会上提到：机房空调故障已经影响两台服务器运行，风险尚未排除。"
    "有人提出需要尽快联系厂商维修，但会议没有明确负责人，也没有约定完成时间。"
    "会议决定：先临时增加备用风扇。"
)


def build_scenarios() -> list[Scenario]:
    items: list[Scenario] = []

    # 1) 正常会议处理（多种文本 × 审批分支）
    for index, (text, risks, todos, tag) in enumerate(
        [
            (_BASE, 1, 1, "normal"),
            (_MULTI, 3, 1, "multi_risk"),
            (_NO_RISK, 0, 0, "no_risk"),
            (_NO_OWNER, 1, 1, "missing_owner"),
        ],
        start=1,
    ):
        items.append(
            Scenario(
                scenario_id=f"S{index:02d}",
                name=f"正常处理-{tag}",
                text=text,
                expected_risks=risks,
                expected_todos=todos,
                tags=["normal", tag],
            )
        )

    # 2) 审批通过 / 修改 / 拒绝
    for index, decision in enumerate(["approve", "edit", "reject"], start=5):
        items.append(
            Scenario(
                scenario_id=f"S{index:02d}",
                name=f"审批-{decision}",
                text=_BASE + f"（审批分支 {decision}）",
                expected_risks=1,
                expected_todos=1,
                decision=decision,
                expect_status="cancelled" if decision == "reject" else "completed",
                tags=["approval", decision],
            )
        )

    # 3) 不审批（停在等待人工）——验证"中断不会被误判为失败"
    items.append(
        Scenario(
            scenario_id="S08",
            name="停在等待审批",
            text=_MULTI + "（不审批）",
            expected_risks=3,
            expected_todos=1,
            decision="none",
            expect_status="waiting_approval",
            tags=["approval", "interrupt"],
        )
    )

    # 4) 重复确认
    items.append(
        Scenario(
            scenario_id="S09",
            name="重复确认三次",
            text=_BASE + "（重复确认）",
            expected_risks=1,
            expected_todos=1,
            duplicate_confirm=True,
            tags=["idempotency", "duplicate"],
        )
    )

    # 5) 服务重启后继续
    for index, tag in enumerate(["restart_before_approval"], start=10):
        items.append(
            Scenario(
                scenario_id=f"S{index:02d}",
                name="审批前重启服务",
                text=_BASE + "（重启）",
                expected_risks=1,
                expected_todos=1,
                restart_before_approval=True,
                tags=["recovery", tag],
            )
        )

    # 6) 节点失败（分析失败 1 次可由框架重试恢复；3 次则任务失败）
    items.append(
        Scenario(
            scenario_id="S11",
            name="分析节点瞬时失败（可重试恢复）",
            text=_BASE + "（瞬时失败）",
            expected_risks=1,
            expected_todos=1,
            analysis_failures=1,
            tags=["fault", "retry"],
        )
    )
    items.append(
        Scenario(
            scenario_id="S12",
            name="分析节点持续失败（不可恢复）",
            text=_BASE + "（持续失败）",
            expected_risks=1,
            expected_todos=1,
            analysis_failures=5,
            expect_status="failed",
            expected_sends=0,
            tags=["fault", "unrecoverable"],
        )
    )

    # 7) 发送侧故障
    items.append(
        Scenario(
            scenario_id="S13",
            name="SMTP 明确拒绝（允许人工重试）",
            text=_BASE + "（SMTP 拒绝）",
            expected_risks=1,
            expected_todos=1,
            send_fails=1,
            expect_status="failed",
            expected_sends=0,
            tags=["fault", "smtp"],
        )
    )
    items.append(
        Scenario(
            scenario_id="S14",
            name="SMTP 超时（结果不确定）",
            text=_BASE + "（SMTP 超时）",
            expected_risks=1,
            expected_todos=1,
            send_raises=1,
            expect_status="failed",
            expected_sends=0,
            tags=["fault", "uncertain"],
        )
    )

    # 8) 批量补齐到 40 个（改变文本措辞，覆盖"多待办/长文/缺截止时间"等）
    variants = [
        ("多待办", _MULTI + "另外，李娜负责在4月1日前完成供应商培训。", 3, 1),
        ("长文本", _BASE + "补充说明：" + "本次延期对季度目标的影响需要进一步评估。" * 40, 1, 1),
        ("缺截止时间", _NO_OWNER + "后续由采购部跟进。", 1, 1),
        ("单风险", _BASE.replace("李娜负责在3月15日前补齐测试用例。", ""), 1, 0),
        ("无待办", _BASE.replace("李娜负责在3月15日前补齐测试用例。", "会议未安排具体待办。"), 1, 0),
    ]
    cursor = 15
    while len(items) < 40:
        for name, text, risks, todos in variants:
            if len(items) >= 40:
                break
            items.append(
                Scenario(
                    scenario_id=f"S{cursor:02d}",
                    name=f"变体-{name}-{cursor}",
                    text=text + f"（变体 {cursor}）",
                    expected_risks=risks,
                    expected_todos=todos,
                    tags=["variant", name],
                )
            )
            cursor += 1
    return items


def summary(scenarios: list[Scenario]) -> dict[str, Any]:
    tags: dict[str, int] = {}
    for item in scenarios:
        for tag in item.tags:
            tags[tag] = tags.get(tag, 0) + 1
    return {
        "scenarios": len(scenarios),
        "by_tag": tags,
        "decisions": {
            decision: sum(1 for s in scenarios if s.decision == decision)
            for decision in ("approve", "edit", "reject", "none")
        },
    }
