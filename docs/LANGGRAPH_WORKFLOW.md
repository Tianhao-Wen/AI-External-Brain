# LangGraph 工作流规格（节点契约 / 状态 / 幂等 / 错误语义）

> 面向"要改这段代码的人"。架构动机见 `docs/LANGGRAPH_ARCHITECTURE.md`，
> 恢复与压测证据见 `docs/LANGGRAPH_RECOVERY_TEST.md` / `docs/LANGGRAPH_BENCHMARK.md`。
> 代码位置：`aieb/workflow/{state,nodes,graph,persistence,runner,deps}.py`。

## 1. 图定义

```python
# aieb/workflow/graph.py
builder = StateGraph(WorkflowState)
builder.add_edge(START, "load_meeting")
builder.add_conditional_edges("load_meeting", route_after_load,
                             {"analyze_meeting": "analyze_meeting",
                              "generate_email_draft": "generate_email_draft"})
builder.add_edge("analyze_meeting", "validate_risks")
builder.add_edge("analyze_meeting", "extract_todos")     # 并行分支
builder.add_edge(["validate_risks", "extract_todos"], "merge_results")   # fan-in
builder.add_edge("merge_results", "persist_business_state")
builder.add_edge("persist_business_state", "generate_email_draft")
builder.add_edge("generate_email_draft", "human_approval")
builder.add_conditional_edges("human_approval", route_after_approval, {...})
builder.add_edge("update_draft", "human_approval")       # 修改后重新审批
builder.add_edge("send_email", "record_audit")
builder.add_edge("cancelled", "record_audit")
builder.add_edge("validation_error", "record_audit")
builder.add_edge("record_audit", END)
graph = builder.compile(checkpointer=checkpointer, name="aieb_meeting_email")
```

`compile(name=...)` 不是装饰：checkpoint 里会记录图名，改图结构后能识别出"老 checkpoint 对不上新图"。

## 2. 状态字段

`WorkflowState`（`TypedDict`，`STATE_SCHEMA_VERSION = 1`）。

**标识类**（只写一次）

| 字段 | 说明 |
| --- | --- |
| `workflow_id` / `thread_id` | 工作流 id 即 LangGraph 的 `thread_id`，`config = {"configurable": {"thread_id": workflow_id}}` |
| `session_id` | 触发本次运行的 UI 会话 |
| `meeting_id` / `document_id` | 业务库会议 id；暂存原文的文件名 |
| `source_name` / `text_hash` | 原始文件名、正文哈希（复用判定的 key） |

**业务中间态**

| 字段 | reducer | 说明 |
| --- | --- | --- |
| `risk_candidates` / `todo_candidates` | `merge_by_key` | LLM 原始候选 |
| `validated_risks` | `merge_by_key` | 过 `risk_rules.filter_risks` 后的风险 |
| `dropped_risks` | `operator.add` | 被过滤掉的（保留证据，便于解释"为什么少了一条"） |
| `action_items` / `decisions` | `extend_unique` | 会议产出的行动项/决议 |
| `meeting_summary` / `meeting_title` | 覆盖 | 会议摘要与标题 |

**审批/发信**

| 字段 | 说明 |
| --- | --- |
| `to_addr` / `extra_instruction` | 收件人、草稿额外指令 |
| `email_draft_id` / `email_version` / `email_draft_hash` | 草稿身份三元组，**审批绑定就靠这三个** |
| `edit_to_addr` / `edit_subject` / `edit_body` | 用户修改意图（由 resume 写入，`update_draft` 消费） |
| `approval_status` | `none / pending / approved / rejected / edited / invalid` |
| `approval_version` / `approval_note` | 审批针对的版本；审批备注 |
| `execution_status` | `running / waiting_approval / completed / failed / cancelled` |
| `current_step` | 最近执行的节点名（**只允许串行节点写**） |
| `errors` | `operator.add`，元素 `{node, type, message}` |
| `retry_count` | 节点内重试累计 |
| `started_at` / `completed_at` | 时间戳 |
| `node_trace` | `operator.add`，节点级轨迹（UI"执行轨迹"直接读它 + `workflow_events`） |

> ⚠️ **并行分支不能写标量字段**。`validate_risks` 与 `extract_todos` 都只写自己的列表；
> 若同时写 `current_step`，LangGraph 抛
> `InvalidUpdateError: Can only receive one value per step at key 'current_step'`。
> `tests/test_workflow_state.py` 有一条用例专门守住这个约束。

## 3. 节点契约（13 个）

`traced(name, deps, fn)` 统一包装：写 `workflow_events`（耗时/模型/token/重试/错误类型/
checkpoint_id）、把 `_usage` 从输出里摘掉并追加 `node_trace`。

