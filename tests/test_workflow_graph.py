"""StateGraph 主流程：启动 → 并行分支 → 落库 → 草稿 → 审批 → 发送。

覆盖验收标准 1-7：
1. 真的在用 StateGraph（节点/边由 `build_graph` 注册，状态由 Checkpoint 驱动）
2. 多个有意义的业务节点
3. 条件分支 + 并行分支状态合并
4. 持久化 Checkpointer
5. 中断不会被误判为失败
6. 审批是真正的 interrupt/resume
7. 确认/取消可用，且取消不发信
"""

from __future__ import annotations

import json

import pytest

pytest.importorskip("langgraph")

from aieb.workflow.graph import NODE_LABELS, route_after_approval, route_after_load
from aieb.workflow.state import ResumeCommand
from tests.workflow_harness import MEETING_TEXT, Harness


def start(h: Harness, **kwargs):
    return h.runner.start_sync(
        name="项目周会.txt", text=MEETING_TEXT, to_addr="team@example.com", **kwargs
    )


# ---------------------------------------------------------------------------
# 正常路径
# ---------------------------------------------------------------------------


def test_start_runs_graph_and_pauses_for_approval(harness: Harness) -> None:
    snap = start(harness)

    assert snap.execution_status == "waiting_approval"
    assert snap.resumable is True
    assert snap.approval is not None, "必须在邮件草稿生成后暂停"
    assert snap.approval.version == 1
    assert snap.approval.draft_hash
    assert snap.approval.subject.startswith("【会议提醒】")
    assert snap.email_draft_id.startswith("mail_")

    # 业务数据已经落库（审批前就持久化，重启后数据不会丢）
    meetings = harness.store.list_meetings()
    assert len(meetings) == 1
    assert meetings[0]["risk_count"] == 1
    assert meetings[0]["todo_count"] == 1
    assert harness.store.list_todos()[0]["content"] == "补齐测试用例"

    # 审批前绝没有发信
    assert harness.fakes.send_calls == 0
    assert harness.fakes.sent == []


def test_approve_sends_exactly_once_and_completes(harness: Harness) -> None:
    snap = start(harness)
    approval = snap.approval
    assert approval is not None

    done = harness.runner.resume_sync(
        snap.workflow_id,
        ResumeCommand(
            decision="approve", version=approval.version, draft_hash=approval.draft_hash
        ),
    )

    assert done.execution_status == "completed"
    assert done.resumable is False
    assert done.completed_at
    assert harness.fakes.send_calls == 1
    assert harness.fakes.sent[0]["to"] == "team@example.com"
    email = harness.store.get_email(done.email_draft_id)
    assert email["status"] == "sent"
    assert email["sent_at"]


def test_reject_cancels_without_sending(harness: Harness) -> None:
    snap = start(harness)
    approval = snap.approval
    assert approval is not None

    done = harness.runner.resume_sync(
        snap.workflow_id,
        ResumeCommand(
            decision="reject",
            version=approval.version,
            draft_hash=approval.draft_hash,
            note="内容不合适",
        ),
    )

    assert done.execution_status == "cancelled"
    assert done.approval_status == "rejected"
    assert harness.fakes.send_calls == 0, "取消后绝不能发信"
    assert harness.store.get_email(done.email_draft_id)["status"] == "cancelled"


def test_edit_bumps_version_and_requires_second_approval(harness: Harness) -> None:
    snap = start(harness)
    approval = snap.approval
    assert approval is not None

    edited = harness.runner.resume_sync(
        snap.workflow_id,
        ResumeCommand(
            decision="edit", version=approval.version, draft_hash=approval.draft_hash
        ),
        edit={"edit_body": "改过的正文：风险 1 条，待办 1 条。"},
    )

    # 修改后必须**再次**审批，而不是直接发送
    assert edited.execution_status == "waiting_approval"
    assert edited.approval is not None
    assert edited.approval.version == 2
    assert "改过的正文" in edited.approval.body
    assert harness.fakes.send_calls == 0

    done = harness.runner.resume_sync(
        snap.workflow_id,
        ResumeCommand(
            decision="approve",
            version=edited.approval.version,
            draft_hash=edited.approval.draft_hash,
        ),
    )
    assert done.execution_status == "completed"
    assert harness.fakes.send_calls == 1
    assert "改过的正文" in harness.fakes.sent[0]["body"], "发出去的必须是用户改后的版本"


