# 部署与浏览器验收

> 浏览器 E2E 原始记录：`benchmarks/results/hybrid_rag/e2e_workflow.json`（**21 项检查全部 true**）
> 截图：`benchmarks/results/hybrid_rag/e2e_shots/workflow_01..06_*.png`
> 复现：`python scripts/e2e_workflow_browser.py`（真实 Chrome + 真实后端进程 + Fake SMTP）

## 1. 浏览器 E2E：21 项检查（真实 LLM）

| # | 检查项 | 验证内容 | 结果 |
| --- | --- | --- | --- |
| 1 | `approval_card_shown` | 启动工作流后出现审批卡片，且明确"确认前不会发出" | ✅ |
| 2 | `shows_real_status` | 卡片显示后端真实状态与 `workflow_id/meeting_id` | ✅ |
| 3 | `draft_has_subject` | 草稿已生成主题（不是空壳审批） | ✅ |
| 4 | `risks_and_todos_visible` | 会议列表展示"风险/待办"（业务落库已生效） | ✅ |
| 5 | `survives_page_reload` | **刷新浏览器**后审批卡片仍在（状态在后端） | ✅ |
| 6 | `completed_after_approve` | 点"确认发送"→ completed | ✅ |
| 7 | `trace_visible` | "查看执行 Trace"显示节点级轨迹 | ✅ |
| 8 | `restart_ok` | **杀进程重启后端**后工作流仍可访问 | ✅ |
| 9 | `meeting_single_after_restart` | 重启后会议仍只有 1 条 | ✅ |
| 10 | `cancel_shows_cancelled` | 取消后显示 `已取消` | ✅ |
| 11 | `chat_page_ok` | 原有 AI 对话页仍可用（渲染 + 输入控件存在） | ✅ |
| 12 | `knowledge_page_ok` | 原有知识库页仍可用 | ✅ |
| 13 | `empty_recipient_blocked` | 清空收件人点确认 → 仍在等待审批（没有发出去） | ✅ |
| 14 | `edit_requires_save` | 改正文后直接点确认 → 被拦下，仍在等待审批 | ✅ |
| 15 | `edit_bumps_version` | 点"保存修改"后卡片显示 `草稿 v2` | ✅ |
| 16 | `completed_after_reapprove` | 对 v2 再确认 → completed | ✅ |
| 17 | `no_duplicate_meetings` | 3 份不同纪要 → 恰好 3 条会议（无重复创建） | ✅ |
| 18 | `mail_count_matches_approvals` | 2 次批准 → Fake SMTP 恰好 2 封 | ✅ |
| 19 | `first_email_sent` | 第一封状态 `sent` | ✅ |
| 20 | `second_email_cancelled` | 第二封状态 `cancelled` 且未投递 | ✅ |
| 21 | `edited_email_version_two` | 改稿的那封邮件版本为 2（审批绑定生效） | ✅ |

> ⚠️ 检查项在运行时**预先全部置为 False**。早期版本用 `all(checks.values())`，
> 某阶段崩溃导致检查项缺失时会被当成"通过" —— 这是一个被本轮修掉的验收漏洞。

### 关于"上传会议 TXT"

工作流入口有两条等价路径，E2E 覆盖了文本粘贴路径（`#mtgText`）：
1. `workflow.start` 传 `text`（E2E 使用，稳定且可断言）；
2. `workflow.start` 传 base64 `data`（文件上传），服务端走 `knowledge.extract_document_text`
   解析后进入**同一条**状态图（`aieb/brain/ops.py:op_workflow_start`，含 15MB / 40 万字上限校验）。

浏览器里"选择文件"按钮的上传交互未纳入本轮 E2E 断言（`test_capability_ui` 覆盖了前端控件的存在性），
这是**未完成项**，下文 §6 明确列出。

## 2. 部署形态与路径

```
<workspace>/
  .aieb/
    brain/brain.sqlite3                   # 业务数据（会议/风险/待办/邮件/activities）
    workflow/workflow.sqlite3             # 编排元数据：runs / events(Trace) / idempotency
    workflow/checkpoints.sqlite3          # LangGraph Checkpoint（框架格式）
    workflow/inbox/<document_id>.txt      # 会议原文暂存（分析完成后删除）
```

- 三份库**刻意分离**：删掉 `workflow/checkpoints.sqlite3` 不会丢业务数据（只是丢失断点能力）；
  删掉 `workflow.sqlite3` 也不会丢业务数据（幂等账本丢失后靠 `find_meeting_by_text` 兜底）。
- E2E / Benchmark / 测试一律使用 `benchmarks/.work/**` 下的独立 workspace，
  **不在正式 `.aieb/` 上跑任何验收**。

## 3. 安装与启动

```powershell
# 1) 主依赖（不含 LangGraph）
pip install -r requirements.txt
pip install -e .

# 2) 可选：有状态工作流（LangGraph）
python -m venv .venv-langgraph --system-site-packages
.venv-langgraph\Scripts\python.exe -m pip install -r requirements-langgraph.txt

# 3) 启动浏览器工作台
.venv-langgraph\Scripts\python.exe -m aieb.brain.cli            # 默认 127.0.0.1:8000
# 或：pip install -e . 之后直接 aieb
```

