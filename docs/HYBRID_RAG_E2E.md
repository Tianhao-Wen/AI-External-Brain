# Hybrid RAG 真实浏览器 E2E 验收

> 产物：`benchmarks/results/hybrid_rag/e2e_browser_{bm25,vector,hybrid,hybrid_rerank}.json`、
> `e2e_browser_degraded.json`、截图 `benchmarks/results/hybrid_rag/e2e_shots/*.png`（29 张）
> 脚本：`scripts/e2e_hybrid_browser.py`、`scripts/e2e_hybrid_degraded.py`
> 执行时间：2026-11-11 19:01–19:33（单机、真实 Chrome、真实 Qdrant、真实 DeepSeek）

---

## 0. 结论

**四种检索模式在真实浏览器里全部跑通，Qdrant 故障时的降级与提示也已验证。但这一轮 E2E 抓出了 3 个真实缺陷，其中 2 个会直接影响用户对结果的信任，已修复。**

| 模式 | 上传 | 页码引用 | 相似编号 | 跨页多证据 | 不重复向量 | 内容更新 | 删除同步 | 会话恢复 | 用时 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `bm25` | ✅ | ✅ | ✅ | ✅ | ✅（无向量，改为"文档不重复"） | ✅ | ✅ | —（未开） | 26.5 s |
| `vector` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | —（未开） | 50.9 s |
| `hybrid` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ 刷新后重放 | 45.8 s |
| `hybrid_rerank` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | —（未开） | 52.3 s |
| Qdrant 不可用（`hybrid`） | ✅ 未被阻塞 | — | — | — | — | — | — | — | 35.9 s |

> 上表是**第一次** E2E 的结果。收尾验收（单文档上下文统一、作用域隔离与并发锁补强）之后，
> 四种模式 + 降级路径**全部重跑了一遍**，`all_passed` 均为 true；
> 其中 hybrid 额外开了 `--session-check`，刷新页面后当前会话能正确重放（`session_history_restored=true`）。

### 会话历史恢复（本次新增）

步骤：对话页发一句“请只回复四个字：收到确认” → 等流式结束 → `page.reload()` → 重新进入对话页 →
断言原来的用户提问与回答都还在（实测 `before_len == after_len == 25`）。
它验证的是“会话落盘 + 重连后 `replay_current`”这条路径，而不是会话内存在内存里凑巧没丢。

---

## 1. 隔离与安全（先讲清楚"没动到什么"）

| 项目 | 做法 |
| --- | --- |
| 数据目录 | 每次运行独立 `benchmarks/.work/e2e_<mode>/`，自带 `.aieb/`（gitignored）；脚本里硬校验路径必须含 `benchmarks/.work` |
| 正式 `.aieb/` | 运行前后核对：`.aieb/brain/brain.sqlite3`、`.aieb/sessions/`、`.aieb/memory/` 最新时间戳仍是 **11-09**，**未被本轮改动** |
| API Key | 配置文件只写 `${DEEPSEEK_API_KEY}` 占位符；密钥通过**父进程环境变量**注入，**没有任何密钥被复制进测试产物或写进磁盘上新增的配置文件** |
| 邮件 | `web_search.enabled=false`，测试流程完全不含邮件发送路径，**未发送任何真实邮件** |
| Qdrant | 只用 6335 端口（基准专用实例），集合名 `e2e_*`；`e2e_*` **不在**自动删除白名单内，所以脚本无法顺手删掉别的集合 |
| 端口 | 每个模式独立端口（18988 / 18990 / 18991 / 18992 / 18993），跑完即停 |

⚠️ **一处需要如实说明的副作用**：一次临时排障脚本（已在结束后删除）没有指定 `AIEB_EMBEDDING_CACHE_DIR`，
因此按默认路径写入了 `.aieb/cache/embeddings/`（向量结果缓存 + 已下载的 bge 模型文件）。
它**只影响缓存目录**，不涉及知识库 SQLite、上传原件、会话、记忆；但这确实是我动了正式 `.aieb/` 下的一处目录，
记录在此而不是略过。后续所有 E2E 运行都显式把缓存指向 `benchmarks/.work/emb_cache_cold`。

---

## 2. 测试文档与问题设计

上传的是一份 3 页 PDF（`采购管理制度.pdf`，489 字符），刻意埋了三类陷阱：

| 页码 | 内容 | 针对的能力 |
| --- | --- | --- |
| P1 | 单一合同 > **95 万** 需双签；质保金 **5%**；项目 `PRJ-2024-123` 预算 120 万 | 单页事实 |
| P2 | `PRJ-2024-1230` 预算 **340 万**；并明确写"与 PRJ-2024-123 勿混淆" | **相似编号**（`…123` vs `…1230`） |
| P3 | 逾期违约金累计封顶 **10%**，并引用"第二条质保金" | **跨页多证据**（需同时用 P1+P3） |