def test_stale_approval_cannot_send_newer_draft(harness: Harness) -> None:
    snap = start(harness)
    approval = snap.approval
    assert approval is not None

    edited = harness.runner.resume_sync(
        snap.workflow_id,
        ResumeCommand(decision="edit", version=1, draft_hash=approval.draft_hash),
        edit={"edit_subject": "改后的主题"},
    )
    assert edited.approval is not None and edited.approval.version == 2

    # 拿着 v1 的审批去确认（例如两个浏览器标签页）
    try:
        harness.runner.resume_sync(
            snap.workflow_id,
            ResumeCommand(decision="approve", version=1, draft_hash=approval.draft_hash),
        )
        raise AssertionError("旧版本审批必须被拒绝")
    except Exception as exc:  # noqa: BLE001 - WorkflowError
        assert "过期" in str(exc)
    assert harness.fakes.send_calls == 0


def test_repeat_approve_does_not_send_twice(harness: Harness) -> None:
    snap = start(harness)
    approval = snap.approval
    assert approval is not None
    command = ResumeCommand(
        decision="approve", version=approval.version, draft_hash=approval.draft_hash
    )

    first = harness.runner.resume_sync(snap.workflow_id, command)
    second = harness.runner.resume_sync(snap.workflow_id, command)
    third = harness.runner.resume_sync(snap.workflow_id, command)

    assert first.execution_status == second.execution_status == third.execution_status == "completed"
    assert harness.fakes.send_calls == 1, "重复确认不能重复发信"


def test_cancel_then_resume_is_noop(harness: Harness) -> None:
    snap = start(harness)
    cancelled = harness.runner.cancel_sync(snap.workflow_id, note="用户取消")
    assert cancelled.execution_status == "cancelled"

    again = harness.runner.resume_sync(
        snap.workflow_id,
        ResumeCommand(
            decision="approve",
            version=cancelled.approval.version if cancelled.approval else 1,
            draft_hash=cancelled.approval.draft_hash if cancelled.approval else "",
        ),
    )
    assert again.execution_status == "cancelled"
    assert harness.fakes.send_calls == 0


# ---------------------------------------------------------------------------
# 图结构与路由
# ---------------------------------------------------------------------------


def test_graph_registers_expected_nodes_and_routes(harness: Harness) -> None:
    graph = harness.runner.graph
    node_names = set(graph.get_graph().nodes)
    expected = {
        "load_meeting",
        "analyze_meeting",
        "validate_risks",
        "extract_todos",
        "merge_results",
        "persist_business_state",
        "generate_email_draft",
        "human_approval",
        "update_draft",
        "send_email",
        "cancelled",
        "validation_error",
        "record_audit",
    }
    assert expected <= node_names, f"缺少节点：{expected - node_names}"
    assert set(NODE_LABELS) == expected, "节点中文标签必须覆盖全部节点"


def test_conditional_routes() -> None:
    assert route_after_load({"meeting_id": "mtg_1"}) == "generate_email_draft"
    assert route_after_load({}) == "analyze_meeting"
    assert route_after_approval({"approval_status": "approved"}) == "send_email"
    assert route_after_approval({"approval_status": "edited"}) == "update_draft"
    assert route_after_approval({"approval_status": "rejected"}) == "cancelled"
    assert route_after_approval({"approval_status": "invalid"}) == "validation_error"
    assert route_after_approval({}) == "validation_error"


def test_parallel_branches_write_disjoint_fields(harness: Harness) -> None:
    snap = start(harness)
    # 风险与待办分别由两个并行分支产出，合并后都在，且没有互相覆盖
    assert [r["title"] for r in snap.risks] == ["上线延期两周"]
    assert [t["content"] for t in snap.todos] == ["补齐测试用例"]
    assert snap.todos[0]["owner"] == "李娜"
    assert snap.todos[0]["due_date"] == "3月15日"


def test_trace_records_every_node(harness: Harness) -> None:
    snap = start(harness)
    events = harness.wf.list_events(snap.workflow_id)
    nodes = [e["node"] for e in events if e["event"] == "node"]
    for name in ("load_meeting", "analyze_meeting", "validate_risks", "extract_todos",
                 "merge_results", "persist_business_state", "generate_email_draft"):
        assert name in nodes, f"Trace 缺少节点 {name}：{nodes}"
    analyze = next(e for e in events if e["node"] == "analyze_meeting")
    assert analyze["input_tokens"] == 120
    assert analyze["duration_ms"] >= 0
    assert analyze["model"] == "fake-model"


def test_status_survives_without_memory_state(harness: Harness) -> None:
    """`status()` 只依赖 Checkpoint + 运行表，不依赖进程内对象。"""
    snap = start(harness)
    history = harness.runner.history(snap.workflow_id)
    assert history["run"]["status"] == "waiting_approval"
    assert history["snapshot"]["resumable"] is True
    assert json.loads(json.dumps(history["snapshot"], default=str))["workflow_id"] == snap.workflow_id