| 环境变量 | 作用 | 备注 |
| --- | --- | --- |
| `DEEPSEEK_API_KEY` | 真实模型调用（工作流分析/草稿） | 缺失时工作流无法运行，其它功能不受影响 |
| `AIEB_WORKFLOW_LLM=langchain` | 分析改走 LangChain 适配层 | 默认关闭，走项目原生 `complete_json` |
| `AIEB_SMTP_HOST/PORT/USER/PASSWORD/FROM/SECURITY` | SMTP 配置 | 未配置时发送返回 `not_configured`，**不假装成功** |
| `AIEB_EMAIL_DRY_RUN=1` | 发送保护模式 | 返回 `send_blocked`（明确失败），用于演示/CI |

### 可选依赖隔离（已实测）

- 未安装 langgraph 时：`aieb.workflow` 导入给出可操作提示；`BrainService.workflow` 才会 import；
  相关测试 `pytest.importorskip("langgraph")` 自动 skip。
- 基础环境全量测试：**1429 passed / 2 failed（既有）/ 9 skipped**。

## 4. 性能与容量（真实测量，非估算）

同一条图、同一批确定性 fake，只更换 Checkpointer（`benchmarks/results/workflow/perf_breakdown.json`）：

| 指标 | InMemorySaver | AsyncSqliteSaver |
| --- | --- | --- |
| 单次运行（start → interrupt，7 superstep）中位数 | 54.05 ms | **101.70 ms** |
| 节点耗时合计 | 24.60 ms | 25.80 ms |
| 差值（图调度 + checkpoint + Trace 写入） | 29.45 ms | 75.90 ms |

- **SQLite Checkpoint 额外开销 ≈ 47.65 ms/次**（中位数差）；
- 单次 `append_event` **2.68 ms** × 约 13 条事件 ≈ 35 ms —— 目前最大的可优化项（每事件一次 commit）；
- 单次 `add_meeting` 3.44 ms；单次 `aget_state`（读 checkpoint）0.89 ms。

真实 LLM 场景（25 场，`live_benchmark.json`）：

| 项 | 值 |
| --- | --- |
| 端到端合计 | 200,566 ms（25 场） |
| 其中 LLM 节点（analyze + draft） | 194,196 ms（**96.8%**） |
| 到首次中断的自动执行 | 197,736 ms（非 LLM 开销 3,541 ms ≈ **1.8%**） |
| 审批往返（控制器 resume 调用） | 2,829 ms |
| 人工等待时间 | **未计入**任何延迟指标 |

结论：在真实 LLM 场景下，LangGraph 的编排/持久化开销约占自动执行时间的 1.8%；
fixture 场景下延迟高 11.7 倍是因为 LLM 只需几十微秒，固定开销被放大。
**两种口径不能互相代替**（详见 `docs/LANGGRAPH_LIVE_BENCHMARK.md` §6）。

## 5. 优雅关闭与重启

- `BrainService.close()`：先关 `WorkflowRunner`（停事件循环线程 + 关 aiosqlite 连接），
  再关 `WorkflowStore`，最后关业务库。`WorkflowRunner.close()` 是幂等的（重复调用不再提交协程）。
- 重启后不需要任何"恢复脚本"：`workflow.status` 直接从 checkpoint 读出 `waiting_approval`。
  E2E 第 8/9 项与故障注入 R4（两个独立进程）都验证了这一点。
- 早期 `close()` 在 loop 已停时仍提交协程，导致退出阻塞 900s（测试套件从 12.7s 涨到 902s），已修复。

## 6. 未完成项（明确标注）

| 项 | 状态 | 原因 |
| --- | --- | --- |
| 浏览器"选择文件上传 TXT"交互断言 | ❌ 未纳入 E2E | 本轮用文本入口覆盖同一状态图；上传解析由 `knowledge.extract_document_text` 的既有测试覆盖 |
| Docker 部署 | ❌ 未验证 | 仓库目前无 Dockerfile；本轮只验证 Windows + venv 启动 |
| 多实例/水平扩展 | ❌ 未验证 | 明确不在范围内（见审计 §11） |
| 配置迁移（旧 workspace 升级） | ⚠️ 部分 | 业务库走 `_ADDED_COLUMNS` ALTER 迁移，`emails.version/draft_hash/workflow_id` 自动补齐；编排库三张表 `CREATE TABLE IF NOT EXISTS` 自动建表。**没有**为"旧版本 checkpoint 升级到新图结构"做迁移（LangGraph 会因图名/结构不符重新开始） |
| 无鉴权 | ⚠️ 已知限制 | 只能声明"单用户/受控环境"的安全边界 |

## 7. 版本兼容矩阵（实测）

| 组件 | 版本 | 备注 |
| --- | --- | --- |
| Python | 3.14.2 | 项目 `requires-python >= 3.11`；本轮只在 3.14 上实测 |
| langgraph | 1.2.20 | `AsyncSqliteSaver` + `interrupt/Command` 行为已核实 |
| langgraph-checkpoint | 4.3.1 | 由 langgraph 传递依赖 |
| langgraph-checkpoint-sqlite | 3.1.3 | `SqliteSaver` 不支持异步节点，**必须**用 `AsyncSqliteSaver` |
| langchain-openai | 1.7.4 | 仅适配层使用 |
| aiosqlite | 0.22.1 | Checkpoint 异步连接 |
