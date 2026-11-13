# 命名迁移清单：WenDream → AI 外脑 Demo（AIEB）

> 本文档记录 2026-11 这次品牌与内部命名统一的**完整对照表**、兼容策略和已知残留。
> 迁移原则：先审计、再分类处理；能兼容的做兼容，不能兼容的写清楚；归属署名不删。

---

## 1. 迁移前后对照表

| 类别 | 迁移前 | 迁移后 | 兼容策略 |
|---|---|---|---|
| 产品中文名 | —（曾混用 WenDream） | **AI 外脑 Demo** | 全站统一 |
| 产品英文名 | WenDream | **AI External Brain Demo** | 全站统一 |
| Python 包目录 | `wendream/` | **`aieb/`** | 不保留旧目录（会造成双份代码）；旧命令由 CLI 别名兜底 |
| import 路径 | `from wendream.x import y` | **`from aieb.x import y`** | 一次性全量替换，共 1038 处 |
| 分发名（pyproject） | `aieb` | `aieb`（不变） | — |
| 主命令 | `wendream` / `python -m wendream.brain.cli` | **`aieb`** / `python -m aieb.brain.cli` | `pyproject` 保留 `wendream` 命中 `aieb.__main__:main` 的兼容别名 |
| TUI 主类 | `WenDreamApp` | **`AiebApp`** | 无外部依赖方，直接改名 |
| TUI 标题 / 横幅 | `WenDream` | **`AI External Brain Demo`** / `AI 外脑 Demo v...` | — |
| 主题名 | `wendream` | **`aieb`** | — |
| WebSocket 子协议 | `wendream.v1` | **`aieb.v1`** | 服务端**同时接受** `wendream.v1`（旧页面仍可握手） |
| WS token 子协议前缀 | `wendream-token.` | **`aieb-token.`** | 服务端同时接受旧前缀 |
| 环境变量前缀 | `WENDREAM_*` | **`AIEB_*`** | `aieb/env_compat.py` 只读映射：新名优先，旧名回退，**不修改环境** |
| 远程 token 变量 | `WENDREAM_REMOTE_TOKEN` | **`AIEB_REMOTE_TOKEN`** | 旧名仍可读 |
| 记忆目录覆盖变量 | `WENDREAM_REMOTE_MEMORY_DIR` | **`AIEB_REMOTE_MEMORY_DIR`** | 旧名仍可读 |
| 懒加载开关 | `WENDREAM_LAZY_EXPLORE_TOOLS` | **`AIEB_LAZY_EXPLORE_TOOLS`** | 旧名仍可读 |
| 团队子进程变量 | `WENDREAM_TEAM_NAME` 等 | **`AIEB_TEAM_NAME` 等** | 子进程用同一份代码，无需双写 |
| 项目指令文件 | `WENDREAM.md` | **`AI-EXTERNAL-BRAIN.md`** | 旧名仍会被读取（新名同目录优先） |
| 项目指令本地覆盖 | `WENDREAM.local.md` | **`AI-EXTERNAL-BRAIN.local.md`** | 旧名仍会被读取 |
| 运行时数据目录 | `.wendream/`（更早为 `.mewcode/`） | **`.aieb/`** | 保留不动，`aieb.migration` 只做兜底迁移 |
| 模型目录 provider 常量 | `PROVIDER_WENDREAM = "wendream"` | **`PROVIDER_AIEB = "aieb"`** | 纯内部常量，不进配置文件 |
| 内置能力展示名 | `WenDream 联网搜索` / `WenDream 网页读取` | **`AI 外脑联网搜索`** / **`AI 外脑网页读取`** | 纯展示 |
| 记忆召回提示词 | `...useful to WenDream...` | `...useful to the assistant...` | 提示词语义不变 |
| 旧版网页 localStorage 键 | `wendream_tts_rate` 等 | **`aieb_tts_rate`** 等 | 未做旧键迁移（仅影响朗读语速/侧栏折叠等视觉偏好，不含业务数据） |
| 测试用真实 API 变量 | `WENDREAM_TEST_API_KEY` | **`AIEB_TEST_API_KEY`** | 变量未设置时该测试自动 skip |

---

## 2. 必须保留的历史引用（不是漏改）

以下位置**故意**保留 `WenDream` 字样，删掉会失真或丢失合法署名：

| 位置 | 含义 |
|---|---|
| `docs/ATTRIBUTION.md` | 归属链：MewCode Agent（开源上游）→ WenDream（通用 Agent 实现）→ AI 外脑 Demo |
| `aieb/__init__.py` 的 `__upstream__` / `__derived_from__` | 机器可读的归属常量，被 `tests/test_branding_version.py` 断言保护 |
| `aieb/env_compat.py` | 旧环境变量前缀常量 |
| `aieb/migration.py` | 历史目录名 / 历史指令文件名的迁移常量 |
| `aieb/memory/instructions.py` | 旧指令文件名的兼容读取常量 |
| `aieb/remote.py` | 旧 WS 子协议兼容常量 |
| `pyproject.toml` 的 `wendream` 命令别名 | 命令行向后兼容 |
| `scripts/legacy/*` | 历史一次性脚本，见该目录 `README.md` |
| `docs/` 下的历史运行日志（`pytest_*.txt`、`*_raw.txt`） | 过去真实运行的记录，不作改写 |

---

## 3. 对现有数据的影响

**结论：`.aieb/` 下的 Session、Memory、知识库、会议记录、邮件草稿与产物全部原样保留。**

- 包重命名只改代码路径，不改 `.aieb/brain/brain.sqlite3` 的结构与位置；
- 会话/记忆的 JSON 里如果记录了模块路径字符串，仅用于展示，不影响读取；
- 前端 `sessionStorage`/`localStorage` 中的旧键会被当作未命中而重新生成，不影响业务数据。

---

## 4. 回滚方式

```bash
git revert <包重命名提交>
```

包目录会恢复为 `wendream/`，`pyproject.toml` 的入口与 `packages` 同步回退；
`.aieb/` 数据不受影响（两种情况下都读取同一目录）。
