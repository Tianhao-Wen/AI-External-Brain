# 真实浏览器 E2E 报告（Playwright + 本机 Chrome）

- 时间：2026-11-08 16:17:23
- 地址：http://127.0.0.1:18889
- 截图目录：`docs/screenshots/`

| 步骤 | 结果 | 证据 |
|---|---|---|
| 工作台 · 品牌 | PASS | AI 外脑 Demo `docs/screenshots/01-dashboard.png` |
| 工作台 · 六个导航入口 | PASS | 6 — |
| 工作台 · 统计来自后端 | PASS | 已上传知识文档 11 已分析会议 8 待完成任务 35 未解决风险 29 — |
| 工作台 · 快捷卡片 4 张 | PASS | 4 — |
| 知识库 · PDF 上传并解析 | PASS | 产品需求说明书.pdf 5.9 KB · 3 页 · 今天 16:16 已解析 `docs/screenshots/02-knowledge.png` |
| 知识库 · 基于 PDF 回答并给出页码 | PASS | 验收条款 C 要求：回答必须给出文档名称与页码引用，无法检索时必须明确说明，不得编造内容。[来源：产品需求说明书.pdf · 第2页]  引用来源 产品需求说明书.pdf · 第2 页 2. 验收标准 验收条款 A:知识库单次检索(含答案生 `docs/screenshots/03-knowledge-answer.png` |
| 会议中心 · 风险与待办结构化展示 | PASS | 核心供应商 A 公司年度评审 / 风险 3 / 待办 3 `docs/screenshots/04-meeting.png` |
| 会议中心 · 风险保留原文依据 | PASS | 原文依据：单一供应商占比 62%，且交付周期可能延长，存在断供风险。 — |
| 会议中心 · 生成邮件草稿（未发送） | PASS | 主题：会议提醒：核心供应商 A 公司年度评审（2025年10月28日 9:30） `docs/screenshots/05-email-draft.png` |
| 会议中心 · 草稿确认页取消发送（未投递） | PASS | 未点击「确认发送」，避免自动化验收误发真实邮件；需要验证真实投递请用 --smtp-configured --email-to <地址> `docs/screenshots/06-email-result.png` |
| 会议中心 · 缺失信息标记待确认 | PASS | 包含『待确认』 `docs/screenshots/07-meeting-missing.png` |
| 待办 · 列表展示 | PASS | 38 条 `docs/screenshots/08-todos.png` |
| 待办 · 状态更新并持久化 | PASS | 状态列显示已完成 — |
| 待办 · 刷新后状态仍为已完成 | PASS | 刷新后仍显示已完成 `docs/screenshots/09-todos-after-reload.png` |
| 长期记忆 · 条目来自真实会议 | PASS | 163 条 `docs/screenshots/10-memory.png` |
| 长期记忆 · 搜索过滤生效 | PASS | 命中 44 条 — |
| 长期记忆 · 删除需二次确认 | PASS | 确认弹窗已出现 `docs/screenshots/11-memory-confirm.png` |
| AI 对话 · 显示流式输出中 | PASS | 停止按钮已出现 — |
| AI 对话 · 得到真实模型回答 | PASS | AI  我是你的企业 AI 外脑：能把知识库文档、会议记录和最新资讯整理成带引用、可执行的结构化结论，包括会议决策与待办、风险预警和汇报材料。 `docs/screenshots/12-chat.png` |
| AI 对话 · 不暴露工具英文名/JSON | PASS |  — |
| 长期记忆 · 新建对话可召回上次会议 | PASS | AI ✓ 正在处理完成  我先从长期记忆中检索这次供应商评审会的记录。最近一次供应商评审会：A 类供应商华瑞精密（A-07）年度评审与质量风险专题会  项目	内容 时间	2025 年 10 月 28 日 09:30—11:20 地点	总部  `docs/screenshots/12b-memory-recall.png` |
| 联网搜索 · 回答含真实链接 | PASS | https://note.com/hirokimiyano/n/n5b5868beee23 `docs/screenshots/13-web-search.png` |
| AI 对话 · 刷新后历史恢复 | PASS | 当前会话历史已重放 `docs/screenshots/14-chat-restored.png` |
| AI 对话 · 刷新后会话列表仍可见 | PASS | 14 个历史会话 — |
| PPT · 生成并出现下载卡片 | PASS | /file?id=art_9c353904f8d2&exp=1794129437&sig=e3eec8bd984d7dd191bfe15d37a5bc2f `docs/screenshots/15-ppt-card.png` |
| PPT · 真实下载且为有效 pptx | PASS | 供应商风险复盘汇报_20261108_161717.pptx / 40108 字节 — |
| 异常处理 · 不支持的文件给出可读提示 | PASS | PPT 已生成：供应商风险复盘汇报_20261108_161717.pptx 不支持 .png 文件，这里只收 PDF `docs/screenshots/16-upload-error.png` |
| 异常处理 · 失败文件不进入已解析列表 | PASS | 匹配到 0 条 not_supported 记录 — |
| 响应式 · 390px 侧栏转横向导航 | PASS | flex-direction=row `docs/screenshots/20-mobile-dashboard.png` |
| 响应式 · 无横向溢出 | PASS | horizontal_overflow=False — |
| 响应式 · 移动端对话页可用 | PASS |  `docs/screenshots/21-mobile-chat.png` |
| 工作台 · 跑完流程后统计非零（README 截图） | PASS | 已上传知识文档 12 已分析会议 8 待完成任务 34 未解决风险 29 `docs/screenshots/01-dashboard-populated.png` |
| 无 JS 运行时异常 | PASS |  — |

**共 33 步，通过 33，失败 0。**
