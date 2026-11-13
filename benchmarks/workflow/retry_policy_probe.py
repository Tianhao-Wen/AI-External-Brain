"""LangGraph `RetryPolicy` 最小复现（可重复执行、不联网、不依赖项目业务代码）。

用途：核实"节点级 retry_policy 到底会不会重试"，并以**事实**而不是印象记录结论。

    python -m benchmarks.workflow.retry_policy_probe

每个用例：一个必然抛异常的节点，统计节点被调用次数（`calls`）。
`max_attempts=3` 表示"最多试 3 次"，因此重试生效时 `calls == 3`。

结论（实测，langgraph 1.2.20 / Python 3.14.2 / Windows 11）：

1. **框架的重试机制本身是好的**，默认策略不重试 `RuntimeError` 是因为
   `langgraph._internal._retry.default_retry_on()` 明确把下列异常判为
   "不可重试"：ValueError / TypeError / ArithmeticError / ImportError /
   LookupError / NameError / SyntaxError / **RuntimeError** / ReferenceError /
   StopIteration / StopAsyncIteration / OSError，以及非 5xx 的 HTTP 错误。
2. 只要 `retry_on` 显式包含该异常类型（或给一个返回 True 的谓词），同步/异步节点、
   带/不带 checkpointer 都会按 `max_attempts` 重试。
3. 之前"RetryPolicy 不生效"的判断是**错误归因**：探针用的是 `RuntimeError("boom")`，
   正好落在默认策略的不可重试名单里。
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable

from langgraph.graph import END, START, StateGraph
from langgraph.types import RetryPolicy
from typing_extensions import TypedDict


class _State(TypedDict, total=False):
    value: str


class Boom(Exception):
    """不在 default_retry_on 黑名单里的普通异常。"""


def _make_graph(
    *,
    node: Callable[[_State], Any],
    retry_policy: RetryPolicy | None,
    async_node: bool,
    checkpointer: Any,
):
    async def anode(state: _State) -> dict[str, Any]:
        return node(state)

    def snode(state: _State) -> dict[str, Any]:
        return node(state)

    builder = StateGraph(_State)
    kwargs = {"retry_policy": retry_policy} if retry_policy is not None else {}
    builder.add_node("boom", anode if async_node else snode, **kwargs)
    builder.add_edge(START, "boom")
    builder.add_edge("boom", END)
    return builder.compile(checkpointer=checkpointer)


def _run_case(
    *,
    label: str,
    exc_factory: Callable[[], Exception],
    retry_policy: RetryPolicy | None,
    async_node: bool,
    with_saver: bool,
) -> dict[str, Any]:
    calls = {"n": 0}

    def node(state: _State) -> dict[str, Any]:
        calls["n"] += 1
        raise exc_factory()

    saver = None
    if with_saver:
        from langgraph.checkpoint.memory import InMemorySaver

        saver = InMemorySaver()

    graph = _make_graph(
        node=node, retry_policy=retry_policy, async_node=async_node, checkpointer=saver
    )
    config = {"configurable": {"thread_id": f"probe-{label}"}} if with_saver else {}
    error = ""
    try:
        asyncio.run(graph.ainvoke({"value": "x"}, config))
    except BaseException as exc:  # noqa: BLE001 - 探针要把任何异常都记下来
        error = f"{type(exc).__name__}: {exc}"
    return {
        "case": label,
        "async_node": async_node,
        "checkpointer": with_saver,
        "retry_on": _policy_desc(retry_policy),
        "calls": calls["n"],
        "retried": calls["n"] > 1,
        "error": error.splitlines()[0][:80],
    }


def _policy_desc(policy: RetryPolicy | None) -> str:
    if policy is None:
        return "（未设置）"
    fn = policy.retry_on
    if getattr(fn, "__name__", "") == "default_retry_on":
        return "default_retry_on"
    if isinstance(fn, tuple):
        return "(" + ", ".join(t.__name__ for t in fn) + ")"
    if isinstance(fn, type):
        return fn.__name__
    return "<callable>"


def cases() -> list[dict[str, Any]]:
    pol_default = RetryPolicy(max_attempts=3, initial_interval=0.0, jitter=False)
    pol_runtime = RetryPolicy(
        max_attempts=3, initial_interval=0.0, jitter=False, retry_on=(RuntimeError,)
    )
    pol_runtime_cls = RetryPolicy(
        max_attempts=3, initial_interval=0.0, jitter=False, retry_on=RuntimeError
    )
    pol_callable = RetryPolicy(
        max_attempts=3, initial_interval=0.0, jitter=False, retry_on=lambda exc: True
    )
    specs = [
        ("默认策略 + RuntimeError", lambda: RuntimeError("boom"), pol_default),
        ("默认策略 + Boom（非黑名单）", lambda: Boom("boom"), pol_default),
        ("默认策略 + OSError", lambda: OSError("boom"), pol_default),
        ("默认策略 + ConnectionError", lambda: ConnectionError("boom"), pol_default),
        ("显式 retry_on=(RuntimeError,)", lambda: RuntimeError("boom"), pol_runtime),
        ("显式 retry_on=RuntimeError", lambda: RuntimeError("boom"), pol_runtime_cls),
        ("显式 retry_on=lambda True", lambda: RuntimeError("boom"), pol_callable),
        ("未设置 retry_policy", lambda: RuntimeError("boom"), None),
    ]
    rows: list[dict[str, Any]] = []
    for label, factory, policy in specs:
        for async_node in (False, True):
            for with_saver in (False, True):
                rows.append(
                    _run_case(
                        label=label,
                        exc_factory=factory,
                        retry_policy=policy,
                        async_node=async_node,
                        with_saver=with_saver,
                    )
                )
    return rows


def main() -> int:
    rows = cases()
    header = f"{'用例':<30}{'异步':<6}{'CP':<4}{'retry_on':<20}{'调用次数':<10}{'重试':<6}错误"
    print(header)
    print("-" * len(header))
    for r in rows:
        print(
            f"{r['case']:<30}{('是' if r['async_node'] else '否'):<6}"
            f"{('有' if r['checkpointer'] else '无'):<4}{r['retry_on']:<20}"
            f"{r['calls']:<10}{('是' if r['retried'] else '否'):<6}{r['error']}"
        )
    ok = sum(1 for r in rows if r["retried"])
    print()
    print(f"共 {len(rows)} 组组合，其中 {ok} 组发生重试。")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
