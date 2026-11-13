# Docker 容器化部署 —— 验证报告

> 日期：2026-11-12　分支：`feature/docker-deployment`　基线：`a118f71`
> **结论先行：Docker 资产已完成并通过 22 项静态校验；"容器运行契约"已在宿主机按同一套环境变量
> 实跑验证（RAG / LangGraph / 重启恢复全部通过）；但 `docker build` 与 `docker compose up`
> 在本机为 BLOCKED —— 本机没有 Docker。没有虚构任何容器运行结果。**

## A. 文件变更清单

| 文件 | 类型 | 用途 |
| --- | --- | --- |
| `Dockerfile` | 新增 | Python 3.12-slim 后端镜像：依赖层/源码层分离、非 root(uid 10001)、tini 作 PID1、HEALTHCHECK、`/data` 为数据根 |
| `.dockerignore` | 新增 | 排除 `.env`/`.aieb`/虚拟环境/SQLite/PDF/`.git`/测试与报告；仅白名单保留 `README.md` 与冒烟脚本 |
| `requirements-docker.txt` | 新增 | 镜像专用依赖清单：`-r requirements.txt` + hybrid 组（qdrant-client/fastembed/onnxruntime）+ langgraph 组，**不改动现有版本约束** |
| `docker/entrypoint.sh` | 新增 | 准备数据目录、首启生成配置、绑定 0.0.0.0 时强制校验 token、`exec` 启动 |
| `docker/config.docker.yaml` | 新增 | 容器默认配置模板（`api_key: "${DEEPSEEK_API_KEY}"`，无任何密钥字面量） |
| `docker-compose.yml` | 修改 | 从"仅 Qdrant"扩展为 `aieb`(后端) + `qdrant` 两服务；命名卷 `aieb_data`/`qdrant_storage`；健康检查、`stop_grace_period`、30 个环境变量透传 |
| `aieb/remote.py` | 修改 | 新增 `GET /healthz`（存活）与 `GET /readyz`（就绪，含 Qdrant TCP 探测）；不改动既有路由与鉴权逻辑 |
| `.env.example` | 修改 | 补充 Docker 段落（`AIEB_REMOTE_TOKEN`/`AIEB_PORT`/Qdrant 端口/dry-run 说明）；**修复一处历史问题：示例里曾残留一个非空 SMTP 口令，已置空** |
| `scripts/docker_smoke_test.py` | 新增 | 部署冒烟测试（HTTP/鉴权/WS RPC/上传 PDF/Hybrid 检索/工作流），容器内外通用 |
| `scripts/validate_docker_assets.py` | 新增 | Docker 资产静态校验（22 项断言） |
| `scripts/verify_container_contract.py` | 新增 | 无 Docker 时的"容器运行契约"验证：同套 env + 0.0.0.0 + token + 独立数据目录，含跨进程重启恢复场景 |
| `docs/docker-deployment.md` | 新增 | 部署指南（环境准备→配置→启动→持久化验证→排障→回退→限制） |
| `README.md` | 修改 | §6 追加"docker compose 一键启动"小节，链接详细文档（未改动任何既有内容） |
| `docs/DOCKER_DEPLOYMENT_VALIDATION.md` | 新增 | 本文 |

未改动：`aieb/agent.py`、`aieb/brain/retrieval/*`（RAG 逻辑）、`aieb/workflow/*`（LangGraph 实现）、任何 Benchmark 结果与测试数据。

## B. 最终部署架构

