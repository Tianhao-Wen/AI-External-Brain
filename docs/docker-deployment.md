# Docker 部署指南（后端 + Qdrant）

> 本文覆盖：环境准备 → 配置 → 构建/启动 → 访问 → 持久化验证 → 排障 → 回退到本地运行。
> 配套文件：`Dockerfile`、`.dockerignore`、`docker-compose.yml`、`docker/entrypoint.sh`、
> `docker/config.docker.yaml`、`requirements-docker.txt`、`.env.example`。

## 0. 重要前提：本轮验证到哪一步

| 项目 | 状态 |
| --- | --- |
| Docker 资产（Dockerfile / compose / entrypoint / 忽略清单） | ✅ 已编写并静态校验（22/22） |
| 容器运行契约（0.0.0.0 监听 + token 鉴权 + 数据目录 + Qdrant 地址 + dry-run 发信） | ✅ 已在宿主机按同一套环境变量实跑验证（**非容器内**） |
| `docker build` / `docker compose up` / `restart` / `down` | ❌ **BLOCKED**：本机未安装 Docker Desktop / Docker Engine / WSL |
| 真实浏览器 E2E（容器化后端） | ❌ BLOCKED（同上，无法拉起容器） |

**没有 Docker 的机器上不能声称"已完成容器部署"**。请在装有 Docker Desktop 的机器上按 §8 的清单跑一遍，
把结果填进 `docs/LANGGRAPH_DEPLOYMENT_VALIDATION.md` 的对应表格（或直接看本轮报告 §D）。

## 1. 环境准备

- **Windows 10/11**：安装 Docker Desktop（WSL2 后端），安装后确认：
  ```powershell
  docker version
  docker compose version      # 需要 Compose V2（docker compose，不是 docker-compose）
  ```
- **Linux**：Docker Engine ≥ 24 + `docker compose` 插件即可。
- 磁盘：镜像约 1.2–1.6 GB（含 onnxruntime / pymupdf / langgraph）；首次运行会下载 embedding 模型
  （`BAAI/bge-small-zh-v1.5`，约 100 MB），落在数据卷里，重建容器不会重复下载。
- 内存：建议 ≥ 6 GB（ONNX 推理 + Qdrant + Python）。

### Windows 注意事项

1. 项目路径不要放在 OneDrive 同步目录（SQLite WAL 与文件锁易冲突）。
2. **不要把宿主机的 `.aieb/` 目录直接 bind mount 进容器**做日常开发：Windows 上的文件权限/锁语义
   与 Linux 不同，SQLite WAL 容易报 `database is locked`。默认用命名卷 `aieb_data`（见 §5）。
3. 如果想在宿主机直接看数据文件，用 `docker compose cp` 或改为 Linux 文件系统上的 bind mount。

## 2. 配置 `.env`

```powershell
Copy-Item .env.example .env
```

然后编辑 `.env`（该文件已被 `.gitignore` 忽略，**不会进版本库，也不会进镜像**）：

| 变量 | 必填 | 说明 |
| --- | --- | --- |
| `DEEPSEEK_API_KEY` | 是（要用对话/会议工作流时） | 只在运行时注入；镜像里没有 |
| `AIEB_REMOTE_TOKEN` | **Docker 模式下必填** | 容器内监听 `0.0.0.0`，项目安全模型要求非 loopback 绑定必须带 token |
| `AIEB_PORT` | 否（默认 18888） | 宿主与容器使用同一端口（Origin 校验依赖端口一致） |
| `AIEB_RETRIEVAL_MODE` | 否（默认 `hybrid`） | `bm25` / `vector` / `hybrid` / `hybrid_rerank` |
| `QDRANT_API_KEY` | 否 | 给 Qdrant 加 API Key 时同时生效到后端 |
| `AIEB_EMAIL_DRY_RUN` | 否（默认 `1`） | 默认禁止真实发信；发送会返回 `send_blocked`（不假装成功） |

生成 token：

```powershell
python -c "import secrets;print(secrets.token_urlsafe(24))"
```

## 3. 构建与首次启动

