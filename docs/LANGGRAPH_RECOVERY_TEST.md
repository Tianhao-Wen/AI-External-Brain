# 断点恢复与人工审批：测试证据

> 全部数据来自**实际执行**，命令与输出可复现。
> 环境：Windows 11 / Python 3.14.2 / langgraph 1.2.20 / `.venv-langgraph`。
> 测试用独立临时 workspace，不读写项目真实的 `.aieb/` 数据；SMTP 使用进程内 Fake SMTP，
> **全程没有真实发信**（`AIEB_SMTP_*` 未配置时 mailer 本身也是 dry-run）。

## 1. 自动化用例清单

```powershell
$env:PYTHONPATH="<repo>"
.venv-langgraph\Scripts\python.exe -m pytest tests/test_workflow_graph.py tests/test_workflow_recovery.py `
    tests/test_workflow_idempotency.py tests/test_workflow_state.py tests/test_workflow_ops.py `
    tests/test_workflow_langchain.py tests/test_workflow_fake_smtp.py tests/test_workflow_retry_policy.py -q
# 65 passed, 1 skipped in 17.14s
```

> 最新一轮验收（含真实 LLM、故障注入、浏览器 E2E）见
> `docs/LANGGRAPH_FAULT_INJECTION_REPORT.md` 与 `docs/LANGGRAPH_DEPLOYMENT_VALIDATION.md`；
> 本文保留的是单元/集成层的原始证据。

| 文件 | 用例数 | 覆盖 |
| --- | --- | --- |
| `test_workflow_graph.py` | 12 | 真实 StateGraph、13 节点、条件/并行/fan-in、interrupt≠失败、取消不发信 |
| `test_workflow_recovery.py` | 10 | 恢复场景 A–I（见下） |
| `test_workflow_idempotency.py` | 9 | 重复点击、重复 RPC、SMTP 超时、不确定态 |
| `test_workflow_state.py` | 9 | reducer、并行写标量的冲突、schema 版本 |
| `test_workflow_ops.py` | 7 | RPC 契约 `workflow.*` → 后端真实状态 |
| `test_workflow_langchain.py` | 7 + 1(skip) | ChatModel/Tool/StructuredOutput（含 1 条真实 LLM 用例，需显式开启） |
| `test_workflow_fake_smtp.py` | 3 | Fake SMTP 全链路：真走 SMTP 协议、真投递 |

## 2. 恢复场景 A–I

| 场景 | 做法 | 断言（已通过） |
| --- | --- | --- |
| **A** 分析完成后中断 | 注入"草稿节点第一次失败"，落库那步已完成 | 续跑后 `waiting_approval`；会议仍只有 1 条；`persist_meeting` 幂等键为 `succeeded`（未重复落库） |
| **B** 草稿生成后中断 | 丢弃进程内 Runner，重开读回 checkpoint | 新 Runner 仍能拿到同一 `email_draft_id` 与版本 |
| **C** 审批期间关闭浏览器 | 前端不留任何内存，新建 Runner | 仍拿到同一个 `ApprovalRequest`（id/version/draft_hash 一致） |
| **D** 审批期间重启后端 | 重新打开 checkpoint 库后 resume | 只发出 1 封 |
| **E** 重启后继续原工作流 | 同 D | 最终业务结果正确（会议/风险/待办各 1） |
| **F** 重复恢复请求 | 并发两次 resume | 只发出 1 封（`executing` 态拦住第二次） |
| **G** 取消后再恢复 | 先 cancel 再 resume | 一封都不发 |
| **H** 失败节点重新执行 | 分析节点连续失败 5 次 | run=`failed` 且事件留痕；`analysis_calls==2`（不无限重试）；续跑后成功且不多写业务数据 |
| **I** 瞬时失败自动重试 | 分析节点只失败 1 次 | 同一次运行内重试成功；`analysis_calls==2`；Trace 里有 `event="retry", retry=1, error_type="RuntimeError"` |

## 3. 真实浏览器 E2E

脚本：`scripts/e2e_workflow_browser.py`（真实 Chrome + 进程内 Fake SMTP + 真实后端进程）。

```powershell
.venv-langgraph\Scripts\python.exe scripts\e2e_workflow_browser.py
```

结果：**21/21 全通过**（`all_passed=true`），原始记录
`benchmarks/results/hybrid_rag/e2e_workflow.json`，截图在
`benchmarks/results/hybrid_rag/e2e_shots/workflow_01..06_*.png`
（审批卡片 / 发送完成 / 执行 Trace / 重启后 / 已取消 / 改稿升版）。
完整的 21 项检查表见 `docs/LANGGRAPH_DEPLOYMENT_VALIDATION.md` §1。

