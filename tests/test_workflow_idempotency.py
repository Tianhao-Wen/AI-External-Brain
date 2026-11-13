"""幂等性与副作用控制：重复点击、重复 RPC、SMTP 超时、不确定状态。

这里区分三种"失败"，处理方式完全不同（这是本模块最核心的业务判断）：

| 情况 | 判定 | 处理 |
| --- | --- | --- |
| SMTP 明确拒绝（550 / 鉴权失败） | `failed` | 允许人工重试 |
| SMTP 抛异常/超时（不知道对方收没收到） | `uncertain` | **绝不自动重试**，标记待人工核实 |
| 用户重复点击 / 重复 RPC | 已终态 | 直接返回快照，不重复执行 |
"""

from __future__ import annotations

import pytest

pytest.importorskip("langgraph")

from aieb.workflow.persistence import WorkflowStore, effect_key
from aieb.workflow.runner import WorkflowError
from aieb.workflow.state import ResumeCommand
from tests.workflow_harness import MEETING_TEXT, Harness


def start(h: Harness):
    return h.runner.start_sync(name="项目周会.txt", text=MEETING_TEXT, to_addr="team@example.com")


def approve(h: Harness, workflow_id: str):
    pending = h.runner.pending_approval_sync(workflow_id)
    assert pending is not None
    return h.runner.resume_sync(
        workflow_id,
        ResumeCommand(
            decision="approve", version=pending.version, draft_hash=pending.draft_hash
        ),
    )


# ---------------------------------------------------------------------------
# 幂等键状态机（纯存储层）
# ---------------------------------------------------------------------------


def test_effect_state_machine(tmp_path) -> None:
    wf = WorkflowStore(tmp_path / "wf.sqlite3")
    try:
        key = effect_key("wf_1", "send_email", "mail_1", 1)

        allowed, record = wf.begin_effect(key, workflow_id="wf_1", action="send_email")
        assert allowed and record["status"] == "executing" and record["attempts"] == 1

        allowed, record = wf.begin_effect(key, workflow_id="wf_1", action="send_email")
        assert not allowed and record["status"] == "executing", "执行中的任务不能被第二个请求抢走"

        wf.finish_effect(key, "succeeded", result={"ok": True})
        allowed, record = wf.begin_effect(key, workflow_id="wf_1", action="send_email")
        assert not allowed and record["status"] == "succeeded", "已完成不得重复执行"

        key2 = effect_key("wf_1", "send_email", "mail_2", 1)
        wf.begin_effect(key2, workflow_id="wf_1", action="send_email")
        wf.finish_effect(key2, "failed", error="550")
        allowed, _ = wf.begin_effect(key2, workflow_id="wf_1", action="send_email")
        assert allowed, "确定失败允许重试"

        key3 = effect_key("wf_1", "send_email", "mail_3", 1)
        wf.begin_effect(key3, workflow_id="wf_1", action="send_email")
        wf.finish_effect(key3, "uncertain", error="timeout")
        allowed, record = wf.begin_effect(key3, workflow_id="wf_1", action="send_email")
        assert not allowed and record["status"] == "uncertain", "不确定状态禁止自动重试"
    finally:
        wf.close()


# ---------------------------------------------------------------------------
# SMTP 超时 → uncertain，绝不自动重发
# ---------------------------------------------------------------------------


def test_smtp_exception_marks_uncertain_and_never_auto_retries(harness: Harness) -> None:
    snapshot = start(harness)
    harness.fakes.send_raises = 1

    result = approve(harness, snapshot.workflow_id)

    assert result.execution_status == "failed"
    assert harness.fakes.send_calls == 1
    email = harness.store.get_email(snapshot.email_draft_id)
    assert email["status"] == "uncertain"
    effects = {e["action"]: e for e in harness.wf.list_effects(snapshot.workflow_id)}
    assert effects["send_email"]["status"] == "uncertain"
    assert any(
        e.get("type") == "UncertainDelivery" for e in harness.wf.list_events(snapshot.workflow_id)[-3:]
        or []
    ) or result.errors, "必须把不确定状态显式暴露出来"

    # 续跑 / 再次确认都不能再发
    harness.runner.continue_run_sync(snapshot.workflow_id)
    with pytest.raises(WorkflowError):
        harness.runner.retry_send_sync(snapshot.workflow_id)
    assert harness.fakes.send_calls == 1, "uncertain 之后绝不能再自动发送"


def test_sender_reports_uncertain_is_treated_as_uncertain(harness: Harness) -> None:
    snapshot = start(harness)
    harness.fakes.send_uncertain = 1

    result = approve(harness, snapshot.workflow_id)

    assert result.execution_status == "failed"
    assert harness.store.get_email(snapshot.email_draft_id)["status"] == "uncertain"