修订版 `采购管理制度_v2.pdf` 只改一个数字（95 万 → **88 万**），用于验证"内容改了，检索结果跟着改"。

---

## 3. 四模式结果（真实 Chrome 驱动）

每条问题都核对两件事：**答案里有没有关键事实** + **回答正文是否带 `[来源：文档 · 第N页]` 引用**。

四种模式的实测回答（完全一致，说明四种检索在"清晰的单文档问答"上都能给出正确结果）：

| 问题 | 期望 | 四种模式的回答 |
| --- | --- | --- |
| 单份合同 > 多少万需双签 | 95 | 「…超过 **95** 万元时，须由部门负责人与财务负责人共同签批。[来源：采购管理制度.pdf · **第1页**]」 |
| `PRJ-2024-1230` 预算 | 340 | 「项目 PRJ-2024-1230 的预算为 **340** 万元。[来源：… · **第2页**]」（未混成 120 万） |
| 违约金封顶 + 质保金 | 10 / 5 | 「违约金累计封顶 **10%**[来源：… · 第3页]；质保金 **5%**[来源：… · 第1页]」 |

同步一致性（Qdrant 只读观测，`count` + `scroll` 统计每个 `document_id` 的向量数）：

| 动作 | 观测 |
| --- | --- |
| 上传 P1 | 1 份文档 / 3 个 chunk / 3 个向量点 |
| **同字节再上传一次** | 文档数仍 **1**、向量点仍 **3** → **不重复生成向量记录**（`chunk_key` 幂等） |
| 上传修订版 | 文档数 **2**（旧版仍在，需人工删除，这是当前产品语义，不是 bug） |
| 选中新版提问 | 答案变为 **88 万** → 新内容已进入检索 |
| 删除旧版 | 向量点从 6 → **3**（只留新版）；再提问仍为 **88 万**，**95 万不再出现** → SQLite / BM25 / Qdrant 三处同步清除 |

---

## 4. Qdrant 故障时的受控降级（本轮抓到并修复的缺陷）

**做法**：把服务的 `AIEB_QDRANT_URL` 指向一个没有监听的端口（`127.0.0.1:6399`），其余完全一致。

| 检查项 | 修复前 | 修复后 |
| --- | --- | --- |
| 上传是否被向量库故障阻塞 | ❌ 不阻塞（✅ 本来就正确） | ✅ 不阻塞，业务优先 |
| 是否仍能回答 | ✅ 走关键词检索给出 95 万 + 页码引用 | ✅ 同左（受控降级，不是整页报错） |
| 界面是否提示"已降级" | ❌ **完全没有任何提示**，用户会把残缺结果当完整结果 | ✅ 显示黄色横幅 |
| 失败原因是否可读 | — | ✅ 「检索已降级：VectorStoreError: 连接 Qdrant 失败（http://127.0.0.1:6399）：…由于目标计算机积极拒绝…」 |

后端其实**一直**在 `RetrievalResult` 里如实标注了 `degraded=True` + `degraded_reason`（探针实测确认），
但信息在两处被丢掉：① `ops.op_knowledge_query` 的知识库分支自己拼响应体，没带 `retrieval` 轨迹；
② 前端 `renderDocDetail` 从不读 `res.retrieval`。两处都已修。

---

## 5. Qdrant 集合删除保护（护栏）

**代码**：`QdrantVectorStore.drop(*, allow_any=False)` 默认只允许删除前缀为
`bench_` / `scale_` / `hybrid_` / `emb_` / `pytest_` / `test_` 的集合；其它名字直接抛 `VectorStoreError`，
**连客户端都不会被调用**。`VectorIndexer.rebuild()`（用户主动整库重建）显式传 `allow_any=True`。

**真机验证**（对 6335 实例，未删除任何集合）：

| 尝试删除 | 结果 |
| --- | --- |
| `aieb_chunks` | ✅ 拒绝（`不在允许自动删除的前缀 … 内`） |
| `aieb_business` | ✅ 拒绝 |
| `prod_vectors` | ✅ 拒绝 |
| 实例上原有 13 个集合 | ✅ 前后列表**完全一致**（未受影响） |

单测：`tests/test_vector_drop_guard.py`（12 个用例，含"白名单集合必须逐字匹配"的防篡改断言）。

---

## 6. 本轮 E2E 发现并修复的缺陷

