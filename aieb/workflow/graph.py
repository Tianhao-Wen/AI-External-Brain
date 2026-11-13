"""StateGraph 定义：节点注册、并行分支、条件路由、编译（带持久化 Checkpointer）。

拓扑（与 docs/LANGGRAPH_WORKFLOW.md 的图一致）：

    START
      ↓
    load_meeting ──(同一份纪要已处理过)──────────────┐
      ↓ (首次)                                        │
    analyze_meeting                                   │
      ├──────────────┐  （并行）                       │
    validate_risks  extract_todos                     │
      └──────┬───────┘                                 │
         merge_results                                 │
             ↓                                         │
    persist_business_state  ←──────────────────────────┘
             ↓
    generate_email_draft
             ↓
    human_approval ←──────────────┐
             │ 条件路由            │
      ┌──────┼────────┬───────────┴───┐
   approve  edit    reject        invalid
      ↓      ↓        ↓              ↓
  send_email update_draft cancelled validation_error
      ↓        └──→ human_approval
  record_audit
      ↓
     END
"""

from __future__ import annotations

from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph

from aieb.workflow.deps import WorkflowDeps
from aieb.workflow.nodes import build_nodes
from aieb.workflow.state import WorkflowState

#: 允许重试的节点（纯 LLM/读操作，不产生副作用）。
#: 重试由节点内的 `nodes.with_retry` 实现，**不用**框架 RetryPolicy ——
#: 在 langgraph 1.2.20 上实测节点级 retry_policy 不触发（有自己的探针记录），
#: 而且业务上我们要能区分“LLM 超时可重试”与“审批过期不可重试”。
RETRYABLE_NODES = ("analyze_meeting", "generate_email_draft")

#: 节点中文名（前端/Trace 展示用）
NODE_LABELS = {
    "load_meeting": "载入会议纪要",
    "analyze_meeting": "模型结构化抽取",
    "validate_risks": "风险校验（接地+规则）",
    "extract_todos": "抽取待办",
    "merge_results": "合并结果",
    "persist_business_state": "写入业务库",
    "generate_email_draft": "生成邮件草稿",
    "human_approval": "等待人工审批",
    "update_draft": "更新草稿（升版）",
    "send_email": "发送邮件",
    "cancelled": "已取消",
    "validation_error": "校验失败",
    "record_audit": "记录审计",
}


def route_after_load(state: WorkflowState) -> str:
    """同一份纪要已经存在 → 跳过分析/落库，直接去生成草稿（避免重复副作用）。"""
    return "generate_email_draft" if state.get("meeting_id") else "analyze_meeting"


def route_after_approval(state: WorkflowState) -> str:
    """审批结果的条件路由（这就是 Conditional Edges 的实际用途）。"""
    status = str(state.get("approval_status") or "")
    if status == "approved":
        return "send_email"
    if status == "edited":
        return "update_draft"
    if status == "rejected":
        return "cancelled"
    return "validation_error"


def build_graph(
    deps: WorkflowDeps,
    *,
    checkpointer: BaseCheckpointSaver | None = None,
    retry_max_attempts: int = 2,
) -> Any:
    """构建并编译 StateGraph。"""
    nodes = build_nodes(deps)
    builder = StateGraph(WorkflowState)

    for name, fn in nodes.items():
        builder.add_node(name, fn, metadata={"label": NODE_LABELS.get(name, name)})

    builder.add_edge(START, "load_meeting")
    builder.add_conditional_edges(
        "load_meeting", route_after_load, ["analyze_meeting", "generate_email_draft"]
    )

    builder.add_edge("analyze_meeting", "validate_risks")
    builder.add_edge("analyze_meeting", "extract_todos")
    # fan-in：两个并行分支都完成后才继续（LangGraph 保证同一 super-step 内各跑一次）
    builder.add_edge(["validate_risks", "extract_todos"], "merge_results")

    builder.add_edge("merge_results", "persist_business_state")
    builder.add_edge("persist_business_state", "generate_email_draft")
    builder.add_edge("generate_email_draft", "human_approval")

    builder.add_conditional_edges(
        "human_approval",
        route_after_approval,
        {
            "send_email": "send_email",
            "update_draft": "update_draft",
            "cancelled": "cancelled",
            "validation_error": "validation_error",
        },
    )
    # 修改后必须重新审批：这是"审批绑定版本"的落地方式
    builder.add_edge("update_draft", "human_approval")

    for terminal in ("send_email", "cancelled", "validation_error"):
        builder.add_edge(terminal, "record_audit")
    builder.add_edge("record_audit", END)

    return builder.compile(checkpointer=checkpointer, name="meeting_workflow")
