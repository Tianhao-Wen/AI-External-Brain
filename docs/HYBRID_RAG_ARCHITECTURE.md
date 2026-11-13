# 混合检索（Hybrid RAG）架构说明

> 代码：`aieb/brain/retrieval/`（9 个模块）+ `aieb/brain/chunkkey.py` + `aieb/brain/store.py` 迁移
> 测试：`tests/test_hybrid_fusion.py`、`test_hybrid_modes.py`、`test_hybrid_qdrant_integration.py`、`test_bm25_equivalence.py`
> 数据与结论见 `docs/HYBRID_RAG_BENCHMARK.md`、`docs/HYBRID_RAG_SCALE_TEST.md`

---

## 1. 一句话总结

在**不替换、不削弱**原有 BM25 的前提下，新增 Embedding + Qdrant(HNSW) 向量检索、
RRF 融合与 cross-encoder 重排，并把 Top-K / 上下文预算 / 索引同步全部做成**可配置、可观测、可回退**的工程实现；
默认仍是 `bm25`，未验证的路径不会悄悄进入生产。

---

## 2. 为什么要做这件事（问题不在"没有向量库"）

审计阶段（`docs/HYBRID_RAG_ARCHITECTURE_AUDIT.md`）确认了四个真实缺陷：

| # | 缺陷 | 后果 |
| --- | --- | --- |
| 1 | 纯 BM25，无语义召回 | 用户换个说法（"报账" vs "报销"）就检索不到 |
| 2 | `chunks.id` 是随机主键 | 无法给外部索引做幂等键，重复上传会重复建向量 |
| 3 | 每次上传全量 `rebuild()` | 批量入库退化成 O(N²) |
| 4 | `build_context` 早停 + 只按字符算 | Top-K 与"真正进模型的块数"脱钩 |

因此本次改造的目标不是"换成向量库"，而是把**检索链路工程化**。

---

## 3. 整体架构

```
                        ┌──────────────────────────────────────────┐
  用户问题 ──────────▶  │ BrainService.search() / answer()          │
                        │  · mode=bm25      → 原 BM25Index（逐位等价）│
                        │  · mode=vector    → Qdrant                │
                        │  · mode=hybrid    → BM25 + Qdrant + RRF   │
                        │  · mode=hybrid_rerank → 再加 cross-encoder │
                        └───────────────┬──────────────────────────┘
                                        │
        ┌───────────────────────────────┼────────────────────────────────┐
        │                               │                                │
 ┌──────▼───────┐              ┌────────▼────────┐              ┌────────▼─────────┐
 │ BM25Index     │              │ VectorRetriever  │              │ (未实现) Sparse  │
 │ 词项精确匹配  │              │ HNSW + payload   │              │ 留作后续扩展     │
 │ 缓存 tf/倒排  │              │ scope 过滤        │              │                  │
 └──────┬───────┘              └────────┬────────┘              └──────────────────┘
        │                               │
        └───────────────┬───────────────┘
                        ▼
              ┌──────────────────┐        ┌────────────────────────┐
              │ RRF 融合 1/(k+r)  │───────▶│ Reranker (cross-encoder)│
              └────────┬─────────┘        └───────────┬────────────┘
                       │                              │
                       ▼                              ▼
              ┌────────────────────────────────────────────┐
              │ ContextBuilder：token 预算 + 四层计数        │
              │ Retrieved / Selected / Included / Tokens    │
              └────────────────────────────────────────────┘
```

模块职责：

| 模块 | 职责 | 关键设计 |
| --- | --- | --- |
| `config.py` | 环境变量配置 | 默认 `bm25`；非法值直接报错而不是静默降级 |
| `embedding.py` | ONNX Embedding + SQLite 缓存 | 缓存键 `(model_version, content_hash)`；线程数显式设置 |
| `vector_store.py` | Qdrant 封装 | 确定性点 ID、服务端 payload 过滤、批次写入、明确错误 |
| `index.py` | SQLite → Qdrant 同步 | SQLite 是唯一真源，向量索引可随时重建 |
| `fusion.py` | RRF | 只用名次，不用分数；跨路去重；保留来源与名次 |
| `rerank.py` | cross-encoder 重排 | 独立超时线程、降级可追踪、不把分数当概率 |
| `context.py` | 上下文预算 | token 预算、去重、可截断但不早停、四层埋点 |
| `service.py` | 统一入口 | 四模式分流、降级原因显式透出 |

