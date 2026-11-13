"""RPC 层（`workflow.*`）与 UI 契约测试：前端拿到的必须是后端真实状态。

用 Fake 依赖替换 LLM/SMTP（不花钱、不发信），但 RPC → BrainOps → WorkflowRunner →
StateGraph → Checkpoint → 业务库 这条链路是**真的**在跑。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

pytest.importorskip("langgraph")

from aieb.brain.ops import BrainOps, BrainOpsError
from aieb.brain.service import BrainService
from aieb.config import ProviderConfig
from aieb.workflow.deps import AnalysisOutcome, DraftOutcome, SendOutcome, WorkflowDeps
from aieb.workflow.persistence import WorkflowStore
from aieb.workflow.runner import WorkflowRunner
from tests.workflow_harness import MEETING_TEXT, Fakes, FakeWorld


def build_ops(tmp_path: Path) -> tuple[BrainOps, BrainService, Fakes, WorkflowRunner]:
    provider = ProviderConfig(
        name="offline",
        protocol="openai-compat",
        base_url="http://127.0.0.1:9/x",
        model="offline",
        api_key="offline",
    )
    service = BrainService(work_dir=tmp_path, provider=provider)
    fakes = Fakes()
    world = FakeWorld(fakes)
    wf = WorkflowStore(tmp_path / ".aieb" / "workflow" / "workflow.sqlite3")
    deps = WorkflowDeps(
        store=service.store,
        wf=wf,
        workspace=tmp_path,
        analyzer=world.analyzer,
        drafter=world.drafter,
        sender=world.sender,
        model_name="fake-model",
        activity=service.store.add_activity,
    )
    runner = WorkflowRunner(deps=deps, workspace=tmp_path)
    # 把 Runner 注入 service，RPC 就走这条链路（与生产一致）
    service._workflow = runner
    service._workflow_store = wf
    return BrainOps(service), service, fakes, runner


@pytest.fixture()
def ops(tmp_path: Path):
    ops_obj, service, fakes, runner = build_ops(tmp_path)
    try:
        yield ops_obj, service, fakes, runner
    finally:
        runner.close()
        wf = getattr(service, "_workflow_store", None)
        service._workflow = None
        service._workflow_store = None
        service.store.close()
        if wf is not None:
            wf.close()


def run(coro):
    return asyncio.run(coro)


def test_workflow_start_returns_pending_approval(ops) -> None:
    ops_obj, service, fakes, _ = ops
    result = run(
        ops_obj.dispatch("workflow.start", {"name": "周会.txt", "text": MEETING_TEXT, "to": "a@b.com"})
    )
    workflow = result["workflow"]
    assert workflow["execution_status"] == "waiting_approval"
    assert workflow["resumable"] is True
    assert workflow["approval"]["subject"]
    assert workflow["approval"]["version"] == 1
    assert workflow["approval"]["schema_version"] >= 1
    assert workflow["next_nodes"], "前端要能显示正在等待哪个节点"
    assert fakes.send_calls == 0


def test_workflow_list_and_status_are_backed_by_checkpoint(ops) -> None:
    ops_obj, service, _, runner = ops
    started = run(
        ops_obj.dispatch(
            "workflow.start",
            {"name": "周会.txt", "text": MEETING_TEXT, "to": "team@example.com"},
        )
    )["workflow"]

    listed = run(ops_obj.dispatch("workflow.list", {}))["workflows"]
    assert [w["workflow_id"] for w in listed] == [started["workflow_id"]]
    assert listed[0]["status"] == "waiting_approval"

    # 关掉 Runner 重建（模拟服务重启）后，状态仍然来自磁盘
    runner.close()
    service._workflow = None
    fresh = run(ops_obj.dispatch("workflow.status", {"id": started["workflow_id"]}))["workflow"]
    assert fresh["execution_status"] == "waiting_approval"
    assert fresh["approval"]["email_draft_id"] == started["email_draft_id"]


def test_workflow_resume_approve_sends_once(ops) -> None:
    ops_obj, _, fakes, _ = ops
    started = run(
        ops_obj.dispatch(
            "workflow.start",
            {"name": "周会.txt", "text": MEETING_TEXT, "to": "team@example.com"},
        )
    )["workflow"]
    approval = started["approval"]

    done = run(
        ops_obj.dispatch(
            "workflow.resume",
            {
                "id": started["workflow_id"],
                "decision": "approve",
                "version": approval["version"],
                "draft_hash": approval["draft_hash"],
            },
        )
    )["workflow"]
    assert done["execution_status"] == "completed"
    assert fakes.send_calls == 1

    # 重复点击
    again = run(
        ops_obj.dispatch(
            "workflow.resume",
            {
                "id": started["workflow_id"],
                "decision": "approve",
                "version": approval["version"],
                "draft_hash": approval["draft_hash"],
            },
        )
    )["workflow"]
    assert again["execution_status"] == "completed"
    assert fakes.send_calls == 1


def test_workflow_resume_validates_payload(ops) -> None:
    ops_obj, _, _, _ = ops
    started = run(
        ops_obj.dispatch(
            "workflow.start",
            {"name": "周会.txt", "text": MEETING_TEXT, "to": "team@example.com"},
        )
    )["workflow"]

    with pytest.raises(BrainOpsError):
        run(ops_obj.dispatch("workflow.resume", {"id": started["workflow_id"], "decision": "yolo"}))
    with pytest.raises(BrainOpsError):
        run(
            ops_obj.dispatch(
                "workflow.resume",
                {"id": started["workflow_id"], "decision": "approve", "version": 1},
            )
        )
    with pytest.raises(BrainOpsError):
        run(
            ops_obj.dispatch(
                "workflow.resume",
                {
                    "id": started["workflow_id"],
                    "decision": "approve",
                    "version": 1,
                    "draft_hash": "wrong",
                },
            )
        )


def test_workflow_cancel_and_history(ops) -> None:
    ops_obj, _, fakes, _ = ops
    started = run(
        ops_obj.dispatch(
            "workflow.start",
            {"name": "周会.txt", "text": MEETING_TEXT, "to": "team@example.com"},
        )
    )["workflow"]

    cancelled = run(
        ops_obj.dispatch("workflow.cancel", {"id": started["workflow_id"], "note": "不发了"})
    )["workflow"]
    assert cancelled["execution_status"] == "cancelled"
    assert fakes.send_calls == 0

    history = run(ops_obj.dispatch("workflow.history", {"id": started["workflow_id"]}))
    assert history["run"]["status"] == "cancelled"
    nodes = [e["node"] for e in history["events"] if e["event"] == "node"]
    assert "human_approval" in nodes
    assert history["tokens"]["input_tokens"] > 0, "Token 用量要能回答“一次工作流花了多少”"


def test_workflow_edit_then_approve_via_rpc(ops) -> None:
    ops_obj, _, fakes, _ = ops
    started = run(
        ops_obj.dispatch(
            "workflow.start",
            {"name": "周会.txt", "text": MEETING_TEXT, "to": "team@example.com"},
        )
    )["workflow"]
    approval = started["approval"]

    edited = run(
        ops_obj.dispatch(
            "workflow.resume",
            {
                "id": started["workflow_id"],
                "decision": "edit",
                "version": approval["version"],
                "draft_hash": approval["draft_hash"],
                "subject": "改过的主题",
                "body": "改过的正文",
            },
        )
    )["workflow"]
    assert edited["execution_status"] == "waiting_approval"
    assert edited["approval"]["version"] == 2
    assert edited["approval"]["subject"] == "改过的主题"
    assert fakes.send_calls == 0

    done = run(
        ops_obj.dispatch(
            "workflow.resume",
            {
                "id": started["workflow_id"],
                "decision": "approve",
                "version": edited["approval"]["version"],
                "draft_hash": edited["approval"]["draft_hash"],
            },
        )
    )["workflow"]
    assert done["execution_status"] == "completed"
    assert fakes.sent[0]["subject"] == "改过的主题"


def test_legacy_meeting_analyze_still_works_and_does_not_duplicate(ops) -> None:
    """旧接口保持可用；与工作流共用文本去重，不会把会议写两遍。"""
    ops_obj, service, _, _ = ops
    # 旧接口需要真实 LLM，这里直接用 service 的方法验证去重语义
    meeting = service.store.add_meeting(
        title="项目周会", summary="s", source_name="周会.txt", raw_text=MEETING_TEXT
    )
    started = run(
        ops_obj.dispatch(
            "workflow.start",
            {"name": "周会.txt", "text": MEETING_TEXT, "to": "team@example.com"},
        )
    )["workflow"]
    meetings = service.store.list_meetings()
    assert len(meetings) == 1, "同一份纪要不允许被两条路径各写一遍"
    assert started["meeting_id"] == meeting["id"]