# ---------------------------------------------------------------------------
# 明确失败 → 允许人工重试
# ---------------------------------------------------------------------------


def test_definite_failure_can_be_retried_manually(harness: Harness) -> None:
    snapshot = start(harness)
    harness.fakes.send_fails = 1

    failed = approve(harness, snapshot.workflow_id)
    assert failed.execution_status == "failed"
    assert harness.store.get_email(snapshot.email_draft_id)["status"] == "failed"
    assert harness.fakes.send_calls == 1

    ok = harness.runner.retry_send_sync(snapshot.workflow_id)
    assert ok.execution_status == "completed"
    assert harness.fakes.send_calls == 2
    assert harness.store.get_email(snapshot.email_draft_id)["status"] == "sent"
    assert len(harness.fakes.sent) == 1


def test_retry_after_success_is_noop(harness: Harness) -> None:
    snapshot = start(harness)
    approve(harness, snapshot.workflow_id)
    again = harness.runner.retry_send_sync(snapshot.workflow_id)
    assert again.execution_status == "completed"
    assert harness.fakes.send_calls == 1


# ---------------------------------------------------------------------------
# 重复点击 / 重复 RPC
# ---------------------------------------------------------------------------


def test_double_click_on_send_button(harness: Harness) -> None:
    snapshot = start(harness)
    pending = harness.runner.pending_approval_sync(snapshot.workflow_id)
    assert pending is not None
    command = ResumeCommand(
        decision="approve", version=pending.version, draft_hash=pending.draft_hash
    )

    first = harness.runner.resume_sync(snapshot.workflow_id, command)
    second = harness.runner.resume_sync(snapshot.workflow_id, command)

    assert first.execution_status == second.execution_status == "completed"
    assert harness.fakes.send_calls == 1


def test_duplicate_start_with_same_text_returns_same_workflow(harness: Harness) -> None:
    first = start(harness)
    second = start(harness)
    assert first.workflow_id == second.workflow_id, "同一份纪要重复提交不应开两条工作流"
    assert len(harness.store.list_meetings()) == 1
    assert harness.fakes.analysis_calls == 1, "也不应重复调用模型"


# ---------------------------------------------------------------------------
# 审批与版本绑定
# ---------------------------------------------------------------------------


def test_empty_recipient_fails_fast_without_touching_smtp(harness: Harness) -> None:
    """收件人为空时：确定失败（可人工重试），不能走到 SMTP、也不能算 uncertain。"""
    snapshot = harness.runner.start_sync(
        name="项目周会.txt", text=MEETING_TEXT, to_addr=""
    )
    pending = harness.runner.pending_approval_sync(snapshot.workflow_id)
    assert pending is not None

    out = harness.runner.resume_sync(
        snapshot.workflow_id,
        ResumeCommand(
            decision="approve", version=pending.version, draft_hash=pending.draft_hash
        ),
    )

    assert out.execution_status == "failed"
    assert harness.fakes.send_calls == 0, "空收件人绝不应该真的调用 SMTP"
    email = harness.store.get_email(snapshot.email_draft_id) or {}
    assert email.get("status") == "failed"
    assert "收件人" in str(email.get("error") or "")
    errors = list(out.errors or [])
    assert any(e.get("type") == "InvalidRecipient" for e in errors)


def test_approve_wrong_hash_is_rejected(harness: Harness) -> None:
    snapshot = start(harness)
    pending = harness.runner.pending_approval_sync(snapshot.workflow_id)
    assert pending is not None

    with pytest.raises(WorkflowError) as excinfo:
        harness.runner.resume_sync(
            snapshot.workflow_id,
            ResumeCommand(decision="approve", version=pending.version, draft_hash="deadbeef"),
        )
    assert "过期" in str(excinfo.value)
    assert harness.fakes.send_calls == 0


def test_draft_edited_by_other_path_invalidates_old_approval(harness: Harness) -> None:
    """绕过工作流直接改草稿（例如旧接口）→ 旧审批必须失效。"""
    snapshot = start(harness)
    pending = harness.runner.pending_approval_sync(snapshot.workflow_id)
    assert pending is not None

    harness.store.update_email(
        snapshot.email_draft_id, subject="被旁路改过的主题", version=99, draft_hash="hacked"
    )

    with pytest.raises(WorkflowError):
        harness.runner.resume_sync(
            snapshot.workflow_id,
            ResumeCommand(
                decision="approve", version=pending.version, draft_hash=pending.draft_hash
            ),
        )
    assert harness.fakes.send_calls == 0