---

## 4. 技术选型：为什么是这些（附实测依据）

### 4.1 为什么保留 BM25

不是"兼容旧代码"，而是**它有结构性优势**，实测也证明了：

| 模式 | Recall@1 | Recall@5 | 说明 |
| --- | --- | --- | --- |
| BM25 | **0.384** | **0.490** | 编号/条款/公司名这类精确词项，BM25 最强 |
| 纯向量（bge-small-zh） | 0.109 | 0.372 | 同义改写强，但精确词项弱 |
| Hybrid(RRF) | 0.277 | 0.483 | 融合后 R@10 最高（0.601），R@1 被向量拉低 |

数据里充满 `CG-2024-001` 这类编号、`违约金累计上限` 这类固定表述 —— 这恰恰是 BM25 的主场。
删掉 BM25 换向量库，等于主动放弃这部分召回。

### 4.2 为什么不引 torch 而走 ONNX

* 本机可用显存 8GB，torch + 大模型推理栈部署负担重；
* `onnxruntime` 已在依赖中，CPU 即可跑；
* fastembed 的模型注册表带**许可与体积**信息，选型可核查（见下表）。

### 4.3 Embedding 选型（实测对比，不是"越新越好"）

同一份 750 chunk 语料、同一批 267 题、同一套评测器：

| 模型 | 维度 | 许可 | 体积 | 纯向量 R@5 | 纯向量 MRR@10 | 单次检索 P50 |
| --- | --- | --- | --- | --- | --- | --- |
| `BAAI/bge-small-zh-v1.5` | 512 | MIT | 0.09 GB | 0.372 | 0.272 | **15 ms** |
| `Qwen/Qwen3-Embedding-0.6B-Q` | 1024 | Apache-2.0 | 1.12 GB | **0.437** | **0.291** | 550 ms |

结论：**Qwen3 质量更高（R@5 +6.5pt），但单次查询编码慢 37 倍**。
默认选 `bge-small-zh-v1.5`（延迟敏感场景），Qwen3 作为可切换的高精度档；
两者都通过 `AIEB_EMBEDDING_MODEL` 一行配置切换，不需要改代码。

> 未选 `BAAI/bge-m3`：fastembed 不支持，需要 `FlagEmbedding + torch`，本机环境不满足。
> 未选 `jina-embeddings-v3` / `jina-reranker-v2`：**CC-BY-NC-4.0 非商用许可**。

### 4.4 Reranker 选型（实测双击）

| 模型 | 许可 | 20 候选重排耗时 | 结论 |
| --- | --- | --- | --- |
| `BAAI/bge-reranker-v2-m3-int8` | Apache-2.0 | 4.1 s（205 ms/对） | 质量好但太慢 |
| `BAAI/bge-reranker-base` | MIT | **0.7 s（35 ms/对）** | **选它** |

线程数也做了实测（32 核机器）：`threads=32` 反而比 `threads=8/16` 慢 2.5 倍（调度开销），
最终固定 `min(16, cpu_count)`。这一步把单次重排从 20 s 降到 2 s 级。

### 4.5 Qdrant 与 HNSW 的实际作用

* Qdrant 提供**持久化 + 服务端过滤 + HNSW ANN**，我们不用自己维护内存字典；
* 点 ID 用 `uuid5(chunk_key)`，**同一份文件重复上传/重复同步天然幂等**；
* `payload` 里存 `scope_id` / `knowledge_base_id`，**过滤在服务端执行**（不是拉回来再筛）；
* **一个容易踩的坑**：Qdrant 默认 `full_scan_threshold=10000`，当每个 segment 的点数少于它时会**暴力全扫、根本不建 HNSW**。本项目显式设为 1000，否则"用了 HNSW"只是配置里的字样。
  实测该配置生效后，75,000 点规模向量检索 P50 从 24 ms 降到 19 ms、P95 从 43 ms 降到 30 ms。