```powershell
docker compose up --build -d
docker compose ps                 # 两个服务都应 healthy（aieb 首次约 40s 后才 healthy）
docker compose logs -f aieb
```

镜像分层：先 `pip install -r requirements-docker.txt`（依赖层），再 `COPY aieb`（源码层），
改业务代码不会让依赖层失效。

## 4. 访问 Web UI

浏览器打开：

```
http://localhost:18888/#token=<你的 AIEB_REMOTE_TOKEN>
```

- token 通过 URL **fragment** 传递，随后被前端写入 `sessionStorage` 并从地址栏抹除；
  WebSocket 握手时用 `aieb-token.<token>` 子协议携带（`aieb/remote.py`）。
- 健康检查端点（容器 healthcheck 与排障用）：
  - `GET /healthz` → 进程存活（200）
  - `GET /readyz` → 就绪，会额外探测 Qdrant；Qdrant 不可达时返回 503（此时 `bm25` 仍可用）

## 5. 数据保存在哪里

容器内 `WORKDIR=/data`，只挂一个命名卷：

| 卷 | 容器路径 | 内容 |
| --- | --- | --- |
| `aieb_data` | `/data/.aieb/` | `brain/brain.sqlite3`（业务库）、`brain/uploads/*.pdf`（上传原文）、`workflow/workflow.sqlite3`（Trace + 幂等账本）、`workflow/checkpoints.sqlite3`（LangGraph Checkpoint）、`workflow/inbox/`（原文暂存）、`sessions/*.jsonl` |
| `aieb_data` | `/data/.cache/` | `fastembed/`（embedding 缓存）、`huggingface/`（模型下载缓存） |
| `qdrant_storage` | `/qdrant/storage` | 向量索引（可从 SQLite 重建，但保留可省重复 embedding） |

查看卷位置与内容：

```powershell
docker volume ls | Select-String aieb
docker compose exec aieb ls -la /data/.aieb /data/.aieb/workflow
docker compose exec aieb du -sh /data/.aieb
```

## 6. 持久化验证（对应任务书测试 A/B/C）

### 测试 A：重启容器

```powershell
docker compose restart aieb
docker compose exec aieb ls /data/.aieb/brain        # 业务库仍在
```

### 测试 B：删除容器后重建（**不要带 `-v`**）

```powershell
docker compose down            # 只删容器/网络，保留卷
docker compose up --build -d
docker compose exec aieb ls /data/.aieb/workflow     # checkpoint 仍在
```

⚠️ `docker compose down -v` 会删除卷 = 业务数据与向量索引全部丢失。**做持久化验证时禁止使用 `-v`。**

### 测试 C：LangGraph 审批中断恢复

```powershell
# 1) 浏览器发起一个会议工作流，停在「等待人工审批」
# 2) 重启后端
docker compose restart aieb
# 3) 刷新页面：工作流还在同一个 workflow_id，点「确认发送」→ completed；
#    邮件只发一次（dry-run 下返回 send_blocked，不会真的投递）
```

自动化版本（容器内自带冒烟脚本，无需浏览器；`--workflow` 会消耗 1–2 次 LLM 调用）：

```powershell
docker compose exec aieb python /app/scripts/docker_smoke_test.py `
  --url http://127.0.0.1:18888 --token $env:AIEB_REMOTE_TOKEN --workflow