| # | 缺陷 | 影响 | 修复 |
| --- | --- | --- | --- |
| 1 | 知识库问答分支（`ops.op_knowledge_query` 带 `doc_ids` 时）自建响应体，**丢掉检索轨迹** | 界面无法得知是否降级 | 响应体加 `retrieval` |
| 2 | 前端从不读 `retrieval.degraded`，**静默降级** | 用户把残缺结果当完整结果 | 新增 `degradationBanner()`，显示"检索已降级 + 原因 + 本次仅用了哪一路" |
| 3 | 上传时向量同步失败只写进 `vector_sync.errors`，界面只提示"解析完成" | 用户以为向量检索可用 | `op_documents_upload` 把同步错误转成 `warning`，前端已有的 warn toast 会显示 |
| 4 | 知识库“基于本文档提问”走 `kb.build_context()`（字符预算 + break），**绕过 token 预算** | 单文档与全局问答上下文口径不一致 | 统一改为 `service.build_context()`，并把 `context_meta` / 真实 token 用量返回给前端（详见 `docs/HYBRID_RAG_ARCHITECTURE.md` §6.1） |

> 缺陷 4 已在收尾验收中修复（原文档把它列为“待办”）。
> 修复后的口径：单文档与全局问答使用同一套 `AIEB_CONTEXT_BUDGET` / `context_max_tokens` 规则，
> 不再存在“换个入口进去上下文策略就变了”的情况。

---

## 7. 已验证 / 尚未验证（必须一起看）

### 已验证

* 真实 Chrome → 真实服务 → 真实 Qdrant → 真实 LLM，四种模式全链路可用；
* PDF 上传、解析（3 页 / 489 字符）、页码引用正确；
* 相似编号（`PRJ-2024-123` vs `PRJ-2024-1230`）**未被混淆**；
* 跨页多证据问题能同时引用两页；
* 同字节重复上传**不产生重复向量**；修订版内容**立即生效**；删除后**三处同步清除**且旧值不再出现；
* Qdrant 不可达时：上传不被阻塞、回答受控降级、**界面明确提示降级且原因可读**；
* 集合删除护栏在真机上拒绝删除业务集合，且未影响任何既有集合；
* 会话历史：刷新页面后当前会话的提问与回答能正确重放（`session_history_restored=true`）。

### 尚未验证（不要当成已验证）

| 未验证项 | 原因/说明 |
| --- | --- |
| 多用户 / 权限（`scope_id`、`knowledge_base_id` 隔离）的**界面级**验证 | 单机单用户 Demo，界面没有多空间概念；隔离只有单测覆盖，**且系统并未实现多租户** |
| 大文件（>15MB）、扫描件 OCR、DOCX | 本轮只测 PDF；OCR 产品明确未启用 |
| 语义/中文分词对真实企业文档的效果 | 只测了 1 份 3 页小文档，**不代表** `docs/HYBRID_RAG_BENCHMARK.md` 的 320 题留出集结论以外的场景 |
| 高并发 / 压力测试 | 只做了“少量并发”的线程级回归（4–8 线程、单进程）；未做压测、未验证多进程部署 |
| 真实邮件发送 | **按要求禁止**，未测 |
| 长时运行稳定性、内存泄漏 | 未测（每轮 E2E 只跑 30–60 秒） |

---

## 8. 复现

```powershell
# 密钥只进进程环境，不落盘（务必按此方式）
$env:DEEPSEEK_API_KEY = ((Get-Content .env | Where-Object { $_ -match 'DEEPSEEK_API_KEY' } | Select-Object -First 1) -split '=',2)[1].Trim()

# 四种模式（hybrid 额外加 --session-check 验证会话历史恢复）
python scripts\e2e_hybrid_browser.py --work-dir benchmarks/.work/e2e_hybrid   --port 18988 --mode hybrid        --collection e2e_hybrid --session-check
python scripts\e2e_hybrid_browser.py --work-dir benchmarks/.work/e2e_bm25     --port 18990 --mode bm25          --collection e2e_bm25
python scripts\e2e_hybrid_browser.py --work-dir benchmarks/.work/e2e_vector   --port 18991 --mode vector        --collection e2e_vector
python scripts\e2e_hybrid_browser.py --work-dir benchmarks/.work/e2e_rerank   --port 18992 --mode hybrid_rerank --collection e2e_rerank

# Qdrant 故障降级
python scripts\e2e_hybrid_degraded.py --work-dir benchmarks/.work/e2e_degraded --port 18993

# 删除护栏
python -m pytest tests\test_vector_drop_guard.py -q
```

前置：Qdrant 需在 `http://127.0.0.1:6335` 运行；本机需安装 Chrome（Playwright 用 `channel="chrome"`）。
每个脚本都以"检查项全通过"为退出码，失败时会把服务端日志尾部写进对应 JSON。
