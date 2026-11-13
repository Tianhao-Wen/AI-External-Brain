# LLM 配置与调用链现状报告


> **命名现状（2026-11 更新）**：本报告记录的是当时的事实。此后 Python 包已从 `wendream/`
> 迁移为 `aieb/`，命令从 `wendream` 改为 `aieb`，环境变量前缀统一为 `AIEB_`（旧名仍可读）。
> 文中出现的旧包路径 / 旧环境变量名请以 [`MIGRATION_RENAMING.md`](MIGRATION_RENAMING.md) 为准。

- 检查时间：2026-11-07
- 项目：`.`
- 检查方式：读配置 + 读调用链代码 + 两次真实 API 调用（App 与 Qwen 各一次）
- 原始输出：`docs/llm_check_output.txt`（不含任何密钥，只有变量名与布尔状态）
- 检查脚本：`python scripts/check_llm.py --probe-qwen --raw`

> 本报告只描述现状，**没有修改模型架构**；原始 WenDream 仅只读参考。

---

## 1. 当前 AI 对话实际使用哪个 Provider / 模型

| 项 | 实际值 |
|---|---|
| 配置文件 | `.\.aieb\config.yaml`（项目级） |
| 配置层级 | `~/.aieb/config.yaml`（缺失）→ 项目 `.aieb/config.yaml`（存在）→ `.aieb/config.local.yaml`（缺失） |
| provider 数量 | **1 个**（`providers[0]` 即当前使用） |
| provider 名称 | `deepseek-flash` |
| 协议 | `openai-compat`（走 `OpenAICompatClient`） |
| base_url | `https://api.deepseek.com` |
| 配置中的 model | `deepseek-chat` |
| **服务端实际返回的 model** | **`deepseek-flash`**（请求 `deepseek-chat`，DeepSeek 侧别名映射到 flash 版本，见原始调用证据） |
| 密钥来源 | `${DEEPSEEK_API_KEY}` → 已设置（代码不会打印密钥） |
| 上下文窗口 | 128000（内置映射） |
| 监听端口 | 配置写 `18888`，实际以 CLI `--port 18889` 覆盖启动 |

### 调用入口（代码路径，自下而上）

```
aieb (pyproject 入口)                       pyproject.toml  [project.scripts] aieb
  └─ wendream/brain/cli.py::main            load_config() → RemoteServer(providers=config.providers, ...)
       └─ aieb/remote.py::RemoteServer.__init__   self._active_provider = providers[0]
            └─ remote.py::_init_agent  (L910)        provider = self._active_provider
                 ├─ L936  BrainService(provider=provider, ...)   ← 业务功能共用这一个 provider
                 └─ L948  client = create_client(provider)       ← 对话用
                      └─ aieb/client.py::create_client       openai-compat → OpenAICompatClient
                           └─ aieb/agent.py L630  self.client.stream(conversation, system, tools)
```

- 业务侧每次处理请求前会同步 provider：
  `aieb/brain/ops.py L38-42 _sync_provider()` ← `lambda: self._active_provider`（`remote.py L943`）。
- 模型切换入口：WS 消息 `switch_provider` → `remote.py::_handle_switch_provider`（L1682，切换会新建会话）。
  **注意：产品化新 UI 里没有模型切换控件**（`brain/ui/app_js.py` 中不存在 `switch_provider`），
  所以当前实际只有「改配置 + 重启服务」这一条切换路径。

---

## 2. 各项功能分别使用哪个模型

**结论：全部功能共用同一个 `_active_provider`，没有按功能分配不同模型。**