| 节点 | 输入 | 输出（主要字段） | 副作用 | 可重试 |
| --- | --- | --- | --- | --- |
| `load_meeting` | `document_id`/`text_hash` | `meeting_id`（复用命中时）、`current_step` | 只读 | 否（无意义） |
| `analyze_meeting` | 暂存原文、`source_name` | `meeting_summary/title`、`risk_candidates`、`todo_candidates`、`decisions`、`action_items`、`retry_count` | 只读（LLM 调用） | ✅ 节点内 2 次 |
| `validate_risks` | `risk_candidates` | `validated_risks`、`dropped_risks` | 只读（纯函数） | — |
| `extract_todos` | `todo_candidates` | `todo_candidates`（规范化） | 只读（纯函数） | — |
| `merge_results` | 两分支结果 | 合并后列表 | 只读 | — |
| `persist_business_state` | `meeting_id`、合并结果 | `meeting_id` | **写业务库**（幂等键 `persist_meeting`） | ❌ |
| `generate_email_draft` | meeting、`to_addr` | `email_draft_id`、`email_version=1`、`email_draft_hash` | **写 emails**（幂等键 `draft_email`） | ✅ 节点内 2 次（落库前失败才算） |
| `human_approval` | `email_draft_id` | `approval_*`、`execution_status` | **纯**，`interrupt()` | ❌ |
| `update_draft` | `edit_*` + 当前草稿 | `email_version+1`、新 `draft_hash`、`pending` | **写 emails**（版本 +1） | ❌ |
| `send_email` | `email_draft_id`/`version` | `execution_status` | **发信**（幂等键 `send_email`） | ❌ 永不自动重试 |
| `cancelled` | — | `execution_status="cancelled"` | 只读 | — |
| `validation_error` | — | `execution_status="failed"` | 只读 | — |
| `record_audit` | 全量 | — | 删暂存原文 + 写 activities | 否（幂等） |

**原文不进 checkpoint**：会议原文存在 `inbox/<document_id>.txt`，
state 里只有 `document_id` 与 `text_hash`。这样 checkpoint 不会因为长文本膨胀，
也不用担心"把敏感原文复制到 N 个快照里"。`record_audit` 结束时删除暂存文件。

## 4. 路由函数

```python
route_after_load(state) -> "analyze_meeting" | "generate_email_draft"
# 命中同文本历史（allow_reuse）→ 直接去生成草稿（复用已落库的会议）

route_after_approval(state) -> "send_email" | "update_draft" | "cancelled" | "validation_error"
# approval_status: approved→send_email, edited→update_draft, rejected→cancelled, invalid→validation_error
```

## 5. 幂等键

```python
effect_key(workflow_id, action, object_id="", version=0)
# "wf:8f3a|send_email:em-12@v2"
```

| action | object_id | version | 说明 |
| --- | --- | --- | --- |
| `persist_meeting` | `meeting_id` | 0 | 一次运行只允许落一次会议/风险/待办 |
| `draft_email` | `meeting_id` | 草稿版本 | 生成草稿 |
| `send_email` | `email_draft_id` | `version` | **按版本隔离**：v1 发过、v2 可以再发（这是正确行为，不是重复） |

状态机：`begin_effect()` 用 `BEGIN IMMEDIATE` 拿写锁后判断：
`pending/failed/cancelled` → 允许；`executing/succeeded/uncertain` → 拒绝。
`finish_effect()` 落终态 + `error`。账本行永不删除 —— 它就是审计证据。

## 6. 错误语义（三态，必须区分）

| 情况 | 判定 | 后续动作 |
| --- | --- | --- |
| SMTP 明确拒绝（4xx/5xx、鉴权失败） | `failed` | 允许人工重试：`workflow.retry` |
| 发送抛异常/超时（不知道对方收没收到） | `uncertain` | **禁止自动重试**，UI 显示"待人工核实" |
| 成功 | `succeeded` | `execution_status=completed` |

其它错误分类：

| 异常 | 归属节点 | 效果 |
| --- | --- | --- |
| LLM 报错（首次） | `analyze_meeting` | 写 `retry` 事件，节点内重试 |
| LLM 报错（第 2 次仍失败） | `analyze_meeting` | run `failed`，不落任何业务行 |
| 审批版本/哈希不匹配 | `human_approval` | `StaleApproval` → `validation_error`，不发信 |
| 恢复时草稿已被改动 | `runner._resume` | `WorkflowError("草稿已被改动…")`，不发信 |
| 幂等键为 `executing/uncertain` | `send_email` | `UncertainDelivery` → `failed` + 邮件标记 `uncertain` |

## 7. 关于框架 `RetryPolicy` 的结论（**已更正**）

