"""LangGraph 有状态工作流（会议 → 风险/待办 → 邮件审批 → 发送）。

模块是**可选能力**：没装 langgraph 时导入本包会给出明确错误，但不会影响
原有 Agent Loop / RAG / 会议功能的启动（`aieb.brain.service` 不 import 本包）。
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "WorkflowRunner",
    "WorkflowError",
    "WorkflowDeps",
    "WorkflowStore",
    "WorkflowState",
    "ApprovalRequest",
    "ResumeCommand",
    "WorkflowSnapshot",
    "build_real_deps",
    "build_graph",
    "open_checkpointer",
]

#: 需要 langgraph 才能导入的名字
_LAZY = {
    "WorkflowRunner": ("aieb.workflow.runner", "WorkflowRunner"),
    "WorkflowError": ("aieb.workflow.runner", "WorkflowError"),
    "WorkflowDeps": ("aieb.workflow.deps", "WorkflowDeps"),
    "build_real_deps": ("aieb.workflow.deps", "build_real_deps"),
    "WorkflowStore": ("aieb.workflow.persistence", "WorkflowStore"),
    "WorkflowState": ("aieb.workflow.state", "WorkflowState"),
    "ApprovalRequest": ("aieb.workflow.state", "ApprovalRequest"),
    "ResumeCommand": ("aieb.workflow.state", "ResumeCommand"),
    "WorkflowSnapshot": ("aieb.workflow.state", "WorkflowSnapshot"),
    "build_graph": ("aieb.workflow.graph", "build_graph"),
    "open_checkpointer": ("aieb.workflow.checkpointer", "open_checkpointer"),
}


def __getattr__(name: str) -> Any:  # PEP 562：只有真的用到才 import langgraph
    target = _LAZY.get(name)
    if target is None:
        raise AttributeError(f"module 'aieb.workflow' 没有属性 {name!r}")
    module_name, attr = target
    try:
        module = __import__(module_name, fromlist=[attr])
    except ImportError as exc:  # pragma: no cover - 未安装可选依赖
        raise ImportError(
            "工作流功能需要安装可选依赖：pip install -r requirements-langgraph.txt"
        ) from exc
    return getattr(module, attr)
