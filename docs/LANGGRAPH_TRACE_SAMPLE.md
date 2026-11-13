# 一次真实工作流的 Trace 样例（已脱敏）

- 执行模式：`live:deepseek-chat`
- workflow_id：`wf_fb676a7b28b6`　thread_id：`wf_fb676a7b28b6`
- 终态：`completed`　Token 合计：in 581 / out 697
- 最慢节点：`analyze_meeting`
- 失败节点：无
- Fake SMTP 投递成功邮件数：1（未真实发信）

## 节点级事件

| seq | node | event | status | duration_ms | model | in/out | retry | error_type | checkpoint_id |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | load_meeting | node | ok | 1.16 | - | 0/0 | 0 | - | - |
| 2 | analyze_meeting | node | ok | 3004.81 | deepseek-chat | 227/462 | 0 | - | - |
| 3 | extract_todos | node | ok | 0.54 | - | 0/0 | 0 | - | - |
| 4 | validate_risks | node | ok | 1.34 | - | 0/0 | 0 | - | - |
| 5 | merge_results | node | ok | 0.0 | - | 0/0 | 0 | - | - |
| 6 | persist_business_state | node | ok | 12.78 | - | 0/0 | 0 | - | - |
| 7 | generate_email_draft | node | ok | 2053.51 | deepseek-chat | 354/235 | 0 | - | - |
| 8 | human_approval | interrupt | interrupted | 0.29 | - | 0/0 | 0 | - | - |
| 9 | graph | start | ok | 0.0 | - | 0/0 | 0 | - | 1f1dde24-eab |
| 10 | human_approval | node | ok | 0.23 | - | 0/0 | 0 | - | - |
| 11 | send_email | node | ok | 32.21 | - | 0/0 | 0 | - | - |
| 12 | record_audit | node | ok | 4.1 | - | 0/0 | 0 | - | - |
| 13 | graph | resume | ok | 0.0 | - | 0/0 | 0 | - | 1f1dde24-eb8 |

## 幂等键（副作用账本）

| action | status | object_id | version | attempts |
| --- | --- | --- | --- | --- |
| draft_email | succeeded | mtg_c956b12a2fe6 | 1 | 1 |
| persist_meeting | succeeded | 5e1a5b7a55e993f2 | 0 | 1 |
| send_email | succeeded | mail_998334ca8ca5 | 1 | 1 |

## 脱敏说明

- 邮箱地址、API Key 在导出时被替换；会议原文不进入 Trace。
- Trace 只写本地 SQLite（`workflow_events` 表），默认不上传任何外部平台。

字段完整性检查：`通过`；脱敏自检：`通过`
