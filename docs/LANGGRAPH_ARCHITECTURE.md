# LangGraph 有状态 Agent 工作流架构

> 本文说明"为什么把会议分析 → 风险/待办 → 邮件审批 → 发送"这条业务流程搬到 LangGraph 上，
> 以及搬过去之后**具体**靠什么机制保证"中断能恢复、重试不重复、发信可追溯"。
> 事实依据：`docs/LANGGRAPH_ARCHITECTURE_AUDIT.md`（改造前的真实代码审计）、
> `tests/test_workflow_*.py`（56 个用例）、`scripts/e2e_workflow_browser.py`（真实浏览器 E2E）、
> `benchmarks/workflow/`（40 场景对照实验）。

## 1. 为什么需要它：原生 Agent Loop 的四条硬限制

原生 Loop 在 `aieb/agent.py`（`while True` + `max_iterations`）里，是**单进程内存态**的：

| 限制 | 代码事实 | 后果 |
| --- | --- | --- |
| 状态只在内存 | `messages` 列表随进程消亡 | 进程重启 → 本轮所有中间结果全丢，无法"接着上次继续" |
| 没有节点级检查点 | 无 checkpoint 概念 | 不知道"分析做完了没""草稿落到哪一版" |
| 没有中断语义 | 工具调用是同步执行 | 无法在"发信前"停下来等人确认，只能靠调用方约定 `confirm=True` |
| 重试靠调用方 | Loop 本身不重试 | LLM 超时 = 整轮失败，前面已完成的落库工作白做 |

会议/邮件这条链路尤其不能靠"重跑一遍"来恢复：
重跑会**重复创建会议/风险/待办**，重跑发信会**重复打扰收件人**。
所以这里需要的不是"更强的 Agent"，而是**可持久化、可中断、可幂等重放的工作流引擎**。

## 2. 落点：四个不变量

整个改造围绕四个不变量设计，测试也按这四条组织：

1. **同一份会议文本只产生一条会议记录**（风险/待办同理）。
2. **发信必须绑定"具体草稿版本"**，旧版本审批不能确认新内容。
3. **进程在任何一步被杀掉，重启后都能继续**，且不重复产生业务数据。
4. **"不知道对方收没收到" ≠ "失败"**，这种状态绝不自动重试。

## 3. 图结构

13 个真实业务节点，含条件分支、并行分支与 fan-in：

```
START
  └─> load_meeting                 # 读暂存原文；命中同文本历史 → 复用，不重复分析
        ├─(复用命中)────────────────────────────────> generate_email_draft
        └─(新文本)─> analyze_meeting            # LLM 结构化分析（节点内可控重试）
                       ├─> validate_risks       # 风险过滤/去重（纯函数）
                       └─> extract_todos        # 待办抽取（纯函数）
                              └─(fan-in)─> merge_results
                                             └─> persist_business_state   # 幂等落库
                                                   └─> generate_email_draft
                                                         └─> human_approval  # interrupt() 真中断
                                                               ├─(approve)→ send_email      # 幂等 + 不确定态保护
                                                               ├─(edit)───→ update_draft ──┐
                                                               ├─(reject)→ cancelled        │
                                                               └─(版本/哈希不符)─────────────┴─> validation_error
                                                                                         
  {send_email | cancelled | validation_error} ─> record_audit ─> END
```

关键设计取舍（都有对应测试）：

- **并行分支不写标量状态**。`validate_risks` / `extract_todos` 只写各自的列表字段；
  若两边都写 `current_step`，LangGraph 会抛
  `InvalidUpdateError: Can only receive one value per step at key 'current_step'`。
  这不是 bug，是框架在保护"合并语义必须明确"。
- **落库在发信之前**。审批时用户看到的草稿一定已经存在于业务库，避免"界面上有、库里没有"。
- **`human_approval` 是纯节点**：它只做 `interrupt(payload)` + 校验，不发信、不改库。

## 4. 类型化状态（Typed State）

`aieb/workflow/state.py` 的 `WorkflowState` 是 `TypedDict`，用 reducer 明确"并行写入怎么合并"：

| 字段 | reducer | 语义 |
| --- | --- | --- |
| `risk_candidates` / `validated_risks` / `todo_candidates` | `merge_by_key` | 按 key 去重合并，后来的覆盖同 key |
| `action_items` / `decisions` | `extend_unique` | 追加去重 |
| `dropped_risks` / `errors` / `node_trace` | `operator.add` | 顺序追加，保留完整证据 |

`STATE_SCHEMA_VERSION = 1` 与 `ApprovalRequest.schema_version` 一起，用于
"老 checkpoint + 新代码"时能识别出结构不一致而不是静默错乱。

## 5. 持久化：三份 SQLite，各管一件事