### 4.6 RRF 融合原理

$$\text{RRF}(d)=\sum_{r}\frac{w_r}{k+\text{rank}_r(d)}$$

* 只用**名次**，所以不需要把 BM25 分数（无界正数）和余弦相似度（[-1,1]）强行归一化；
* `k` 越小越奖励"单路第一名"，越大越奖励"两路都靠前"；
* 本项目 `k=60`（常用起点），并做了 k 的敏感性验证（见 `tests/test_hybrid_fusion.py` 的 `k=1 vs k=60` 对照）；
* **必须用统一的 `chunk_key` 去重**，否则同一段文字会被两路各算一次分。
* 明确没有做的一件事：**没有把两路分数直接相加**，也没有使用 Qdrant 原生 sparse/hybrid（那会与"BM25 基线"不可比）。

---

## 5. 数据一致性与隔离

### 5.1 稳定标识

```
document_key = sha256(文件字节)[:16]          # 同一份文件必然同键
content_hash  = sha256(标准化(chunk 文本))[:16] # 同一段文字必然同键
chunk_key     = f"{document_key}:{page}:{content_hash}"
point_id      = uuid5(命名空间, chunk_key)
```

`documents` / `chunks` 表通过**轻量迁移**（`ALTER TABLE ADD COLUMN`）补上
`document_key` / `scope_id` / `content_hash`，不重建表、不搬数据，正式 `.aieb/` 里的旧库也能直接用。

### 5.2 双索引同步

| 操作 | SQLite | BM25 | Qdrant |
| --- | --- | --- | --- |
| 上传新文档 | 新增 | **增量追加**（不再全量 rebuild） | `upsert`（幂等） |
| 重复上传同一文件 | **返回已存在记录**（`reused=True`） | 不变 | 不变 |
| 删除文档 | 级联删 chunk | `invalidate()` + 重建 | 按 `document_id` / `document_key` 删除 |
| 换 Embedding 模型 | 不变 | 不变 | **必须换集合名或重建**（维度不一致会直接报错） |

失败策略：向量同步失败**不回滚 SQLite**，只记录失败原因；
`VectorIndexer.rebuild()` 是明确的恢复路径（丢弃集合并从 SQLite 全量重建）。

**并发（第一阶段收尾时补强）**：

| 共享状态 | 保护方式 |
| --- | --- |
| SQLite | 单连接 + `check_same_thread=False` + `RLock` + WAL |
| BM25 内存索引 | `threading.RLock`：`rebuild` / `add_chunks` / `invalidate` / `_score` 共用一把可重入锁 |
| Qdrant | 服务端保证；幂等 point id（`uuid5(chunk_key)`） |

不加锁时的真实风险：删除触发 `rebuild()` 与上传触发 `add_chunks()` 并发，会让 `_docs`
与 `_lengths`/postings 长度错位，打分时直接越界或返回脏结果。
回归用例：`tests/test_rag_isolation_concurrency.py`（并发上传/检索/删除 + 并发 rebuild/add）。

> 代价：BM25 打分与写操作互斥。单次查询 17 ms 的量级下这不是瓶颈；
> 但如果将来要恢复多线程并行的吞吐，需要改成“写时复制快照 + 读不加锁”，那是下一阶段的事，本轮不做也不声称已做。

### 5.3 访问隔离（诚实版）

* 当前**没有多租户登录体系**，不假装已经上线；
* 已建立的隔离能力：
  * 文档级 `scope_id` 字段 + `document_ids_in_scope()`；
  * 向量路：服务端 payload 过滤（`scope_id` / `knowledge_base_id` / `document_ids`）；
  * **BM25 路（包括默认的 `bm25` 模式）：应用层先算“允许的 doc_id”再检索**；
    空集合用哨兵 `__no_document_in_scope__` 表达“一个都不允许”，
    否则“空集合 = 不过滤”会把整库吐出来（这是本轮修掉的真实缺陷）；
  * 显式 `doc_ids` 参数同样与作用域取交集，不能绕过。