```
                   宿主机 (Windows / Docker Desktop)
  ┌────────────────────────────────────────────────────────────┐
  │ 浏览器  http://localhost:18888/#token=<AIEB_REMOTE_TOKEN>   │
  └───────────────────────────┬────────────────────────────────┘
                              │  published port 18888:18888
  ┌───────────────────────────▼────────────────────────────────┐
  │ compose 网络 (name: aieb)                                   │
  │  ┌──────────────────────────┐    ┌───────────────────────┐  │
  │  │ aieb (python:3.12-slim)  │    │ qdrant v1.20.0        │  │
  │  │ USER aieb (uid 10001)    │───►│   qdrant:6333 (REST)  │  │
  │  │ WORKDIR /data            │    │   /qdrant/storage     │  │
  │  │ ENTRYPOINT tini→bash     │    │   healthcheck TCP     │  │
  │  │  /app  = 代码+依赖        │    └───────────┬───────────┘  │
  │  └───────────┬──────────────┘                │              │
  └──────────────┼───────────────────────────────┼──────────────┘
                 │ volume aieb_data             │ volume qdrant_storage
        ┌────────▼─────────────────────┐  ┌──────▼──────────────┐
        │ /data/.aieb/brain/           │  │ 向量索引(派生)        │
        │   brain.sqlite3  (+WAL/SHM)  │  │ 可从 SQLite 重建      │
        │   uploads/*.pdf              │  └─────────────────────┘
        │ /data/.aieb/workflow/        │
        │   workflow.sqlite3 (Trace/幂等)│
        │   checkpoints.sqlite3 (LangGraph)
        │   inbox/ (原文暂存)           │
        │ /data/.aieb/sessions/*.jsonl  │
        │ /data/.cache/{fastembed,hf}   │
        └──────────────────────────────┘
```

- **后端容器**：监听 `0.0.0.0:18888`（可用 `AIEB_PORT` 改），`docker/entrypoint.sh` 准备配置后 `exec python -m aieb.brain.cli`。
- **Qdrant 容器**：镜像 tag 与代码锁定版本一致（`v1.20.0`）；后端通过**服务名** `http://qdrant:6333` 访问。
- **Docker 网络**：compose 默认网络，服务名即域名；不暴露额外端口（gRPC 仅本机调试可选）。
- **SQLite / Checkpoint**：都在 `aieb_data` 卷的 `/data/.aieb/` 下，容器重建不丢。
- **向量索引**：`qdrant_storage` 卷；即使丢失也能从 SQLite 重建（幂等 upsert）。
- **前端访问**：同一容器提供的 HTML + WebSocket（`/`、`/ws`、`/file`、`/healthz`、`/readyz`）。

## C. 实际执行过的命令（真实输出）

### C1. 环境探测（Docker 是否可用）

```powershell
docker version --format '{{.Server.Version}}'      # → 无法将"docker"项识别为...（未安装）
docker compose version                             # → 同上
Test-Path "C:\Program Files\Docker\Docker\resources\bin\docker.exe"   # → False
wsl --status                                       # → 提示需先安装适用于 Linux 的 Windows 子系统
Get-Command podman                                 # → 不存在
```

结论：**Docker Engine / Docker Desktop / WSL / Podman 均不可用** → 容器相关步骤 BLOCKED。

### C2. 镜像与依赖的静态可用性（联网只读校验）

```powershell
python benchmarks\.work\check_tags.py
# OK  library/python:3.12-slim  amd64_size=41.3MB  updated=2026-11-08
# OK  qdrant/qdrant:v1.20.0     amd64_size=69.2MB  updated=2026-10-21

python benchmarks\.work\check_wheels.py
# textual/anthropic/openai/pyyaml/pydantic/mcp/httpx/websockets/python-pptx/qdrant-client/
# fastembed/onnxruntime/langgraph/langgraph-checkpoint-sqlite/langchain-openai/aiosqlite → 均有 cp312 或 pure wheel
# pymupdf 提供 cp311-abi3 manylinux_2_28 轮子（slim/bookworm glibc 2.36 满足）
```

### C3. Docker 资产静态校验

```powershell
python scripts\validate_docker_assets.py
# 静态校验：22/22 通过
```

覆盖：基础镜像带 tag、依赖层先于源码层、非 root、HEALTHCHECK、无 `COPY . .`、tini+exec、
`EXPOSE` 与默认端口一致、镜像内无密钥字面量、`.dockerignore` 排除敏感项、
compose 服务/卷/健康检查/`depends_on`/端口一致性/`AIEB_QDRANT_URL` 用服务名/默认 dry-run、
`.env.example` 与配置模板无真实密钥。

### C4. 容器运行契约验证（无 Docker，宿主机同套环境变量）

