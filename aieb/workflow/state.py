"""工作流状态：Typed State、Reducer 与对外契约（Pydantic）。

职责边界（很重要，避免"所有东西塞进一个 State"）：

| 数据 | 存放位置 | 说明 |
| --- | --- | --- |
| Graph 执行状态 | `WorkflowState`（本文件） | 只放**必要 ID 与结构化摘要**，不放会议原文/邮件正文全集 |
| Checkpoint 快照 | `checkpoints.sqlite3`（LangGraph SqliteSaver） | 每个 super-step 一份，用于恢复 |
| 业务真相 | `brain.sqlite3`（会议/风险/待办/邮件/记忆） | 唯一真源；Graph 只写这些表 |
| 审计日志 | `workflow.sqlite3` 的 `workflow_events` | 节点级 Trace，仅本地 |
| 前端展示状态 | `WorkflowSnapshot`（由 Runner 组装） | 从 Checkpoint + 运行表派生，不单独存 |

也就是说：**State 是执行上下文，不是业务库的副本**。
会议原文通过 `meeting_id` 引用业务库里的 `meetings.raw_text`，避免在 Checkpoint 里长期留存大段敏感原文。
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, Literal, TypedDict

from pydantic import BaseModel, Field

#: 执行状态机（Graph 内部的 execution_status）
ExecutionStatus = Literal[
    "pending",
    "running",
    "waiting_approval",
    "completed",
    "cancelled",
    "failed",
]

#: 审批结论
ApprovalDecision = Literal["approve", "edit", "reject"]

#: 审批状态
ApprovalStatus = Literal["none", "pending", "approved", "edited", "rejected", "invalid"]


# ---------------------------------------------------------------------------
# Reducer
# ---------------------------------------------------------------------------


def merge_by_key(left: list[dict[str, Any]] | None, right: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """按 `key` 合并列表：同键覆盖、新键追加、保持原顺序。

    并行分支各自写自己的字段时不需要它；但一旦两个节点都可能追加同一类对象
    （例如 validate/merge 两处都想补风险字段），就必须有明确规则，
    否则"谁后写谁赢"会让结果依赖调度顺序。
    """
    merged: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for item in list(left or []) + list(right or []):
        key = str(item.get("id") or item.get("key") or item.get("title") or item.get("content") or "")
        if key not in merged:
            order.append(key)
        merged[key] = {**merged.get(key, {}), **item}
    return [merged[k] for k in order]


def extend_unique(left: list[Any] | None, right: list[Any] | None) -> list[Any]:
    """追加去重（保序）：并行节点都往同一个 list 写也不会重复。"""
    out = list(left or [])
    for item in right or []:
        if item not in out:
            out.append(item)
    return out


def replace_scalar(_left: Any, right: Any) -> Any:
    """标量字段：后写覆盖（显式声明，避免读者以为有隐藏合并逻辑）。"""
    return right


# ---------------------------------------------------------------------------
# Typed State
# ---------------------------------------------------------------------------


class WorkflowState(TypedDict, total=False):
    """会议 → 风险/待办 → 邮件审批 → 发送 的执行状态。"""

    # --- 身份与关联 ---
    workflow_id: str
    thread_id: str
    session_id: str
    meeting_id: str
    document_id: str
    source_name: str
    #: 会议原文的 sha256[:16]，用于幂等与"同一份纪要只处理一次"
    text_hash: str
    #: 邮件相关输入（收件人、额外要求、用户编辑内容）
    to_addr: str
    extra_instruction: str
    edit_to_addr: str
    edit_subject: str
    edit_body: str

    # --- 结构化业务结果（只放结构化数据，不放原文）---
    meeting_summary: str
    meeting_title: str
    risk_candidates: Annotated[list[dict[str, Any]], merge_by_key]
    todo_candidates: Annotated[list[dict[str, Any]], merge_by_key]
    validated_risks: Annotated[list[dict[str, Any]], merge_by_key]
    dropped_risks: Annotated[list[dict[str, Any]], operator.add]
    action_items: Annotated[list[dict[str, Any]], merge_by_key]
    decisions: Annotated[list[str], extend_unique]

    # --- 邮件与审批 ---
    email_draft_id: str
    email_version: int
    email_draft_hash: str
    approval_status: str
    approval_version: int
    approval_note: str

    # --- 执行元信息 ---
    execution_status: str
    current_step: str
    errors: Annotated[list[dict[str, Any]], operator.add]
    retry_count: int
    started_at: str
    completed_at: str
    #: 节点级执行事件（同时在 workflow.sqlite3 落一份，State 里只留最近若干条摘要）
    node_trace: Annotated[list[dict[str, Any]], operator.add]


# ---------------------------------------------------------------------------
# 对外契约
# ---------------------------------------------------------------------------


#: State / 审批载荷的 schema 版本。升级字段时递增，旧 Checkpoint 可据此兼容。
STATE_SCHEMA_VERSION = 1


class ApprovalRequest(BaseModel):
    """`interrupt()` 抛给前端的内容：必须**自包含**到能独立渲染审批卡片。"""

    schema_version: int = STATE_SCHEMA_VERSION
    workflow_id: str
    email_draft_id: str
    version: int
    draft_hash: str
    to_addr: str = ""
    subject: str = ""
    body: str = ""
    allowed_actions: list[str] = Field(default_factory=lambda: ["approve", "edit", "reject"])
    created_at: str = ""


class ResumeCommand(BaseModel):
    """前端回传的审批决定。

    `version` + `draft_hash` 是**审批绑定的凭证**：
    恢复时若与当前草稿不一致，一律判为 `invalid`，绝不按旧审批发送新内容。
    """

    decision: ApprovalDecision
    version: int
    draft_hash: str
    to_addr: str = ""
    subject: str = ""
    body: str = ""
    note: str = ""
    decided_by: str = "user"


class WorkflowSnapshot(BaseModel):
    """给前端的只读快照（Checkpoint + 运行表派生，不单独持久化）。"""

    workflow_id: str
    thread_id: str
    execution_status: str
    current_step: str
    approval_status: str = "none"
    approval: ApprovalRequest | None = None
    meeting_id: str = ""
    email_draft_id: str = ""
    email_version: int = 0
    summary: str = ""
    risks: list[dict[str, Any]] = Field(default_factory=list)
    todos: list[dict[str, Any]] = Field(default_factory=list)
    errors: list[dict[str, Any]] = Field(default_factory=list)
    started_at: str = ""
    completed_at: str = ""
    resumable: bool = False
    next_nodes: list[str] = Field(default_factory=list)
    checkpoint_id: str = ""
    version: int = 1