* 回归用例：`test_bm25_default_mode_honours_scope`、`test_explicit_doc_ids_cannot_bypass_scope`、
  `test_vector_route_receives_document_filter`、`test_bm25_route_honours_scope_filter`。

> 结论口径：**“单进程 + 单作用域配置”下的隔离已验证；多租户（多用户同时在线、每用户一个作用域）未实现、未验证。**

### 5.4 破坏性操作护栏（删除集合）

`QdrantVectorStore.drop()` 默认**只允许删除**前缀为
`bench_` / `scale_` / `hybrid_` / `emb_` / `pytest_` / `test_` 的集合；
其它名字直接抛 `VectorStoreError`，**连 Qdrant 客户端都不会被调用**。

绕过路径检查：

| 调用点 | 是否可删任意集合 | 说明 |
| --- | --- | --- |
| `VectorIndexer.rebuild()` | 是（`allow_any=True`） | 用户/维护者主动发起的整库重建，目标是当前配置的集合本身 |
| RPC / UI handler（`aieb/brain/ops.py`） | **否** | 源码中不存在 `vector_indexer` 与 `drop(` 调用，有回归用例把关 |
| 基准脚本（ablations / runners） | 否 | 集合名都以白名单前缀开头 |

真机验证：对 6335 实例尝试删除 `aieb_chunks` / `aieb_business` / `prod_vectors`，
全部被拒且实例原有 13 个集合前后完全一致；单测 `tests/test_vector_drop_guard.py`（12 个用例）。

---

## 6. 上下文预算：把"检索到"与"喂进去"分开

旧实现：

```python
for hit in hits:                     # 遇到放不下的就 break
    if used + len(block) > 6000: break
```

新实现（`build_budgeted_context`，`AIEB_CONTEXT_BUDGET=budgeted` 时启用）：

* 按 **token** 预算（默认 1800）而不是字符；字符预算保留作并行约束；
* 放不下的块**跳过继续尝试**，必要时按剩余预算截断（带 `…` 标记）；
* 按 `content_hash` 去重；
* 输出四层计数：`retrieved / selected / included / dropped / estimated_tokens / budget_utilisation`；
* 保证不变式：`estimated_tokens <= max_tokens`（收尾时会回退尾部块）。

token 估算用实测校准的 `chars_per_token = 1.74`（DeepSeek 上 3221 字符 ≈ 1851 tokens）。
它是**估算**，报告里同时给出 provider 回传的真实 `prompt_tokens` 做对照。

> 默认仍是 `legacy`：切换是**行为变更**，应由数据决定（见 `docs/HYBRID_RAG_BENCHMARK.md` 的结论）。

### 6.1 单文档问答曾走另一条路径（已统一）

收尾验收时发现：知识库页的“基于本文档提问”走的是 `ops.op_knowledge_query` 的
`doc_ids` 分支，它直接调 `knowledge.build_context()`（**字符预算 + 遇长则 break**），
绕过了 `AIEB_CONTEXT_BUDGET`，于是“单文档问答”和“全局问答”的上下文口径不一致 ——
同一个问题，换个入口进去看到的上下文预算、去重与截断策略都不一样。

现在两条路径统一为 `BrainService.build_context()`：

| 能力 | 单文档问答 | 全局问答 |
| --- | --- | --- |
| 预算口径 | `AIEB_CONTEXT_BUDGET` + `context_max_tokens` | 同左 |
| 不早停 / 按内容哈希去重 | ✅ | ✅ |
| 四层计数（retrieved/selected/included/tokens） | ✅ 随响应返回 `context_meta` | ✅ |
| 页码 + `chunk_key` 引用 | ✅（`format_citations` 新增 `chunk_key`） | ✅ |
| 真实 token 用量 | ✅ `llm.complete(usage_out=...)` 回传 provider 计量 | 部分（流式路径未汇总） |