| 检查项 | 含义 | 结果 |
| --- | --- | --- |
| `approval_card_shown` | 页面出现审批卡片，不在前端假装完成 | ✅ |
| `shows_real_status` | 卡片显示后端真实状态与 `workflow_id/meeting_id` | ✅ |
| `draft_has_subject` | 草稿已有主题/正文（不是空壳审批） | ✅ |
| `survives_page_reload` | **刷新页面**后审批卡片仍在（状态在后端，不在前端内存） | ✅ |
| `completed_after_approve` | 点"确认发送"后状态变 `completed` | ✅ |
| `trace_visible` | "查看执行 Trace"能显示节点级轨迹 | ✅ |
| `restart_ok` | **杀掉后端进程再启动**，工作流仍可访问 | ✅ |
| `meeting_single_after_restart` | 重启后会议仍只有 1 条 | ✅ |
| `cancel_shows_cancelled` | 取消后显示 `cancelled` | ✅ |
| `no_duplicate_meetings` | 全流程结束后会议总数不增加 | ✅ |
| `exactly_one_mail_delivered` | Fake SMTP 只收到 1 封 | ✅ |
| `first_email_sent` | 第一封是 `sent` 状态 | ✅ |
| `second_email_cancelled` | 第二次流程取消，邮件状态 `cancelled` 且未投递 | ✅ |

关键证据片段（JSON 截取）：

```json
"checks": {
  "approval_card_shown": true, "shows_real_status": true, "draft_has_subject": true,
  "survives_page_reload": true, "completed_after_approve": true, "trace_visible": true,
  "restart_ok": true, "meeting_single_after_restart": true, "cancel_shows_cancelled": true,
  "no_duplicate_meetings": true, "exactly_one_mail_delivered": true,
  "first_email_sent": true, "second_email_cancelled": true, "all_passed": true
}
```

页面实际文案（`workflow_waiting_approval` 步骤抓取）：

```
会议→邮件工作流
wf_3dc725210b4c · 会议 mtg_48593ce9b6c1 · 下一步：等待人工审批
等待人工审批
需要你确认后才会发送邮件（草稿 v1，确认前不会发出）。
收件人 / 主题 / 正文
确认发送 / 保存修改（需再次确认） / 取消发送 / 查看执行 Trace
审批绑定到具体草稿 版本：修改后版本号会 +1，旧版本确认会被拒绝。
```

## 4. 审批版本绑定（单独验证）

`tests/test_workflow_recovery.py` + `test_workflow_idempotency.py` 中的强断言：

| 攻击/误操作 | 系统行为 |
| --- | --- |
| 拿 v1 的 `(version, draft_hash)` 去确认 v2 草稿 | `WorkflowError("审批已过期…")`，不发信 |
| 审批期间另一个人改了草稿正文 | 恢复时二次读库发现哈希不符 → 拒绝，不发信 |
| 用户在 UI 改了正文但没保存就点确认 | 前端拒绝（必须先"保存修改"，版本 +1 后重新审批） |
| 重复点"确认发送"两次 | 第二次命中 `succeeded` 幂等键 → 直接返回，不重复发 |

## 5. Fake SMTP 证据（为什么需要一个假 SMTP）

项目原有 `mailer.send_email` 在未配置 SMTP 时是 dry-run，且**刻意返回
`ok=False, error_code="send_blocked"`**（不假装成功）。所以"发送成功"这条分支
在纯 dry-run 下永远走不到。为验证真实链路（真 SMTP 协议 + 真投递 + 状态回写），
`tests/fake_smtp.py` 起了一个线程化的 `socketserver` SMTP 服务
（EHLO / AUTH LOGIN / MAIL / RCPT / DATA / QUIT），把 `smtp.env()` 注入被测进程环境变量。

`tests/test_workflow_fake_smtp.py` 3 条用例：真投递 1 封、取消不发、
发送失败时邮件状态为 `failed` 而非 `completed`。

## 6. 已知限制（诚实记录）

1. **不是跨系统 Exactly-Once**：SMTP 无事务确认，"send 返回后、写库前被杀"窗口靠
   `uncertain` + 人工核实兜底。
2. **框架 `RetryPolicy` 在 1.2.20 未生效**，重试由节点内实现（见 `LANGGRAPH_WORKFLOW.md` §7）。
3. **Runner 绑定事件循环线程**，不支持多线程随意共享。
4. E2E 的"重启后端"是杀进程重启（`subprocess`），不是在容器/K8s 里做 Pod 调度验证。
