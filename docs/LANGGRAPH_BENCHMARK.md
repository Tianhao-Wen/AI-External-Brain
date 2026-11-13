# 原生流程 vs LangGraph：40 场景对照实验

> 数据来源：`python -m benchmarks.workflow.runner`（本次实际执行输出）
> 原始记录：`benchmarks/results/workflow/workflow_bench_fixture.json`
> 环境：Windows 11 / Python 3.14.2 / `.venv-langgraph` / langgraph 1.2.20
> **不包含任何估算或推测数据**；不利指标（延迟高一个数量级）原样记录。

## 1. 实验设计

| 维度 | 做法 |
| --- | --- |
| 场景 | 40 个：14 个业务/审批/故障主场景 + 26 个变体（多待办、长文本、缺截止时间、单风险、无待办） |
| 输入 | 同一批会议文本；两边**完全相同** |
| 组件 | 分析/草稿/发送都用同一套 fake（`tests/workflow_harness.py`），只有编排层不同 |
| 业务规则 | 两边都复用 `meetings.sanitize` / `store.add_meeting` / 相同的邮件状态字段 |
| 评分 | 终态 + 业务行数 + 副作用次数；不看代码风格 |
| 发送 | Fake Sender / 进程内 Fake SMTP，**没有真实发信** |
| 重复执行 | 每个场景使用独立 workspace，跑前清空 |

**为什么用确定性 fake 而不是真实 LLM**：本实验比的是**编排能力**（状态、检查点、重试、
幂等、恢复），不是模型质量。真实 LLM 的非确定性会让"风险条数"本身变成噪声，
无法归因。真实 LLM 的联通性单独由 `tests/test_workflow_langchain.py` 的显式用例验证
（需 `AIEB_TEST_LIVE_LLM=1`，断言 `input_tokens > 0`）。

**期望值怎么来的**（避免"用实测结果反推期望"）：
- 风险/待办条数 = `meetings.sanitize(analysis, text)` 的输出（业务规则决定的唯一真相）；
- 发送次数 = 由审批决策决定（approve/edit → 1，reject/none → 0）；
- 分析阶段**永久失败**时 → 期望 0 会议、0 风险、0 待办、0 发送（规格要求不落半成品数据）。

**能力差异不计入成功率**：改造前的流程没有 checkpoint，也没有"暂停等人工"这项能力，
S08 对原生流程标记为 `capability_gap` 并从分母剔除（39 而非 40），单独报告。

## 2. 汇总结果（40 场景，实测）

| 指标 | 原生流程 | LangGraph |
| --- | --- | --- |
| 可评分场景数 | 39（S08 为能力差异） | 40 |
| **任务成功率** | **0.9744**（38/39） | **1.0**（40/40） |
| P50 延迟 | 14.9 ms | 174.2 ms |
| P95 延迟 | 16.1 ms | 197.6 ms |
| LLM 调用次数 | 78 | 81 |
| 工具/发送次数 | 34 | 35 |
| **重复副作用次数** | 0 | 0 |
| 恢复成功率 | 不支持（无 Checkpoint） | 1.0（1/1） |
| 检查点恢复耗时 | — | 1.3–2.1 ms |

> **复跑波动**：本轮（2026-11-11，代码改动后）重跑同一批场景，得到
> 原生 P50 14.4 ms / P95 15.7 ms / LLM 78 次，LangGraph P50 166.7 ms / P95 192.8 ms / LLM 81 次，
> 成功率、副作用、恢复率与上表**完全一致**。延迟差异属于运行波动（±5%），不影响任何结论。
> 最终以 `benchmarks/results/workflow/workflow_bench_fixture.json` 为准。

### 怎么读这些数字（包括对 LangGraph 不利的部分）