| 功能 | 代码位置 | 用的模型 |
|---|---|---|
| AI 对话（含工具循环、自动压缩） | `agent.py` → `client.stream`；`context/manager`、`memory/consolidation` 复用同一 `self.client` | 当前 provider |
| PDF RAG 问答（/知识库提问） | `brain/ops.py L130` → `brain/llm.py::complete(service.provider)`；注入上下文的 `SearchKnowledge` 工具 → `service.answer` 同样用 `self.provider` | 同上 |
| TXT 会议风险分析 | `service.analyze_meeting` → `meetings.analyze_async(self.provider)` → `llm.complete_json` | 同上 |
| 邮件草稿生成 | `service.draft_email` → `meetings.draft_email(self.provider)` → `llm.complete_json`（模型失败时退化为模板草稿） | 同上 |
| 长期记忆「提取」 | `agent.py` 内 `_extract_memories` → `memory_manager.extract(self.client, ...)` | 同上 |
| 长期记忆「精选召回」 | `remote.py L1097-1104` 独立 side-query：`create_client(self._active_provider)` | 同上 |
| 长期记忆「/RecallMemory 工具」 | 纯本地检索（SQLite `memory_facts` + 记忆文件），**不调用模型** | — |
| 联网搜索结果整理 | 搜索本身不发 LLM（Bing/DDG/Bing News）；**结果整理由对话模型完成**（`WebSearch` 只返回结构化文本） | 当前 provider |
| 联网「原生检索」（可选） | `tools/web_search.py::QwenNativeSearchProvider`，需 `web_search.native_enabled=true`；当前配置为 `false` | 未启用 |
| PPT 内容生成 | `service.make_ppt` → `pptgen.generate(self.provider)` → `build_outline` → `llm.complete_json` | 同上 |
| 多模态（图/视频/语音，产品 UI 未使用） | `models/services.py` → `DashScopeClient`，密钥 `ALIYUN_BAILIAN_API_KEY` | 独立于对话模型 |

---

## 3. 原 WenDream 的 DeepSeek / Qwen 接入能力是否保留

**代码能力保留，配置未迁移。**

- 协议层：`validator.py::VALID_PROTOCOLS = {anthropic, openai, openai-compat}` —— DeepSeek、Qwen(百炼 Token Plan) 都是 `openai-compat`，可直接接入。
- 百炼能力层保留：`aieb/models/services.py`（图片/视频/语音等）、`aieb/models/dashscope.py`、
  `tools/web_search.py::QwenNativeSearchProvider` 与 `build_native_provider()`。
- 原 WenDream 配置（只读核对）里确实有 3 个 provider：

| 原 provider | 协议 | base_url | model | 密钥变量 |
|---|---|---|---|---|
| deepseek-flash | openai-compat | `https://api.deepseek.com` | `deepseek-flash` | `${DEEPSEEK_API_KEY}` |
| deepseek-v4-pro | openai-compat | `https://api.deepseek.com` | `deepseek-v4-pro` | `${DEEPSEEK_API_KEY}` |
| qwen3.8-max | openai-compat | `https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1` | `qwen3.8-max` | `${ALIYUN_BAILIAN_API_KEY}` |

- **AIEB 当前只迁移了 1 个 DeepSeek provider**；Qwen 未写入本项目配置，因此对话不会用到 Qwen。
- Qwen 是否「真能用」——已实测（`--probe-qwen`）：
  - `dashscope.aliyuncs.com/compatible-mode/v1` + `qwen-plus` → **HTTP 401 invalid_api_key**（该端点不接受现有 Key）
  - `token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1` + `qwen3.8-max` + `ALIYUN_BAILIAN_API_KEY` → **HTTP 200，1.66s，响应「Qwen 连通性通过」**

  → **Qwen 通道可用，只是没配置进本项目。**

---

## 4. 能否「Qwen 主 + DeepSeek 备用」

- **当前不支持自动故障转移。** 对话链只持有一个 client（`agent.client`），失败时只在**同一 provider** 内重试
  （`agent.py` 的 max_tokens escalation / 输出恢复，以及 SDK 层错误映射），不会切到第二个 provider。
  唯一的切换是手动 WS 消息 `switch_provider`（新 UI 无入口）。
- **可以做到「Qwen 为主」**：把 Qwen 放在 `providers[0]` 即可（不改代码）。
- 「DeepSeek 作为自动备用」需要改代码（在 `create_client` 外层加 provider 池/熔断/回退），**本次未做**，等你确认。

### 最小配置（不改代码，Qwen 主 + DeepSeek 在列表里待切换）

写入 `.aieb/config.local.yaml`（该文件已被 `.gitignore` 忽略，优先级高于 `config.yaml`）：

```yaml
providers:
  - name: qwen3.8-max
    display_name: "千问 Qwen3.8-Max（Token Plan）"
    protocol: openai-compat
    base_url: https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1
    model: qwen3.8-max
    api_key: "${ALIYUN_BAILIAN_API_KEY}"

  - name: deepseek-flash
    display_name: DeepSeek
    protocol: openai-compat
    base_url: https://api.deepseek.com
    model: deepseek-chat
    api_key: "${DEEPSEEK_API_KEY}"
```

