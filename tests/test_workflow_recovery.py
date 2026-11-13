"""断点恢复测试（A–H）。全部使用**真实 SQLite**（业务库 + 编排库 + Checkpoint）。

| 场景 | 做法 |
| --- | --- |
| A 会议分析完成后中断 | 注入“草稿节点第一次失败”，落库的那一步已完成 → 续跑不重复建业务数据 |
| B 草稿生成后中断 | 草稿已落库后直接丢弃进程内对象，重开 Runner 读回状态 |
| C 审批期间关闭浏览器 | 不保留任何前端内存，新建 Runner 后仍能拿到同一个审批请求 |
| D 审批期间重启后端 | 重新打开 Checkpoint 库后 resume，且只发一封 |
| E 重启后继续原工作流 | 同 D，额外断言最终业务结果正确 |
| F 重复恢复请求 | 并发两次 resume，只发一封 |
| G 取消后再恢复 | 取消后 resume 不得发信 |
| H 失败节点重新执行 | 分析节点持续失败 → 如实失败；续跑成功，且风险/待办不多写 |
| I 瞬时失败自动重试 | 分析节点只失败 1 次 → 同一次运行内重试后成功，重试记录进 Trace |
"""

from __future__ import annotations

import threading

import pytest

pytest.importorskip("langgraph")

from aieb.workflow.runner import WorkflowError
from aieb.workflow.state import ResumeCommand
from tests.workflow_harness import MEETING_TEXT, Harness


def start(h: Harness, *, allow_reuse: bool = True):
    return h.runner.start_sync(
        name="项目周会.txt",
        text=MEETING_TEXT,
        to_addr="team@example.com",
        allow_reuse=allow_reuse,
    )


def approve_current(h: Harness, workflow_id: str):
    pending = h.runner.pending_approval_sync(workflow_id)
    assert pending is not None
    return h.runner.resume_sync(
        workflow_id,
        ResumeCommand(
            decision="approve", version=pending.version, draft_hash=pending.draft_hash
        ),
    )


# ---------------------------------------------------------------------------
# A / H：执行中途失败 → 续跑
# ---------------------------------------------------------------------------


def test_A_continue_after_persist_does_not_duplicate_business_data(harness: Harness) -> None:
    """会议已落库、草稿生成时崩溃：续跑后不得再建一份会议/风险/待办。

    草稿节点自带 1 次可控重试，所以这里注入 2 次失败才能真正把工作流打断
    （注入 1 次会被重试吃掉，那种情况见下面的 test_A2）。
    """
    harness.fakes.draft_failures = 2

    with pytest.raises(WorkflowError):
        start(harness)

    assert len(harness.store.list_meetings()) == 1, "落库那一步必须先成功"
    run = harness.wf.list_runs()[0]
    assert run["status"] == "failed"

    snap = harness.runner.continue_run_sync(run["workflow_id"])

    assert snap.execution_status == "waiting_approval"
    assert len(harness.store.list_meetings()) == 1, "续跑不得重复创建会议"
    assert len(harness.store.list_risks()) == 1
    assert len(harness.store.list_todos()) == 1
    # 持久化副作用只执行过一次（幂等键为 succeeded，续跑直接复用）
    effects = {e["action"]: e["status"] for e in harness.wf.list_effects(run["workflow_id"])}
    assert effects["persist_meeting"] == "succeeded"


def test_A2_transient_draft_failure_is_retried_without_duplicate_meeting(harness: Harness) -> None:
    """草稿节点只失败 1 次 → 自动重试成功；会议/风险/待办仍然只有一份。"""
    harness.fakes.draft_failures = 1

    snap = start(harness)

    assert snap.execution_status == "waiting_approval"
    assert harness.fakes.draft_calls == 2, "应当恰好重试 1 次"
    assert len(harness.store.list_meetings()) == 1
    assert len(harness.store.list_risks()) == 1
    assert len(harness.store.list_todos()) == 1
    run = harness.wf.list_runs()[0]
    retries = [e for e in harness.wf.list_events(run["workflow_id"]) if e["event"] == "retry"]
    assert any(e["node"] == "generate_email_draft" for e in retries)


def test_H_failed_node_is_retried_and_business_writes_stay_single(harness: Harness) -> None:
    """分析节点**持续**失败 → 工作流失败并如实记录；恢复后重跑同一步不重复写业务数据。"""
    harness.fakes.analysis_failures = 5  # 超过节点内显式重试的 2 次
    with pytest.raises(WorkflowError):
        start(harness)
    run = harness.wf.list_runs()[0]
    assert run["status"] == "failed"
    events = harness.wf.list_events(run["workflow_id"])
    assert any(e["status"] == "error" for e in events), "失败必须留痕，不能装成功"
    assert len(harness.store.list_meetings()) == 0
    # 2 次尝试（1 次原调用 + 1 次重试），不能无限重试
    assert harness.fakes.analysis_calls == 2

    harness.fakes.analysis_failures = 0
    snap = harness.runner.continue_run_sync(run["workflow_id"])
    assert snap.execution_status == "waiting_approval"
    assert len(harness.store.list_meetings()) == 1
    assert harness.fakes.analysis_calls >= 3, "失败节点应该被重新执行过"


def test_I_transient_failure_recovers_within_one_run(harness: Harness) -> None:
    """瞬时失败（只失败 1 次）在**同一次运行内**自动重试恢复，不需要人工介入。"""
    harness.fakes.analysis_failures = 1
    snap = start(harness)

    assert snap.execution_status == "waiting_approval"
    assert harness.fakes.analysis_calls == 2, "应当恰好重试 1 次"
    run = harness.wf.list_runs()[0]
    retries = [e for e in harness.wf.list_events(run["workflow_id"]) if e["event"] == "retry"]
    assert retries, "重试必须写进 Trace，否则外部无法判断发生过重试"
    assert retries[0]["retry"] == 1
    assert retries[0]["error_type"] == "RuntimeError"
    # 只落一份业务数据
    assert len(harness.store.list_meetings()) == 1
    assert len(harness.store.list_risks()) == 1