```

## 7. 冒烟测试与排障

宿主机执行（推荐）：

```powershell
python scripts/docker_smoke_test.py --url http://localhost:18888 --token $env:AIEB_REMOTE_TOKEN
# 可选：--rag（知识库列表）、--rag-upload（上传合成 PDF + 一次 Hybrid 检索，消耗 1 次 LLM）
```

### 常见故障

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| `[entrypoint] 启动中止：绑定 0.0.0.0 必须提供访问 token` | 未设置 `AIEB_REMOTE_TOKEN` | 在 `.env` 里设置后 `docker compose up -d` |
| 浏览器页面能开，但 WebSocket 一直"重连中" | URL 里没带 token，或 token 不一致 | 用 `http://localhost:18888/#token=...` |
| 页面 403 `Forbidden origin` | 宿主端口 ≠ 容器端口 | 保持 `AIEB_PORT` 一致；或把 `http://localhost:<端口>` 写进 `config.yaml` 的 `remote.allowed_origins` |
| `readyz` 返回 503 | 后端连不上 Qdrant | `docker compose logs qdrant`；确认 `AIEB_QDRANT_URL=http://qdrant:6333`（容器内不能用 127.0.0.1） |
| 上传 PDF 后提示"只能走关键词检索" | 向量写入失败（Qdrant 不可用/维度不匹配） | 看 `docker compose logs qdrant`；重启后重新上传 |
| `database is locked` | 两个实例同时写同一个数据目录 | 只保留一个 `aieb` 容器；不要把一个卷同时挂给两个容器 |
| Qdrant 端口 6333 被占用 | 本机已有原生 Qdrant | 改 `.env` 的 `QDRANT_HTTP_PORT=6334` 之类；容器间通信不受影响 |

### 与"本机原生 Qdrant"共存的规则

- 容器之间走服务名 `qdrant:6333`，与宿主端口无关；`QDRANT_HTTP_PORT` 只影响你从宿主机访问。
- 若宿主机已有原生 Qdrant 占用 6333/6334，**不要**去杀它；直接把 `QDRANT_HTTP_PORT`/`QDRANT_GRPC_PORT`
  改成 7333/7334，或干脆不启动 compose 里的 qdrant（`docker compose up -d aieb` 需要手动指定
  `AIEB_QDRANT_URL` 指向宿主 Qdrant，容器内要写 `http://host.docker.internal:6333`）。

## 8. 在有 Docker 的机器上需要补跑的命令

```powershell
docker compose build
docker compose up -d
docker compose ps
docker compose exec aieb python -c "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:18888/readyz').read().decode())"
python scripts/docker_smoke_test.py --url http://localhost:18888 --token <token> --rag-upload --workflow
docker compose restart aieb
python scripts/docker_smoke_test.py --url http://localhost:18888 --token <token> --rag --min-docs 1
docker compose down
docker compose up --build -d
python scripts/docker_smoke_test.py --url http://localhost:18888 --token <token> --rag --min-docs 1
docker compose logs --tail=100 aieb
```

把每一步输出记到 `docs/LANGGRAPH_DEPLOYMENT_VALIDATION.md` §D 的验证表里。

## 9. 回退到本地（非 Docker）运行

Docker 改造是**增量**的，本地开发方式完全没有被修改：

```powershell
# 1) 停掉容器（保留数据卷）
docker compose down

# 2) 本地起后端（原来的方式）
python -m venv .venv --system-site-packages
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m pip install -e ".[hybrid,langgraph]"
.venv\Scripts\python.exe -m aieb.brain.cli            # 127.0.0.1:18888

# 3) 只需要向量库时，单独起 Qdrant
docker compose up -d qdrant
```

本地模式的数据仍在 `<项目根>/.aieb/`，与容器卷 `aieb_data` **互不影响**（两份独立数据）。

## 10. 已知限制

1. **本机无 Docker**：镜像构建、compose 启动、容器重启/重建、容器内浏览器 E2E 全部未在本轮执行（BLOCKED）。
2. **无多用户认证**：只有单一 token 的访问控制，没有账号体系/RBAC。**不要把端口暴露到公网**。
3. **单实例**：数据目录带"单实例锁"（`.aieb` 下的 lock 文件），同一卷不能给两个容器同时使用。
4. **Qdrant 健康检查**用 `bash /dev/tcp`（官方镜像无 curl/wget），某些镜像版本可能不生效；
   因此后端用 `depends_on: service_started` + 懒连接 + `/readyz` 应用侧探测，不依赖该探针。
5. **向量模型首次下载需要外网**；纯内网环境请预先准备模型并挂载到 `/data/.cache/fastembed`。
6. **镜像体积**较大（onnxruntime + pymupdf + langgraph），未做多阶段瘦身。
7. **无 Docker 时不要声称已完成生产级部署** —— 本仓库的结论是"资产就绪 + 契约已验证 + 容器运行待补验"。