```powershell
# (1) Qdrant 不可达：验证降级与鉴权
python scripts\verify_container_contract.py --port 18899 --qdrant-url http://127.0.0.1:6399
# healthz 200 / readyz 503（降级）/ index 200 / ws 401 / bad-origin 403 / ws+token OK  → 6/6

# (2) 启动本地 Qdrant（版本与 compose 锁定一致：1.20.0），换独立存储目录
tools\qdrant\qdrant.exe  (QDRANT__STORAGE__STORAGE_PATH=benchmarks\.work\qdrant_ct\storage)

# (3) 真实 Hybrid RAG：上传合成 PDF → 分块 → 向量写入 → 检索 + 引用
python scripts\verify_container_contract.py --port 18899 --qdrant-url http://127.0.0.1:6333 --rag-upload
# rag_pdf_parsed: chunks=1 / rag_vector_index_written: written=1 embed_seconds=0.024 errors=[]
# rag_hybrid_query: citations=1 mode=hybrid / 回答命中「每晚600元、超5000需部门审批」
# qdrant_collection_present: ['aieb_chunks']  → 12/12

# (4) 重启进程后数据仍在（等价于 docker compose restart）
python scripts\verify_container_contract.py --port 18899 --qdrant-url http://127.0.0.1:6333 --rag --min-docs 1 --keep
# rag_documents_list: documents=1（要求 ≥1）  → 7/7

# (5) 在该部署配置下跑通 LangGraph 工作流
python scripts\verify_container_contract.py --port 18899 --qdrant-url http://127.0.0.1:6333 --rag --min-docs 1 --workflow --keep
# workflow_start: status=waiting_approval id=wf_a750542d6d35 / workflow_cancel: status=cancelled  → 9/9

# (6) 审批中断跨进程恢复（= 任务书测试 C）
python scripts\verify_container_contract.py --port 18899 --qdrant-url http://127.0.0.1:6333 --restart-workflow --keep
# restart.workflow_waiting_approval PASS → 终止进程 → restart.checkpoint_recovered PASS
# restart.workflow_findable_by_id PASS / resume_after_restart PASS(cancelled)
# restart.trace_available_after_restart PASS(13 事件) / no_duplicate_send PASS  → 6/6
```

### C5. 回归测试

```powershell
.venv-langgraph\Scripts\python.exe -m pytest tests -q
# 2 failed, 1485 passed, 3 skipped in 105.01s
```

两个失败与本次改动无关（Windows `sleep` 语义问题），已在本仓库前一轮用独立 `git worktree` 在基线
`2f93ec4` 上复现为**既有失败**：`test_bash_encoding::test_bash_utf8_child_output_is_clean`、
`test_hooks::TestCommandExecutor::test_timeout`。

## D. 验证结果表

