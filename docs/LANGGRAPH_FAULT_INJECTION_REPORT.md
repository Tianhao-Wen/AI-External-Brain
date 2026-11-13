# Checkpoint 故障注入与并发实验报告

> 原始数据：`benchmarks/results/workflow/fault_injection.json`（19 个场景，逐条含注入点/预期/实际/DB 终态）
> 复现：`python -m benchmarks.workflow.fault_injection`
> 环境：Windows 11 / Python 3.14.2 / langgraph 1.2.20 / 真实 SQLite（WAL）
> **结果：19/19 符合预期**；全部场景 `duplicate_side_effects = 0`

## 0. 方法说明（为什么这组实验是可信的）

| 关注点 | 做法 |
| --- | --- |
| "真重启"还是"假装重启" | R 组用 `subprocess` 起**独立进程**，崩溃是 `os._exit(9)`（跳过 `finally`、不关连接）；恢复是另一个进程重新打开同一个 checkpoint 文件 |
| 副作用怎么跨进程计数 | fake sender 把投递记录追加到 `<ws>/sends.log`，业务数据从真实 SQLite 读 |
| 真实 Checkpointer | `AsyncSqliteSaver`（`<ws>/.aieb/workflow/checkpoints.sqlite3`），不是 InMemorySaver |
| 故障如何精确注入 | `WorkflowDeps.fault` 钩子（每个节点入口调用一次，生产为 `None`）+ 环境变量 `AIEB_CHAOS_*` |
| 邮件 | 全部 Fake sender / Fake SMTP，**零真实投递** |

## 1. R 组：真实进程崩溃 + 重启恢复

| 场景 | 注入位置 | 预期 | 实际 | DB 终态 | 重复副作用 |
| --- | --- | --- | --- | --- | --- |
| R1 分析后崩溃 | `validate_risks` 前 `os._exit(9)` | 崩溃不落库；重启 `continue` 走完流程 | crash=True → continue → approve=completed | 会议 1 / 发信 1 | 0 |
| R2 落库后崩溃 | `generate_email_draft` 前 | 会议/风险/待办各 1，不因续跑翻倍 | crash=True → completed | 会议 1 / 风险 1 / 待办 1 / 发信 1 | 0 |
| R3 草稿后崩溃 | `human_approval` 前 | 重启后 `continue` 停在等待审批，再 approve 发 1 封 | continue=waiting_approval → approve=completed | 发信 1 | 0 |
| R4 等待审批时进程退出 | 进程正常退出 | **新进程**能拿到 `waiting_approval` + `resumable=True` | 全部满足，approve 成功 | 发信 1 | 0 |
| R5 重启后取消 | 审批期间退出 | 不发信，邮件 `cancelled` | status=cancelled | 发信 0 | 0 |
| R6 已完成后再 resume | 重复 approve（终态） | 不重复发信 | 返回"没有待审批的中断"，状态仍 completed | 发信 1 | 0 |

R1/R2 的关键点：崩溃发生在**任意节点边界**，恢复后 `load_meeting` 命中
`find_meeting_by_text` → 走 `meeting_id` 复用分支 → 跳过重复分析/落库。
R2 的幂等键 `persist_meeting` 状态为 `succeeded`，重放直接复用结果。

## 2. C 组：并发与重复请求

| 场景 | 注入方式 | 预期 | 实际 | 说明 |
| --- | --- | --- | --- | --- |
| C1 连续两次 approve | 第二次 resume（已终态） | 只发 1 封 | completed/completed，sends=1 | 第二次走"无挂起中断 → 返回快照" |
| C2 两个并发 approve | 2 线程同时 resume | 只发 1 封 | 两个都返回 completed，sends=1 | 图在同一 loop 内串行 + 幂等键兜底 |
| C3 approve 与 cancel 竞争 | 2 线程分别 approve/cancel | 终态唯一，发送 ≤1 | 两个请求都返回 completed，sends=1 | 先完成者定终态，后者幂等返回 |
| C4 同一文本重复 start | 第二次 start（allow_reuse） | 复用同一工作流，业务数据只落一次 | 同一 workflow_id，会议 1、run 1 | 按 `text_hash` 复用未完成 run |
| C5 三个不同工作流并发 | 3 线程各自 start+approve | 互不串扰 | 3 会议 / 3 run / 3 封，无异常 | 各自独立 thread_id |

**并发边界（必须写清）**：以上都是**单进程内**并发。本项目**不承诺**多进程/多实例同时操作同一
`workflow_id` 的互斥；`workflow_idempotency` 的 `executing` 状态只在本机 SQLite 内生效。