| 文件 | 表 | 作用 |
| --- | --- | --- |
| `<ws>/.aieb/workflow/checkpoints.sqlite3` | LangGraph 自管 | **图状态**：每个 superstep 的状态快照、待恢复的 interrupt |
| `<ws>/.aieb/workflow/workflow.sqlite3` | `workflow_runs` / `workflow_events` / `workflow_idempotency` | **可观测性 + 幂等账本** |
| `<ws>/.aieb/brain.sqlite3` | `meetings` / `risks` / `todos` / `emails` / `activities` | **业务数据**（改造前就有） |

刻意分开的原因：
checkpoint 是框架内部格式（升级 LangGraph 可能不兼容），业务库要保持人可读、可 SQL 查、
可被现有 RPC 直接复用。**把业务数据塞进 checkpoint 会让"数据"和"执行状态"耦合死。**

三份库都在同一个 workspace 目录下，且都通过 `workspace` 参数注入 —— 测试用独立临时目录，
不碰 `.aieb/` 真实数据。

### Checkpoint 为什么必须是异步版

`aieb/workflow/checkpointer.py` 里 `SqliteSaver`（同步）在图上存在 async 节点时会直接抛出
`SqliteSaver does not support async methods`。因此 Runner 用 `AsyncSqliteSaver` +
**专属事件循环线程**（`aieb/workflow/runner.py` 的 `_run_loop`），所有图调用通过
`asyncio.run_coroutine_threadsafe` 投递。这样做的代价是 Runner 与事件循环绑定，
好处是同一份代码在 FastAPI/uvicorn 与脚本里行为一致。

## 6. 中断与人工审批（真实 interrupt/resume）

- 节点内 `interrupt(request.model_dump())` 把控制权交回调用方，图**不是**失败，
  状态是 `waiting_approval`（测试 `test_interrupt_is_not_failure` 覆盖）。
- 审批载荷 `ApprovalRequest` 携带 `email_draft_id` + `version` + `draft_hash` +
  `to_addr/subject/body`，并声明 `allowed_actions`。
- 恢复时 `ResumeCommand(decision, version, draft_hash, ...)` 会做**两次**校验：
  1. 与 checkpoint 里挂起的中断载荷比对（版本 + 哈希）；
  2. 与**当前业务库里的草稿**比对（防止"审批期间另一个人改了草稿"）。
- 任一不一致 → `WorkflowError("审批已过期…"/"草稿已被改动…")`，路由到 `validation_error`，
  **不会**发出一封内容与审批内容不一致的邮件。

前端（`aieb/brain/ui/app_js.py`）配合的一条硬规则：
用户在审批卡片里改了正文但没点"保存修改"就点"确认发送" → 前端拒绝并提示，
避免"用户以为发的是改后的内容，实际发的是旧草稿"。

## 7. 幂等与"不确定态"

`workflow_idempotency` 是一张效果账本，key = `effect_key(workflow_id, action, object_id, version)`：

| 已有状态 | 再次请求 | 为什么 |
| --- | --- | --- |
| `pending` / `failed` / `cancelled` | 允许执行 | 没生效过，可以再来 |
| `executing` | 拒绝 | 正在执行，重复进入 = 并发双发 |
| `succeeded` | 拒绝并返回既有结果 | 已生效，不能重复 |
| `uncertain` | 拒绝，**只能人工核实** | "可能已发出"，自动重试会重复打扰 |

`send_email` 节点把发送结果映射成三态：
明确拒绝（SMTP 4xx/5xx）→ `failed`（可人工重试，走 `workflow.retry`）；
抛异常/超时 → `uncertain`（标记待核实，绝不自动重试）；
成功 → `succeeded`。

> **诚实边界**：这套机制实现的是"单业务库 + 单 Mailer 下的幂等"，**不是**跨系统的
> Exactly-Once Delivery。SMTP 没有事务性确认，"进程在 `send` 返回后、写库前被杀"
> 这类窗口只能靠 `uncertain` + 人工核实兜底。文档与实现都不宣称更强的保证。

## 8. 可控重试（以及为什么不用框架 RetryPolicy）

`analyze_meeting` / `generate_email_draft` 节点内用 `nodes.with_retry` 做显式重试
（fake/测试默认 2 次尝试，生产 `retry_attempts=1` 以避免与 `analyze_async` 内部重试叠加），
每次失败写一条 `workflow_events(event="retry")`。

> **重要更正**：早期结论"langgraph 1.2.20 的 `RetryPolicy` 不触发"是**错的**。
> 源码核查发现 `langgraph/_internal/_retry.py` 的 `default_retry_on()` 明确把
> `RuntimeError / ValueError / OSError` 等判为"不可重试"，而当时的探针正好用了 `RuntimeError`。
> 用 `python -m benchmarks.workflow.retry_policy_probe` 跑 32 组组合后确认：
> 默认策略 + `ConnectionError` 会重试 3 次；显式 `retry_on=(RuntimeError,)` 也会重试 3 次；
> 同步/异步、有无 checkpointer 行为一致。回归测试：`tests/test_workflow_retry_policy.py`。