| 验证项目 | 结果 | 证据 |
| --- | --- | --- |
| Docker 可用性 | **BLOCKED** | `docker`/`docker compose` 命令不存在；无 Docker Desktop、无 WSL、无 Podman |
| Dockerfile / compose 静态校验 | **PASS** | `scripts\validate_docker_assets.py` → 22/22 |
| 基础镜像与依赖可获取性 | **PASS** | 镜像 tag 查询 OK；17 个依赖均有 cp312/abi3 Linux 轮子 |
| Docker Build | **BLOCKED** | 本机无 Docker（**未执行，未虚构**） |
| Compose 启动 | **BLOCKED** | 同上 |
| Qdrant 连接（容器内服务名地址） | **PASS（宿主机等价验证）** | `AIEB_QDRANT_URL=http://127.0.0.1:6333` 与容器内 `http://qdrant:6333` 同一配置路径；`/readyz` 200、collection `aieb_chunks` 存在（512 维 cosine） |
| PDF / Hybrid RAG | **PASS** | 上传合成 PDF → chunks=1、written=1、`errors=[]`；`knowledge.query` 返回 citations=1、mode=hybrid、回答含「每晚600元/超5000需审批」 |
| LangGraph 工作流 | **PASS** | `workflow.start → waiting_approval → cancel`，在容器同套配置下完成（真实 DeepSeek） |
| Checkpoint 恢复（跨进程） | **PASS** | 6/6：`checkpoint_recovered`（resumable=True）、按原 ID 可查、可继续取消、Trace 13 条仍可读、无重复发信 |
| Volume 持久化（== 文件级） | **PASS（等价验证）** | 停止进程后重启，`documents=1` 仍在；`/data/.aieb/` 内含 brain.sqlite3、uploads/*.pdf、workflow/checkpoints.sqlite3、sessions/*.jsonl |
| `docker compose restart` / `down` + `up` | **BLOCKED** | 无 Docker；等价实验已用"停进程→新进程"覆盖 |
| 浏览器 E2E（容器化后端） | **BLOCKED** | 无法拉起容器；上一轮非容器 E2E 21/21 见 `docs/LANGGRAPH_DEPLOYMENT_VALIDATION.md` |
| pytest 回归 | **PASS（无新增失败）** | 1485 passed / 2 failed（既有）/ 3 skipped |

## E. 已知限制（诚实清单）

1. **是否真正运行 Docker**：❌ **没有**。本机未安装 Docker Desktop / Docker Engine / WSL / Podman，
   `docker build`、`docker compose up`、`docker compose restart/down` 全部未执行。
2. **是否实际运行了完整业务流程**：✅ 是，但运行在**宿主机进程**上（与容器同套环境变量、同一启动入口、
   同一数据目录布局）。RAG（上传→索引→检索）、LangGraph（分析→审批→取消→跨进程恢复）均真实跑通，
   并调用了真实 DeepSeek（本轮共约 6 次调用）。
3. **是否使用 Fake SMTP**：✅ 邮件全程 `AIEB_EMAIL_DRY_RUN=1`（compose 默认值），发送返回 `send_blocked`，
   零真实投递；邮件相关自动化测试仍用仓库内 Fake SMTP。
4. **是否验证了容器重建后的状态保持**：❌ 容器重建未验证（BLOCKED）；✅ **数据目录重建/进程重启**已验证
   （文档、SQLite、Checkpoint 全部保留）。
5. **尚未解决的兼容性问题**：
   - Qdrant 镜像的 healthcheck 用 `bash /dev/tcp`，在不同镜像版本上可能不可用 → 已用
     `depends_on: service_started` + `/readyz` 应用侧探测兜底，避免阻塞启动；
   - Windows 上的 bind mount + SQLite WAL 有锁风险 → 默认改为命名卷；
   - 宿主端口 ≠ 容器端口会触发 Origin 403 → 已强制"宿主端口=容器端口"，并在文档给出改端口方案；
   - 镜像体积较大（onnxruntime + pymupdf + langgraph），未做多阶段瘦身；
   - 首次启动需要外网下载 embedding 模型（已缓存到 `/data/.cache`，重建容器不重复下载）。
6. **安全边界**：只有单 token 鉴权（无账号体系/RBAC），**不得暴露到公网**；`.env` 与 `.aieb/config.yaml`
   已被 `.gitignore`/`.dockerignore` 双重排除；镜像内不含任何密钥、数据库或私有文档。
7. **历史遗留问题（本次修复）**：`.env.example` 里曾残留一个非空 SMTP 口令（已在仓库历史中），
   本次已置空并加注释。**如果那是真实凭据，请更换/吊销**。

## F. 在装有 Docker 的机器上补齐验证的命令清单

```powershell
# 0) 准备
docker version && docker compose version
Copy-Item .env.example .env      # 填 DEEPSEEK_API_KEY 与 AIEB_REMOTE_TOKEN

# 1) 构建与启动
docker compose build
docker compose up -d
docker compose ps                                    # 期望 aieb / qdrant 均 healthy

# 2) 服务与就绪
curl.exe http://localhost:18888/healthz
curl.exe http://localhost:18888/readyz

# 3) 业务：RAG + 工作流
python scripts\docker_smoke_test.py --url http://localhost:18888 --token <TOKEN> --rag-upload --workflow

# 4) 测试 A：重启
docker compose restart aieb
python scripts\docker_smoke_test.py --url http://localhost:18888 --token <TOKEN> --rag --min-docs 1

# 5) 测试 B：删除容器后重建（禁止加 -v）
docker compose down
docker compose up --build -d
python scripts\docker_smoke_test.py --url http://localhost:18888 --token <TOKEN> --rag --min-docs 1

# 6) 测试 C：审批中断时重启（浏览器里先发起工作流并停在等待审批）
docker compose restart aieb
# 刷新页面 → 同一 workflow_id → 确认发送 → 只发 1 封

# 7) 日志与清理
docker compose logs --tail=200 aieb
docker compose down
```

把 1)–7) 的真实输出贴回本文 §D 表格，即可把 BLOCKED 项转为 PASS/FAIL。
