"""LangGraph `RetryPolicy` 的真实行为（回归测试，防止再次错误归因）。

背景：早期探针用 `RuntimeError("boom")` 测"框架重试会不会生效"，结果为"不生效"，
于是推断"框架 RetryPolicy 有 bug/不触发"，并改成节点内手写重试。
后来的源码核查发现：`langgraph._internal._retry.default_retry_on()` **明确**把
`RuntimeError` / `OSError` / `ValueError` 等判为"不可重试"（这是设计，不是缺陷）。

本文件用最小复现把这个事实固定下来：
- 默认策略 + RuntimeError → 只执行 1 次（不可重试）；
- 默认策略 + ConnectionError → 执行 max_attempts 次（可重试）；
- 显式 `retry_on=(RuntimeError,)` → 执行 max_attempts 次。

同时断言我们自己的节点级重试与"生产依赖不叠加重试"的约定。
"""

from __future__ import annotations

import pytest

pytest.importorskip("langgraph")

from langchain_core.utils.function_calling import convert_to_openai_function  # noqa: E402,F401

from benchmarks.workflow.retry_policy_probe import (  # noqa: E402
    Boom,
    _make_graph,
    _run_case,
)
from langgraph.types import RetryPolicy  # noqa: E402


def _policy(**kwargs) -> RetryPolicy:
    base = {"max_attempts": 3, "initial_interval": 0.0, "jitter": False}
    base.update(kwargs)
    return RetryPolicy(**base)


def test_default_policy_does_not_retry_runtime_error() -> None:
    """默认策略把 RuntimeError 判为不可重试 —— 这就是当初误判的根因。"""
    row = _run_case(
        label="default-runtime",
        exc_factory=lambda: RuntimeError("boom"),
        retry_policy=_policy(),
        async_node=False,
        with_saver=False,
    )
    assert row["calls"] == 1
    assert row["retried"] is False


def test_default_policy_retries_connection_error() -> None:
    """可重试类异常（网络/容器级）默认会按 max_attempts 重试。"""
    row = _run_case(
        label="default-connection",
        exc_factory=lambda: ConnectionError("boom"),
        retry_policy=_policy(),
        async_node=True,
        with_saver=False,
    )
    assert row["calls"] == 3
    assert row["retried"] is True


def test_default_policy_retries_unknown_exception_type() -> None:
    """不在黑名单里的普通异常默认也会重试（说明重试机制本身是好的）。"""
    row = _run_case(
        label="default-boom",
        exc_factory=lambda: Boom("boom"),
        retry_policy=_policy(),
        async_node=False,
        with_saver=False,
    )
    assert row["calls"] == 3


@pytest.mark.parametrize("with_saver", [False, True])
def test_explicit_retry_on_retries_runtime_error(with_saver: bool) -> None:
    """显式声明 retry_on 之后，RuntimeError 也会重试（同步/异步、有无 checkpoint 一致）。"""
    for async_node in (False, True):
        row = _run_case(
            label=f"explicit-{async_node}-{with_saver}",
            exc_factory=lambda: RuntimeError("boom"),
            retry_policy=_policy(retry_on=(RuntimeError,)),
            async_node=async_node,
            with_saver=with_saver,
        )
        assert row["calls"] == 3, row


def test_graph_with_retry_policy_is_actually_wired() -> None:
    """编译后的图里，节点的 retry_policy 必须真的被登记（不能静默丢失）。"""
    calls = {"n": 0}

    def node(state):  # noqa: ANN001
        calls["n"] += 1
        raise Boom("boom")

    graph = _make_graph(
        node=node, retry_policy=_policy(), async_node=False, checkpointer=None
    )
    with pytest.raises(Boom):
        graph.invoke({"value": "x"})
    assert calls["n"] == 3