回归用例：`tests/test_rag_isolation_concurrency.py` 的前 6 个用例
（四层计数、同预算、跨页多证据、长上下文不早停不重复、legacy 兼容、bm25 兼容）。

---

## 7. 配置项（全部可用环境变量覆盖）

| 变量 | 默认 | 作用 |
| --- | --- | --- |
| `AIEB_RETRIEVAL_MODE` | `bm25` | `bm25｜vector｜hybrid｜hybrid_rerank` |
| `AIEB_CONTEXT_BUDGET` | `legacy` | `legacy｜budgeted` |
| `AIEB_CONTEXT_MAX_TOKENS` | 1800 | 新预算的 token 上限 |
| `AIEB_QDRANT_URL` | `http://127.0.0.1:6333` | Qdrant 地址 |
| `AIEB_QDRANT_COLLECTION` | `aieb_chunks` | 集合名（换模型必须换集合） |
| `AIEB_EMBEDDING_MODEL` | `BAAI/bge-small-zh-v1.5` | Embedding 模型 |
| `AIEB_RERANK_MODEL` | `BAAI/bge-reranker-base` | 重排模型 |
| `AIEB_RERANK_CANDIDATES` | 10 | 重排候选上限 |
| `AIEB_RERANK_MAX_CHARS` | 256 | 送进 cross-encoder 的片段长度 |
| `AIEB_RRF_K` | 60 | RRF 融合常数 |
| `AIEB_RETRIEVAL_CANDIDATES` | 30 | 每路召回候选数 |
| `AIEB_RETRIEVAL_SCOPE` | `default` | 作用域 |

启动 Qdrant：

```bash
docker compose up -d qdrant            # 标准路径
# 无 Docker 的 Windows：直接用官方二进制
tools\qdrant\qdrant.exe                # 需先下载 qdrant-x86_64-pc-windows-msvc.zip
```

---

## 8. 实际缺陷与解决方法（本次真实踩到的）

| # | 缺陷 | 现象 | 解决 |
| --- | --- | --- | --- |
| 1 | `chunks.id` 随机 | 重复上传会产生新主键，外部索引无法幂等 | 新增 `chunk_key`（内容哈希派生）+ 上传去重 |
| 2 | BM25 每次上传全量重建 | 批量入库 O(N²) | `add_chunks()` 增量追加 |
| 3 | BM25 查询时重建 `Counter` | 1,500 块 P50 已 240 ms，线性外推不可用 | 建索引时缓存 `tf/长度` + 倒排候选剪枝，**逐位等价**（21 条等价测试） |
| 4 | openai-compat 无 usage | Token 成本无法统计 | （上一阶段已修）`StreamEnd` 回传 usage |
| 5 | Embedding 默认线程数 | 实测仅 ~12 texts/s | 显式 `threads=min(16, cpu)` → 独立测试 85~93 texts/s |
| 6 | Reranker 长片段 | 20 候选重排 20 s+ | 片段截断 256 字符 + 换 `bge-reranker-base` + 线程调优 → 2 s 级 |
| 7 | Qdrant 默认全扫阈值 | 5 万点仍不建 HNSW，"用了 HNSW"名不副实 | `full_scan_threshold=1000` |
| 8 | 预算早停 | Top-10 只有 8 块进 prompt | `build_budgeted_context`（可截断、不早停、四层埋点） |

---

## 9. 资源与成本限制（实测）

| 项目 | 数值 |
| --- | --- |
| 本机 | Windows 11 / 32 逻辑核 / RTX 4060 Laptop 8GB / Python 3.14.2 |
| Qdrant | 官方 1.20.0 Windows 二进制（本机无 Docker），单机单分片 |
| Embedding | bge-small-zh-v1.5（512 维，ONNX CPU） |
| 索引 75,000 chunk | BM25 重建 18.8 s（≈4,000 chunk/s）；Embedding 60,000 条新文本 1,438 s（≈42 texts/s，与 Qdrant 并发时）；Qdrant 写入 34.9 s（≈2,150 points/s） |
| 内存 | 75,000 chunk 时 RSS ≈ 3.1 GB（BM25 结构）；进程峰值 ≈ 3.7 GB |
| 磁盘 | SQLite 149 MB、上传副本 83 MB、Embedding 缓存 389 MB（75,000 条向量，可重建） |
| 检索延迟 | 75,000 chunk：BM25 P50 605 ms / 向量 P50 19 ms / Hybrid P50 619 ms |