> ⚠️ 本节曾给出错误结论（"langgraph 1.2.20 的 retry_policy 不触发"）。
> 源码核查 + 32 组复现后**推翻**了它，原因见下。原结论保留在此处作为教训。

LangGraph 的节点重试**是好的**。真正的原因是 `default_retry_on()` 把
`RuntimeError / ValueError / OSError / TypeError / LookupError / ...` 明确判为"不可重试"：

```python
# langgraph/_internal/_retry.py（已安装版本的实际代码）
if isinstance(exc, (ValueError, TypeError, ArithmeticError, ImportError, LookupError,
                    NameError, SyntaxError, RuntimeError, ReferenceError,
                    StopIteration, StopAsyncIteration, OSError)):
    return False
return True
```

早期探针用的是 `raise RuntimeError("boom")` → 正好命中黑名单 → 误判为"框架不重试"。

实测矩阵（`python -m benchmarks.workflow.retry_policy_probe`，32 组 = 8 种策略 × 同步/异步 × 有无 checkpointer）：

| 组合 | 节点调用次数 | 结论 |
| --- | --- | --- |
| 默认策略 + `RuntimeError` | 1 | ❌ 不重试（符合 `default_retry_on`） |
| 默认策略 + `OSError` | 1 | ❌ 不重试 |
| 默认策略 + `ConnectionError` | 3 | ✅ 按 `max_attempts` 重试 |
| 默认策略 + 自定义 `Boom(Exception)` | 3 | ✅ 重试 |
| 显式 `retry_on=(RuntimeError,)` / `=RuntimeError` / `=lambda e: True` | 3 | ✅ 重试 |
| 不设置 `retry_policy` | 1 | ❌ 不重试（预期） |

同步/异步节点、`InMemorySaver`/`AsyncSqliteSaver` 四种组合结果完全一致。
回归守卫：`tests/test_workflow_retry_policy.py`（6 条）。

### 为什么项目仍然用节点内 `with_retry`

不是因为框架不行，而是业务需要（详见 `docs/LANGGRAPH_FINAL_AUDIT.md` §7）：

1. 要区分"LLM 超时可重试"与"审批过期不可重试"；
2. 重试次数要直接进 Trace（`workflow_events.retry`），而不是靠数外层重跑了多少次；
3. 生产依赖 `meetings.analyze_async` 内部已重试 2 次，叠加框架 `max_attempts=3` 会放大到 6 次模型调用，
   所以 `build_real_deps()` 显式 `retry_attempts=1`（不叠加）。

```python
outcome, retries = await with_retry(
    lambda: deps.analyzer(source_name, text),
    attempts=deps.retry_attempts,          # fake/测试默认 2；生产 1（内部已重试）
    on_error=lambda attempt, exc: deps.wf.append_event(
        workflow_id, node="analyze_meeting", event="retry", status="error",
        retry=attempt, error_type=type(exc).__name__,
    ),
)
```

好处：重试次数可见（`retry` 列）、可被测试断言（`test_I_transient_failure_recovers_within_one_run`、
`test_A2_transient_draft_failure_is_retried_without_duplicate_meeting`）、
且不会误把"审批过期"当成可重试错误。

## 8. RPC / UI 契约

| op | 入参 | 返回 |
| --- | --- | --- |
| `workflow.start` | `name`,`text`(或 base64 `data`),`to_addr`,`extra_instruction` | `WorkflowSnapshot` |
| `workflow.status` | `workflow_id` | `WorkflowSnapshot`（含 `resumable`、`pending_approval`） |
| `workflow.resume` | `workflow_id`,`decision`(approve/edit/reject),`version`,`draft_hash`,`to/subject/body`,`note` | `WorkflowSnapshot` |
| `workflow.cancel` | `workflow_id` | `WorkflowSnapshot` |
| `workflow.retry` | `workflow_id` | 仅对 `failed`（明确失败）有效 |
| `workflow.list` / `workflow.history` | — | 运行列表 / 事件轨迹（Trace） |

前端硬规则：审批卡片里改动过正文 → 必须先"保存修改"（版本 +1、重新审批），
否则"确认发送"被前端的 `bindWorkflowCard` 拒绝，避免发出与界面不一致的内容。

## 9. 加/改节点的检查清单

1. 新节点是否会写**标量**状态？如果是，它不能落在并行分支里。
2. 是否有副作用？有 → 必须给幂等键，并想清楚 `uncertain` 怎么表达。
3. 是否需要重试？只有"纯读 + 无副作用"的节点可以自动重试。
4. 是否要写进 Trace？`traced()` 自动写；额外细节放 `_usage["detail"]`。
5. 加测试：`tests/test_workflow_graph.py`（结构）、`_recovery`（恢复）、`_idempotency`（重复执行）。
