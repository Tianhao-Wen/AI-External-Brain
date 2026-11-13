# 仓库文件分类（Repository Inventory）

本文件是 **AI External Brain** 发布仓库的文件分类，不是历史项目清单。
沿用的 Agent 运行时与新增业务代码的边界见 [`ATTRIBUTION.md`](ATTRIBUTION.md)。

| 分类 | 含义 | 处理 |
|---|---|---|
| CORE | 产品代码 / 配置模板 | 保留 |
| TEST | 回归测试与浏览器 E2E | 保留 |
| DEV TOOL | 开发/验证辅助脚本 | 保留（`scripts/`） |
| LEGACY | 历史一次性工具与遗留资产 | 保留但不参与主流程 |
| GENERATED | 生成物（日志、缓存、产物） | `.gitignore` 忽略，不入库 |

## CORE

- `aieb/` —— Python 包（产品主体；包名 `aieb`，原 `wendream`）
  - `aieb/brain/` —— 业务层：知识库、会议分析、待办、邮件、PPT、业务 API、产品 UI
  - 其余子包为复用的通用 Agent 能力（Agent Loop / 工具 / 会话 / 记忆 / WebSocket / 权限）
- `tests/` —— 回归测试（含 `tests/js/` 前端脚本检查）
- `pyproject.toml` —— `name = "aieb"`，主命令 `aieb`（另保留 `wendream` 兼容别名）
- `.gitignore`
- `.aieb/config.yaml.example` —— 配置模板（**无密钥**）
- `AI-EXTERNAL-BRAIN.md` —— 项目级 Agent 指令（原 `WENDREAM.md` / `MEWCODE.md`）
- `README.md`、`docs/` —— 交付说明与证据

## TEST

- `tests/test_brain_*.py` —— 业务层与产品 UI 测试
- `tests/test_session_state.py` —— 会话状态/重命名/删除/重连
- `tests/test_branding_version.py` —— 品牌与版本一致性（含归属常量断言）
- `tests/js/*.mjs` —— 前端脚本静态检查（XSS 消毒、UI 冒烟）
- `scripts/browser_e2e.py`、`scripts/e2e_session_state.py` —— 真实浏览器验收

## DEV TOOL

- `scripts/audit_secrets.py` —— 发布前密钥扫描
- `scripts/redact_pii.py`、`scripts/redact_local_secrets.py` —— 文档/截图脱敏
- `scripts/check_llm.py`、`scripts/smoke_test.py` —— 模型连通性与冒烟
- `scripts/verify_email.py`、`scripts/verify_pptx.py` —— 真实发送 / 真实打开校验
- `scripts/make_demo_samples.py`、`scripts/make_sample_pdf.py` —— 演示素材生成
- `scripts/dedupe_memories.py` —— 记忆去重只读报告（`--apply` 才写入）

## LEGACY（保留但不在主流程）

- `aieb/app.py`（TUI）与 `aieb/web_content.py`（旧版网页）—— 仅参与回归测试
- `scripts/legacy/` —— 历史一次性迁移脚本，见该目录 `README.md`

## GENERATED（不入库）

- `.aieb/debug.log`、`.aieb/*.log`、`.aieb/session*/`、`.aieb/memory/`、`.aieb/history`
- `.aieb/brain/`（SQLite 业务库、上传件、导出产物）
- `__pycache__/`、`*.pyc`、`.pytest_cache/`

## 关于前端依赖

Web UI 的前端依赖（marked / highlight.js / DOMPurify）全部通过 CDN 引入，没有构建步骤，
因此仓库内没有 `package.json` / `node_modules/`。

## 关于锁文件

仓库不提交 `uv.lock` / `requirements.lock`。若使用 `uv`，请用 `uv lock` 重新生成。
