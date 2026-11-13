"""Typed State 与 Reducer 的单元测试（包含"并行写入冲突"的规则验证）。"""

from __future__ import annotations

import json
from typing import Annotated, get_args, get_origin, get_type_hints

import pytest
from pydantic import ValidationError

from aieb.workflow.state import (
    ApprovalRequest,
    ResumeCommand,
    WorkflowSnapshot,
    WorkflowState,
    extend_unique,
    merge_by_key,
)


# ---------------------------------------------------------------------------
# Reducer
# ---------------------------------------------------------------------------


def test_merge_by_key_overwrites_same_key_and_keeps_order() -> None:
    left = [{"id": "a", "v": 1}, {"id": "b", "v": 2}]
    right = [{"id": "b", "v": 20}, {"id": "c", "v": 3}]
    merged = merge_by_key(left, right)
    assert [item["id"] for item in merged] == ["a", "b", "c"]
    assert merged[1]["v"] == 20, "同键应覆盖而不是重复追加"


def test_merge_by_key_falls_back_to_title_or_content() -> None:
    left = [{"title": "上线延期", "level": "medium"}]
    right = [{"title": "上线延期", "level": "high"}]
    merged = merge_by_key(left, right)
    assert len(merged) == 1 and merged[0]["level"] == "high"


def test_merge_by_key_is_order_independent_for_disjoint_writes() -> None:
    a = [{"id": "1"}]
    b = [{"id": "2"}]
    forward = merge_by_key(a, b)
    backward = merge_by_key(b, a)
    assert {item["id"] for item in forward} == {item["id"] for item in backward} == {"1", "2"}


def test_extend_unique_dedupes_parallel_appends() -> None:
    assert extend_unique(["x"], ["x", "y"]) == ["x", "y"]
    assert extend_unique(None, ["a"]) == ["a"]


# ---------------------------------------------------------------------------
# State 声明：并行写冲突必须由 reducer 解决
# ---------------------------------------------------------------------------


def _reducer_names() -> dict[str, str]:
    hints = get_type_hints(WorkflowState, include_extras=True)
    out: dict[str, str] = {}
    for key, hint in hints.items():
        if get_origin(hint) is Annotated:
            args = get_args(hint)
            out[key] = getattr(args[1], "__name__", str(args[1]))
    return out


def test_list_fields_used_by_parallel_nodes_have_reducers() -> None:
    reducers = _reducer_names()
    # 这两个字段分别由并行分支写入，必须带 reducer，否则并行时会被覆盖/冲突
    assert "validated_risks" in reducers
    assert "action_items" in reducers
    assert "errors" in reducers and reducers["errors"] == "add"
    assert "node_trace" in reducers
    # 标量字段不应带 reducer（明确"后写覆盖"的语义）
    assert "current_step" not in reducers
    assert "approval_status" not in reducers


def test_state_declares_identity_and_execution_fields() -> None:
    hints = get_type_hints(WorkflowState, include_extras=True)
    for field in (
        "workflow_id",
        "thread_id",
        "session_id",
        "meeting_id",
        "document_id",
        "meeting_summary",
        "risk_candidates",
        "validated_risks",
        "action_items",
        "decisions",
        "email_draft_id",
        "approval_status",
        "execution_status",
        "current_step",
        "errors",
        "retry_count",
        "started_at",
        "completed_at",
    ):
        assert field in hints, f"缺少状态字段 {field}"


# ---------------------------------------------------------------------------
# 契约校验
# ---------------------------------------------------------------------------


def test_approval_request_round_trip_and_schema_version() -> None:
    request = ApprovalRequest(
        workflow_id="wf_1",
        email_draft_id="mail_1",
        version=2,
        draft_hash="abc123",
        to_addr="a@example.com",
        subject="主题",
        body="正文",
    )
    payload = request.model_dump()
    assert payload["schema_version"] >= 1
    assert ApprovalRequest.model_validate(payload) == request
    # 未知字段不应导致解析失败（前后端/新旧版本兼容）
    extra = {**payload, "future_field": "x"}
    assert ApprovalRequest.model_validate(extra).workflow_id == "wf_1"


def test_resume_command_rejects_unknown_decision() -> None:
    with pytest.raises(ValidationError):
        ResumeCommand(decision="approved_by_model", version=1, draft_hash="h")


def test_snapshot_is_json_serialisable() -> None:
    snapshot = WorkflowSnapshot(
        workflow_id="wf_1",
        thread_id="wf_1",
        execution_status="waiting_approval",
        current_step="generate_email_draft",
        approval=ApprovalRequest(
            workflow_id="wf_1", email_draft_id="m1", version=1, draft_hash="h"
        ),
        risks=[{"title": "r"}],
        todos=[{"content": "t"}],
    )
    text = json.dumps(snapshot.model_dump(), ensure_ascii=False)
    assert "waiting_approval" in text
    assert WorkflowSnapshot.model_validate(json.loads(text)).approval is not None
