# 归属声明与许可证状态 / Attribution

## 归属（Attribution）

**AI External Brain 在既有通用 Agent 运行时之上扩展实现。**

仓库内的记录：

- `aieb/__init__.py`：`__upstream__ = "MewCode Agent"`、`__derived_from__ = "WenDream"`；
- 品牌与包名迁移对照见 [`MIGRATION_RENAMING.md`](MIGRATION_RENAMING.md)。

职责边界：

- **本项目实现**：`aieb/brain/`（知识库、Hybrid RAG、会议分析、待办、邮件、PPT、产品 UI、业务 API）与
  `aieb/workflow/`（LangGraph 工作流）。
- **沿用既有运行时**：Agent Loop、Tool Calling、Session、Memory、WebSearch/WebFetch、ArtifactStore、WebSocket、
  权限与安全，以及 TUI（`aieb/app.py`）与旧版网页（`aieb/web_content.py`）。这部分**不是本项目从零实现**。
- 迁移只改命名，不改变代码归属。

## 许可证状态

**上游许可证文件未随代码提供（公开发布阻塞项）**

- 本仓库当前与历史中均没有 `LICENSE` / `NOTICE`，`pyproject.toml` 也未声明许可证类型；
- 未声明许可证的代码默认保留全部权利，**不构成再分发授权**；
- 需要确认授权的具体文件（沿用运行时）：
  `aieb/agent.py`、`aieb/client.py`、`aieb/config.py`、`aieb/conversation.py`、`aieb/serialization.py`、
  `aieb/prompts.py`、`aieb/remote.py`、`aieb/artifacts.py`、`aieb/cache.py`、`aieb/app.py`、`aieb/web_content.py`，
  以及 `aieb/tools/`、`aieb/memory/`、`aieb/sessions/`、`aieb/permissions/`、`aieb/hooks/`、`aieb/mcp/`、
  `aieb/skills/`、`aieb/teams/`、`aieb/context/` 等目录。

发布前必须完成以下任一条件：

1. 由版权人在发布仓库中补充明确的 LICENSE；
2. 取得版权人的书面授权，并保留其要求的版权声明与 `NOTICE`；
3. 替换或移除上述运行时模块（当前 `aieb/brain/` 依赖这些模块，无法直接独立运行）。

在完成上述任一项之前，本仓库状态为 **不可公开发布 / 不可再分发**。

## 第三方组件

| 组件 | 用途 | 许可证（以官方发布为准） |
|---|---|---|
| `websockets` | HTTP/WebSocket 服务 | BSD-3-Clause |
| `pydantic` / `pydantic-core` | 结构化输出校验 | MIT |
| `PyMuPDF` (`pymupdf`) | PDF 文本与页码提取 | AGPL-3.0 / 商业双许可（商业分发需注意） |
| `python-pptx` | 生成 `.pptx` | MIT |
| `marked` / `DOMPurify`（CDN） | 前端 Markdown 渲染与消毒 | MIT / Apache-2.0 |
| `anthropic` / `openai` SDK | 模型接入 | MIT / Apache-2.0 |

> 注意：PyMuPDF 采用 AGPL-3.0 或商业许可。若本 Demo 用于商业闭源分发，需确认 PyMuPDF 授权方式，
> 或替换为其它 PDF 解析库。这里如实列出，不回避该约束。
