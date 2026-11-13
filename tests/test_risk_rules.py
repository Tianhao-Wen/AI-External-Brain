"""``aieb.brain.risk_rules`` 的单元测试：固化"什么该丢、什么不该丢"的边界。

这些测试不调用任何模型，跑得很快，可以在 CI 里当作红线：
任何一次改动如果让"已解决问题的过滤"失效，或者误删了真实风险，这里会先红。
"""

from __future__ import annotations

import pytest

from aieb.brain import risk_rules


RAW = """会议主题：测试会议
一、进展
磁盘告警已通过清理日志恢复，当前使用率均在 55% 以下。
二、讨论
张伟提醒回访中不要承诺具体交付日期。
如果接受现有条款，潜在的一次性损失上限约 300 万元。
三、风险
订单服务与支付网关的接口联调比落后两周，支付网关方尚未提供沙箱环境。
以上问题叠加，按期交付的可能性已明显降低。
"""


def _risk(title: str, evidence: str, **kwargs) -> dict:
    base = {"title": title, "level": "medium", "description": title, "evidence": evidence}
    base.update(kwargs)
    return base


@pytest.mark.parametrize(
    "risk,expected_fragment",
    [
        # 已解决的问题：依据里带"已恢复"
        (_risk("磁盘使用率告警", "磁盘告警已通过清理日志恢复"), "已解决"),
        # 规范提醒：只有要求，没有问题事实
        (_risk("承诺交付日期", "张伟提醒回访中不要承诺具体交付日期"), "规范/提醒"),
        # 假设 + 测算
        (_risk("条款损失", "如果接受现有条款，潜在的一次性损失上限约 300 万元"), "假设"),
        # 总结句
        (_risk("交付可能性下降", "以上问题叠加，按期交付的可能性已明显降低"), "总结"),
    ],
)
def test_drop_invalid_risks(risk, expected_fragment):
    kept, dropped = risk_rules.filter_risks([risk], RAW)
    assert kept == []
    assert len(dropped) == 1
    assert expected_fragment in dropped[0]["dropped_reason"]


def test_keep_genuine_risk():
    """真正的未解决风险必须保留——这是不能妥协的底线。"""
    risk = _risk(
        "接口联调落后",
        "订单服务与支付网关的接口联调比落后两周，支付网关方尚未提供沙箱环境",
        level="high",
    )
    kept, dropped = risk_rules.filter_risks([risk], RAW)
    assert dropped == []
    assert len(kept) == 1


def test_resolved_marker_with_unresolved_marker_is_kept():
    """同时出现"已恢复"和"尚未"时说明只缓解了一部分，不能当已解决丢掉。"""
    risk = _risk("告警未彻底消除", "磁盘告警已恢复部分节点，剩余 3 个节点尚未恢复")
    kept, dropped = risk_rules.filter_risks([risk], RAW)
    assert len(kept) == 1
    assert dropped == []


def test_empty_evidence_is_dropped():
    kept, dropped = risk_rules.filter_risks([_risk("无依据的风险", "")], RAW)
    assert kept == []
    assert "依据" in dropped[0]["dropped_reason"]


def test_status_resolved_is_dropped():
    risk = _risk("已修复的缺陷", "订单服务与支付网关的接口联调比落后两周", status="resolved")
    kept, dropped = risk_rules.filter_risks([risk], RAW)
    assert kept == []
    assert "已解决" in dropped[0]["dropped_reason"]


def test_next_sentence_resolution_is_detected():
    """依据本身只是告警记录，但紧接着的一句说明了已恢复。"""
    text = "今日共发生 2 起告警，均为磁盘使用率超过 80% 的阈值提醒，已通过清理日志恢复。"
    risk = _risk("磁盘告警", "今日共发生 2 起告警，均为磁盘使用率超过 80% 的阈值提醒")
    kept, dropped = risk_rules.filter_risks([risk], text)
    assert kept == []
    assert "后一句" in dropped[0]["dropped_reason"]


def test_dedupe_merges_same_topic():
    risks = [
        _risk("预算超支", "按当前进度推算，本季度将超支约 45 万元", level="medium"),
        _risk("预算超支风险", "按当前进度推算，本季度将超支约 45 万元", level="high"),
    ]
    kept, _dropped = risk_rules.filter_risks(risks, RAW)
    assert len(kept) == 1
    # 合并时保留更严重的等级
    assert kept[0]["level"] == "high"


def test_dedupe_does_not_merge_unrelated():
    risks = [
        _risk("接口联调落后", "订单服务与支付网关的接口联调比落后两周，支付网关方尚未提供沙箱环境"),
        _risk("执行人未确定", "暂停压测环境的执行人尚未确定"),
    ]
    kept, _dropped = risk_rules.filter_risks(risks, RAW)
    assert len(kept) == 2