重启 `aieb --port 18889` 后，`providers[0]` 即 Qwen；可用
`python scripts/check_llm.py --raw` 确认实际返回的 `model` 字段。

> 注意：`config.local.yaml` 的 `providers` 列表是**整体覆盖**，必须把两个 provider 都列全。

---

## 5. 代码里实际读取的环境变量名（不是猜的）

| 变量名 | 读取位置 | 当前状态 |
|---|---|---|
| `DEEPSEEK_API_KEY` | 配置里 `${DEEPSEEK_API_KEY}` → `resolve_env_vars` | 已设置 |
| `ALIYUN_BAILIAN_API_KEY` | `models/services.py:115`（百炼能力层）；原 Qwen provider 的 `${}` | 已设置 |
| `DASHSCOPE_API_KEY` | `models/services.py:115`（同上，作为备选名） | 未设置 |
| `DASHSCOPE_BASE_URL` | `models/services.py:114` | 未设置 |
| `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` | `config.py::_ENV_KEY_MAP`（协议级回退） | 未设置 |
| `TAVILY_API_KEY` | `tools/web_search.py:1242` | 未设置 |
| `AIEB_SMTP_HOST/PORT/SECURITY/USER/PASSWORD/FROM` | `brain/mailer.py` | 已设置（本次邮件验收用） |
| `WENDREAM_REMOTE_TOKEN` | `remote.py:129` | 未设置 |
| `WENDREAM_REMOTE_MEMORY_DIR` | `memory/auto_memory.py:54` | 未设置 |
| `WENDREAM_LAZY_EXPLORE_TOOLS` | `tools/__init__.py:169` | 未设置（默认 1） |

命名不一致之处（如实记录）：邮件用 `AIEB_` 前缀，其余多为历史的 `WENDREAM_` 前缀；
`openai-compat` 协议的默认回退变量是 `OPENAI_API_KEY`，因此 **DeepSeek/Qwen 的 Key 必须靠配置里的 `${...}` 显式指定变量名**，不会被自动推断。

---

## 6. 真实调用结果（已执行，不含密钥）

命令：`python scripts/check_llm.py --probe-qwen --raw`

| 调用 | 请求 | 结果 |
|---|---|---|
| 应用同款客户端（`create_client` → `OpenAICompatClient.stream`） | provider=deepseek-flash，model=deepseek-chat | **OK**，0.9s，回答「模型连通性测试通过。」 |
| 原始 HTTP（确认服务端返回的 model） | 同上 | **HTTP 200**，响应 `model = deepseek-flash`，`id = 7353…f6a1`，`usage` 正常 |
| Qwen（DashScope 公共端点） | qwen-plus | **HTTP 401 invalid_api_key**（端点/Key 不匹配） |
| Qwen（百炼 Token Plan） | qwen3.8-max | **HTTP 200**，1.66s，回答「Qwen 连通性通过」，`usage.prompt_tokens=70` |

---

## 7. 如果还没配置可用模型，最小配置步骤

本机已有可用配置（DeepSeek 200 OK），以下是「从零配好」的最小步骤，供换机/重置时使用：

```powershell
# 1) 配置模型（模板已存在）
Copy-Item .aieb\config.yaml.example .aieb\config.yaml
# 2) 密钥只走环境变量
$env:DEEPSEEK_API_KEY = "<你的 key>"
# 3) 启动
aieb --port 18889
# 4) 验证真实调用（会打印 provider/model/状态/耗时/响应）
python scripts/check_llm.py --raw
```

---

## 8. 现状小结（一句话版）

- 对话与全部业务功能（PDF 问答、会议分析、记忆提取/召回、PPT 生成、搜索整理）**共用同一个 provider**：`deepseek-flash / openai-compat / https://api.deepseek.com / 请求 deepseek-chat，实际返回 deepseek-flash`；
- **原 WenDream 的 DeepSeek 与 Qwen 接入能力都在代码里保留**，但本项目只配置了 DeepSeek；
- **Qwen 通道实测可用**（百炼 Token Plan 端点 + `ALIYUN_BAILIAN_API_KEY`），可按 §4 的 YAML 一键切为主模型；
- **不支持自动主备切换**（需要时我可另开一次改动，仅加 provider 回退，不动业务层）。