SQLite 相关观察：C2/C3 没有出现 `database is locked`（写事务很短 + `timeout=10s` + WAL）；
真正的写锁冲突见 F4。

## 3. F 组：故障注入

| 场景 | 注入位置 | 预期 | 实际 | DB 终态 | 重复副作用 |
| --- | --- | --- | --- | --- | --- |
| F1 LLM 连续超时 | analyzer 前 5 次抛 `RuntimeError` | 只尝试 2 次后失败；不落业务数据；每次失败有 retry 记录 | attempts=2、retry_events=2、failed | 会议 0 | 0 |
| F2 LLM 返回非法 JSON | analyzer 抛 `ValueError` | 失败并如实上报，不写业务数据 | failed | 会议 0 | 0 |
| F3 业务落库失败 | `store.add_meeting` 抛 `database is locked` | 幂等键记 `failed`；续跑后成功且只 1 条 | effect=failed → continue=waiting_approval → 会议 1 | 会议 1 | 0 |
| F4 业务库被悲观锁占用 | 另一连接 `BEGIN EXCLUSIVE` | 明确失败（不挂死/不静默成功） | `工作流执行失败：database is locked`，耗时 5.4s | 会议 0 | 0 |
| F5 Checkpoint 库损坏 | checkpoint 文件写成非 sqlite | 启动/首调即报错，不产生业务数据 | `DatabaseError: file is not a database` | 会议 0 | 0 |
| F6 SMTP 抛异常（结果不确定） | sender 抛 `TimeoutError` | 邮件 `uncertain`、工作流 failed、`retry_send` 拒绝 | 全部满足 | 会议 1、邮件 uncertain | 0 |
| F7 SMTP 明确拒绝（550） | sender 返回 `ok=False` | 邮件 failed、幂等键 failed（允许人工 retry） | 满足 | 会议 1、effect=failed | 0 |
| F8 用旧版本审批新草稿 | `version + 1` | 拒绝执行、不发信 | `审批已过期：你确认的是 v2，当前草稿是 v1…` | 发信 0 | 0 |

### 三个值得强调的行为

1. **F1 的"2 次"**：节点内 `with_retry` 用 `attempts=2`（1 次原调用 + 1 次重试），
   **不会**无限重试；每次失败都写一条 `event="retry"` 的 Trace（这也是"重试可观测"的证据）。
   注意生产路径 `build_real_deps(retry_attempts=1)` 是为避免与 `analyze_async` 内部重试叠加
   （见 `docs/LANGGRAPH_FINAL_AUDIT.md` §7/§8.4）。
2. **F6 vs F7 的区别**：这是本项目最核心的业务语义 —— "不知道对方收没收到"记 `uncertain` 且
   **永不自动重试**；"对方明确拒收"记 `failed` 且允许人工 `workflow.retry`。
3. **F4 的 5.4s**：`BrainStore` 用的是 `sqlite3` 默认忙碌超时，写锁被占时会阻塞到超时；
   这是"可预期的失败"，不是死锁（没有重试风暴）。

## 4. 审批版本绑定专项（与故障注入同源）

| 攻击/误操作 | 系统行为 | 证据 |
| --- | --- | --- |
| 用 v1 的 `(version, draft_hash)` 确认 v2 草稿 | `WorkflowError("审批已过期…")`，不发信 | F8、`test_approve_wrong_hash_is_rejected` |
| 审批期间别人改了草稿（绕过工作流） | 恢复时二次读库发现哈希不符 → 拒绝 | `test_draft_edited_by_other_path_invalidates_old_approval` |
| 界面上改了正文但没保存就点确认 | 前端拦截（必须先"保存修改"→ 版本 +1） | E2E `edit_requires_save` |
| 收件人为空点确认 | 前端拦截；后端 `send_email` 也会快速失败（`InvalidRecipient`） | E2E `empty_recipient_blocked`、`test_empty_recipient_fails_fast_without_touching_smtp` |
| 重复点确认两次 | 第二次命中幂等键/无挂起中断 → 返回快照 | C1、C2 |

## 5. 没有测到的（诚实清单）

1. **多进程/多实例**同时操作同一 `workflow_id`（需要分布式锁，本阶段明确不做）；
2. **K8s Pod 驱逐 / 断电** 级别故障（当前用 `os._exit(9)` 模拟进程被强杀）；
3. **高并发吞吐**（没有做 N 并发压测，只有 3 并发功能验证）；
4. **长时间浸泡**（没有跑小时级稳定性测试）；
5. `update_draft` 无幂等键，若两次 edit 并发可能产生两版草稿（邮件仍只有 1 行）—— 已知限制。