# ---------------------------------------------------------------------------
# B 草稿生成后中断 → 重开 Runner
# ---------------------------------------------------------------------------


def test_B_draft_survives_runner_restart(harness: Harness) -> None:
    snap = start(harness)
    workflow_id = snap.workflow_id
    email_id = snap.email_draft_id
    assert email_id

    runner = harness.reopen_runner()  # 模拟进程重启（内存全丢，磁盘保留）

    resumed = runner.status_sync(workflow_id)
    assert resumed.execution_status == "waiting_approval"
    assert resumed.resumable is True
    assert resumed.approval is not None
    assert resumed.approval.email_draft_id == email_id
    assert resumed.email_draft_id == email_id


# ---------------------------------------------------------------------------
# C 审批期间关闭浏览器 → 状态仍然可查询
# ---------------------------------------------------------------------------


def test_C_pending_approval_visible_after_browser_closed(harness: Harness) -> None:
    snap = start(harness)
    assert snap.approval is not None

    runner = harness.reopen_runner()
    listing = runner.list()
    assert [r["workflow_id"] for r in listing] == [snap.workflow_id]
    assert listing[0]["status"] == "waiting_approval"
    assert runner.pending_approval_sync(snap.workflow_id) is not None


# ---------------------------------------------------------------------------
# D / E 审批期间重启后端 → 继续并只发一封
# ---------------------------------------------------------------------------


def test_D_resume_after_backend_restart_sends_once(harness: Harness) -> None:
    snap = start(harness)
    runner = harness.reopen_runner()

    pending = runner.pending_approval_sync(snap.workflow_id)
    assert pending is not None
    done = runner.resume_sync(
        snap.workflow_id,
        ResumeCommand(
            decision="approve", version=pending.version, draft_hash=pending.draft_hash
        ),
    )

    assert done.execution_status == "completed"
    assert harness.fakes.send_calls == 1
    assert harness.store.get_email(snap.email_draft_id)["status"] == "sent"


def test_E_business_result_is_correct_after_restart(harness: Harness) -> None:
    snap = start(harness)
    runner = harness.reopen_runner()
    done = approve_current_with(runner, snap.workflow_id)

    assert done.execution_status == "completed"
    meetings = harness.store.list_meetings()
    assert len(meetings) == 1
    assert meetings[0]["title"] == "项目周会"
    assert harness.store.list_risks()[0]["title"] == "上线延期两周"
    todos = harness.store.list_todos()
    assert len(todos) == 1 and todos[0]["owner"] == "李娜"


def approve_current_with(runner, workflow_id: str):
    pending = runner.pending_approval_sync(workflow_id)
    assert pending is not None
    return runner.resume_sync(
        workflow_id,
        ResumeCommand(
            decision="approve", version=pending.version, draft_hash=pending.draft_hash
        ),
    )


# ---------------------------------------------------------------------------
# F 重复恢复请求（并发）
# ---------------------------------------------------------------------------


def test_F_concurrent_resume_only_sends_once(harness: Harness) -> None:
    snap = start(harness)
    pending = harness.runner.pending_approval_sync(snap.workflow_id)
    assert pending is not None
    command = ResumeCommand(
        decision="approve", version=pending.version, draft_hash=pending.draft_hash
    )

    results: list[object] = []
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            results.append(harness.runner.resume_sync(snap.workflow_id, command))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"并发恢复不应报错：{errors[:2]}"
    assert harness.fakes.send_calls == 1, "三个并发确认只能发一封"
    assert all(getattr(r, "execution_status", "") == "completed" for r in results)


# ---------------------------------------------------------------------------
# G 取消后再恢复
# ---------------------------------------------------------------------------


def test_G_resume_after_cancel_is_noop(harness: Harness) -> None:
    snap = start(harness)
    pending = harness.runner.pending_approval_sync(snap.workflow_id)
    assert pending is not None

    cancelled = harness.runner.cancel_sync(snap.workflow_id, note="不发了")
    assert cancelled.execution_status == "cancelled"
    assert harness.fakes.send_calls == 0

    again = harness.runner.resume_sync(
        snap.workflow_id,
        ResumeCommand(
            decision="approve", version=pending.version, draft_hash=pending.draft_hash
        ),
    )
    assert again.execution_status == "cancelled"
    assert harness.fakes.send_calls == 0, "取消之后任何恢复请求都不能发信"


# ---------------------------------------------------------------------------
# 额外：thread 隔离
# ---------------------------------------------------------------------------


def test_two_workflows_do_not_share_state(harness: Harness) -> None:
    first = start(harness)
    second = harness.runner.start_sync(
        name="另一个会.txt",
        text=MEETING_TEXT + "（第二份，内容不同，用于区分）",
        to_addr="other@example.com",
        allow_reuse=False,
    )
    assert first.workflow_id != second.workflow_id
    assert first.email_draft_id != second.email_draft_id

    done = approve_current(harness, first.workflow_id)
    assert done.execution_status == "completed"

    other = harness.runner.status_sync(second.workflow_id)
    assert other.execution_status == "waiting_approval", "另一个工作流不应被连带推进"
    assert other.approval is not None and other.approval.to_addr == "other@example.com"
    assert harness.fakes.send_calls == 1