**成本结论**：本方案的钱主要花在 **Embedding 计算**与 **BM25 的 CPU 打分**上，不在 Qdrant 上。
Embedding 有磁盘缓存（同一文本只算一次），所以增量场景成本可控；冷启动全量索引是主要开销。

---

## 10. 面试官可能追问的技术细节

**Q：为什么不直接把 BM25 换成向量检索？**
A：实测纯向量 R@1 只有 0.109，BM25 是 0.384。本项目语料里大量是编号、条款号、公司名这类精确词项。
所以是"加一路"而不是"换一路"，`mode=bm25` 仍可一键退回，且**逐位等价于改造前**。

**Q：RRF 的 k 怎么定的？取了多大？**
A：k=60，常用起点；我做过 k=1 与 k=60 的对照（`tests/test_hybrid_fusion.py`），
k 小会放大单路第一名的优势。真正的选择应该由开发集决定，我把参数暴露成 `AIEB_RRF_K` 而不写死。

**Q：融合时怎么保证不是把 BM25 和余弦相似度硬加？**
A：RRF 只用名次。代码里 `rrf_fuse` 的输入是两路**已排序**的列表，
没有任何一处把 `score` 相加；`test_rrf_uses_rank_not_raw_scores` 把 BM25 分数放大 1000 倍，融合结果不变。

**Q：HNSW 真的生效了吗？**
A：一开始并没有 —— Qdrant 默认 `full_scan_threshold=10000`，segment 点数不够就全扫。
我把阈值调到 1000 并重新测量：75,000 点向量检索 P50 从 24 ms → 19 ms、P95 43 ms → 30 ms。
如果只看配置里写了 `m=16` 就宣称用了 HNSW，是自欺。

**Q：重复上传同一份 PDF 会产生重复向量吗？**
A：不会。点 ID 是 `uuid5(document_key:page:content_hash)`，且上传层按文件内容哈希直接返回已存在记录
（`reused=True`）。`test_upsert_is_idempotent_for_same_content` 连续 upsert 三次仍然只有 1 个点。

**Q：删除文档后向量会不会残留？**
A：`delete_document` 同时删 SQLite（级联 chunk）、BM25（invalidate + 重建）、Qdrant（`document_id` 与 `document_key` 双重过滤删除）。
集成测试校验删除后计数下降、且原文档内容不再被召回。

**Q：token 预算怎么估的？准吗？**
A：`chars_per_token=1.74`，由"3221 字符 → 1851 prompt tokens"实测校准。
它只是预算用的估算器；报告里的成本数字用的是 provider 回传的真实 `prompt_tokens`。
两者会有偏差（含英文/表格时更大），所以报告同时给出两个口径。

**Q：为什么 `AIEB_RETRIEVAL_MODE` 默认还是 bm25？**
A：因为混合检索在本数据集上**并没有全面超过 BM25**（见 BENCHMARK 报告：R@1 甚至更低）。
默认值必须是"已知不退化"的那个；更好但未证明的方案通过配置启用，由使用方按场景选择。

---

## 11. 未来可做（本次刻意不做）

1. **稀疏检索（SPLADE/BM42）**：现在只有 BM25 + dense，三者融合可能更好，但要先对齐"BM25 基线"的比较口径。
2. **GPU 推理**：本机有 4060，但 onnxruntime-gpu 需要 CUDA 依赖；CPU 上重排 2 s/次已是可用边界。
3. **异步索引队列**：现在上传即同步向量化（保证一致性但阻塞）；生产应改成队列 + 状态机。
4. **真正的多租户**：`scope_id` 已就位，但登录/鉴权体系没做，不宣称已完成。
5. **Reranker 蒸馏或 token 级裁剪**：进一步压延迟。