既然框架可用，为什么还保留节点内重试？因为：

1. 业务上要区分"LLM 超时可重试"与"审批过期不可重试"，后者不是异常类型能简单表达的；
2. 重试次数要直接进 Trace（`retry` 列），而不是数外层重跑了多少次；
3. 不想让两层重试叠乘（2 × 3 = 6 次模型调用）。

`send_email` / `persist_business_state` **永不**自动重试，它们的重复执行由幂等账本 +
人工判断处理。

## 9. 可观测性

- `workflow_events` 每个节点一条：`node` / `event` / `status` / `duration_ms` /
  `model` / `input_tokens` / `output_tokens` / `retry` / `error_type` /
  `checkpoint_id` / `detail`。
- `workflow_runs` 记录 `status` / `current_step` / `started_at` / `completed_at` /
  `error`，页面刷新后前端拿到的是**后端真实状态**而不是前端内存。
- UI 的"执行轨迹"直接读这些事件，不做二次加工。

## 10. LangChain 适配层

`aieb/workflow/langchain_adapter.py`：

- `DeepSeekChatModel(BaseChatModel)`：把项目自有的 `create_client` + `ConversationManager`
  包成标准 ChatModel，`_generate`/`_stream` 都实现，并从 `StreamEnd` 里取
  `input_tokens/output_tokens`（Live 测试断言过 `input_tokens > 0`）。
- `to_langchain_tool`：同时提供 `func`（同步）与 `coroutine`（异步），
  否则 `StructuredTool` 在异步环境会报 `does not support sync invocation`。
- `structured_output(model, schema=...)`：`with_structured_output` 的薄封装，
  `AIEB_WORKFLOW_LLM=langchain` 时会议分析走这条路；默认仍走项目原有 `complete_json`。

这是"框架接管编排，不接管模型调用"的折中：换成 LangChain 的收益是能接生态里的
tool/structured-output/回调，代价是多一层适配，所以保留开关而不是全面替换。

## 11. 部署与环境变量

```powershell
python -m venv .venv-langgraph --system-site-packages
.venv-langgraph\Scripts\python.exe -m pip install -r requirements-langgraph.txt
.venv-langgraph\Scripts\python.exe -m aieb.brain.cli
```

| 变量 | 默认 | 作用 |
| --- | --- | --- |
| `AIEB_WORKFLOW_LLM` | 空（走项目自有 JSON 调用） | 设为 `langchain` 时会议分析走 LangChain 适配层 |
| `AIEB_TEST_LIVE_LLM` | 空 | 设为 `1` 时才跑真实 LLM 测试（会花钱） |
| `AIEB_SMTP_*` | 见 `aieb/brain/mailer.py` | SMTP 配置；未配置时发送为 dry-run，且**返回失败码**而不是假装成功 |

不装 LangGraph 时：`aieb.workflow` 的导入抛 `ImportError` 并提示安装
`requirements-langgraph.txt`；应用其余部分（Agent Loop、知识库、会议分析 REST/UI）
完全不受影响，相关测试自动 skip（`pytest.importorskip("langgraph")`）。

## 12. 明确的局限

1. **延迟更高**：40 场景对照实验里 LangGraph P50 约 166 ms，原生约 15 ms
   （同一批 fake 依赖，差异主要来自 checkpoint 序列化与持久化）。详见
   `docs/LANGGRAPH_BENCHMARK.md`，不做粉饰。
2. **不是跨系统 Exactly-Once**：见 §7 的边界说明。
3. **框架级 RetryPolicy 可用但未采用**：改用节点内显式重试（原因见 §8），并纠正了早期"框架不重试"的错误归因。
4. **Runner 与事件循环绑定**：不适用于同一 Runner 被多线程随意共享的场景；
   跨线程调用必须走 `*_sync` 包装。
5. **原生 Agent Loop 未改动**：本模块是并列的第二条编排路径，不是对 Loop 的替换。

## 13. 相关文档

- `docs/LANGGRAPH_WORKFLOW.md` —— 节点契约、状态表、幂等键、错误语义
- `docs/LANGGRAPH_RECOVERY_TEST.md` —— 断点恢复 A–I 场景与真实浏览器 E2E 证据
- `docs/LANGGRAPH_BENCHMARK.md` —— 40 场景对照实验（含不利数据）
- `docs/LANGGRAPH_INTERVIEW_SUMMARY.md` —— 面试视角的技术叙事与追问