1. **延迟高约 11.7 倍（P50 14.9 → 174.2 ms）**。这是真实代价：每个 superstep 都要
   序列化状态并写 SQLite checkpoint，加上 Runner 的跨线程事件循环调度。
   本次实验的 fake 分析只需几十微秒，所以**固定开销被放大**；真实 LLM 调用是秒级，
   checkpoint 开销占比会显著下降 —— 但本实验没有测真实 LLM 的延迟，因此这里不给
   "真实场景只增加 x%" 这类没有依据的换算。
2. **LLM 调用多 3 次（78 → 81）**。来源可逐条解释：S11 重试 1 次 + S11 恢复后多跑
   1 次草稿 + S12 重试 1 次。这是"用 3 次 LLM 调用换 1 个场景的成功"，不是浪费。
3. **发送多 1 次（34 → 35）**：S11 在 LangGraph 下恢复完成并成功发信，原生直接失败。
4. **重复副作用两边都是 0**：原生流程靠 `status == "sent"` 判断兜底，在本批场景下
   没有触发重复发送。所以**不能**声称"原生一定会重复发送" —— 只能说它在这批场景里
   恰好没重复，而它的保护是单点状态判断，缺少 `executing/uncertain` 语义。
5. **成功率 0.9744 → 1.0 的差异只有 1 个场景（S11）**：诚实地说，这不是"大幅提升成功率"，
   而是在**故障注入场景**上补齐了重试能力；真正的结构性差异在"恢复能力"一列
   （原生不支持，LangGraph 支持）。

## 3. 关键场景明细（实测）

| 场景 | 原生流程 | LangGraph |
| --- | --- | --- |
| **S02** 多风险正常处理 | completed，1 封，风险/待办按业务规则落库 | 同左 |
| **S08** 停在等待审批 | `failed`（无中断能力，只能整体失败）→ 标记为能力差异 | `waiting_approval`，未发信，状态可被前端查询 |
| **S10** 审批前重启服务 | completed（本次重跑恰好没受影响，但它没有检查点，无法证明"没有重复执行"） | completed，**从 checkpoint 恢复**，恢复耗时 1.3 ms，会议仍 1 条 |
| **S11** 分析瞬时失败 | `failed`（无重试，0 封） | **completed**（重试 1 次成功，1 封） |
| **S12** 分析持续失败 | `failed`（0 落库，0 封） | `failed`（2 次尝试后面失败，0 落库，0 封）—— 不假装成功 |
| **S13** SMTP 明确拒绝 | `failed`（无重试入口，用户只能重跑整条流程） | `failed` + 幂等键 `failed`，可用 `workflow.retry` 只重发不重算 |
| **S14** SMTP 超时（不确定） | `failed`（与 S13 无法区分） | `failed` + 邮件标记 `uncertain`，**禁止自动重试**，提示人工核实 |

S13/S14 的差别是本项目最核心的业务价值：
"对方明确拒收"和"不知道对方收没收到"在原生流程里长得一模一样，
而 LangGraph 版本把后者单独建模成 `uncertain`，避免自动重试造成重复打扰。

## 4. 复现方式

```powershell
cd <repo>
$env:PYTHONPATH="<repo>"
.venv-langgraph\Scripts\python.exe -m benchmarks.workflow.runner            # 40 场景
.venv-langgraph\Scripts\python.exe -m benchmarks.workflow.runner --limit 5  # 只跑前 5 个
```

工作目录：`benchmarks/.work/wf_bench/<backend>/<scenario_id>/`（已 gitignore，跑前自动清空）。
每次运行会覆盖 `benchmarks/results/workflow/workflow_bench_fixture.json`。

## 5. 局限（不要过度解读）

1. **fake 依赖**：本实验不测真实 LLM 的质量/延迟，只测编排行为。
2. **场景规模 40**：足以覆盖分支与故障类型，但不是统计意义上的大规模压测。
3. **并发**：`duplicate_confirm` 场景测的是同一工作流的重复提交，不是多工作流高并发吞吐。
4. **原生流程的"公平性"**：原生版本没有重试、没有 HITL 暂停，这是**改造前的真实状态**，
   不是刻意阉割；S08 已按能力差异剔除，其余场景按同一标准评分。
