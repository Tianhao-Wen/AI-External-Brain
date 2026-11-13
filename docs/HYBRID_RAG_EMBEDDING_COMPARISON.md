# 向量模型对比（dev 切分）

> 数据：`benchmarks/results/hybrid_rag/emb_compare_*.json`（质量/吞吐）、`emb_load_*.json`（离线加载）
> 脚本：`python -m benchmarks.ablations.embedding_compare --model <name> --split dev --cache-dir benchmarks/.work/emb_cache_cold --fresh-cache`
> 结论口径：**dev 切分（163 题，其中 160 道有答案）**。**没有用冻结留出集做模型选择**，留出集只跑最终一轮。

---

## 0. 结论

**继续使用 `BAAI/bge-small-zh-v1.5` 作为默认模型，不换。**

理由不是"它质量最高"（它不是），而是**性价比**：更大的模型换来的检索增益，
被索引成本、内存与查询延迟的涨幅吃掉了大头，而 **Hybrid 融合已经把差距压得很小**。

| 模型 | 维度 | 磁盘 | 许可 | 混合检索 R@5 | vs 默认 | 索引吞吐 | 查询嵌入延迟 | 峰值内存 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| **`BAAI/bge-small-zh-v1.5`（默认）** | 512 | 0.09 GB | MIT | 0.716 | — | **44.8 texts/s** | **11.6 ms** | **427 MB** |
| `jinaai/jina-embeddings-v2-base-zh` | 768 | 0.64 GB | Apache-2.0 | 0.738 | +2.2 pt | 14.3 | 24.0 ms | 984 MB |
| `Qwen/Qwen3-Embedding-0.6B-Q` | 1024 | 1.12 GB | Apache-2.0 | 0.713 | **−0.3 pt** | 2.63 | **591 ms** | 1,848 MB |
| `intfloat/multilingual-e5-large` | 1024 | 2.24 GB | MIT | 0.722 | +0.6 pt | 5.58 | 47.0 ms | 1,782 MB |

必须点明两件容易误读的事：

1. **"更新的模型"并没有带来更好的混合检索效果**。Qwen3-Embedding（2025 年发布）
   在同样语料、同样 chunk、同样评分口径下，混合检索 R@5 **比默认还低 0.3 个百分点**，
   而查询嵌入延迟是默认的 **51 倍**（591 ms vs 11.6 ms）、索引吞吐只有 **1/17**。
2. **纯向量检索时大模型确实更好**（见 §2，e5-large R@5 +11.6 pt、MRR +11 pt），
   但**加到 Hybrid 之后收益被 BM25 分流掉大半**（R@5 只 +0.6 pt）。
   这是本方案选择 Hybrid 的另一个副作用：**降低了对单个向量模型的依赖**。

**如果将来要开"质量优先"档**：`intfloat/multilingual-e5-large` 是唯一值得考虑的一个
（MIT 许可、Hybrid MRR@10 0.632 vs 0.572、R@10 0.900 vs 0.872），
代价是索引慢 8 倍、内存 +1.35 GB、单查询 +35 ms。用 `AIEB_EMBEDDING_MODEL` 切换即可，无需改代码。

---

## 1. 候选核查（先核查再跑，不预设）

| 候选 | fastembed 注册表 | 维度 | 体积 | 许可 | 是否纳入 |
| --- | --- | --- | --- | --- | --- |
| `BAAI/bge-small-zh-v1.5` | ✅ 在册 | 512 | 0.09 GB | MIT | ✅ 基线 |
| `jinaai/jina-embeddings-v2-base-zh` | ✅ 在册 | 768 | 0.64 GB | Apache-2.0 | ✅ 纳入 |
| `Qwen/Qwen3-Embedding-0.6B` / `-Q` | ✅ 在册（ONNX） | 1024 | 2.38 / 1.12 GB | Apache-2.0 | ✅ 纳入（取 `-Q`，理由见下） |
| `intfloat/multilingual-e5-large` | ✅ 在册（ONNX） | 1024 | 2.24 GB | MIT | ✅ 纳入 |
| `BAAI/bge-m3` | ❌ **不在册** | — | — | MIT | ❌ **不可行**：需 `FlagEmbedding`+`torch`，本机 Python 3.14 无 torch 轮子；换成自建 ONNX 属于新增框架，本阶段明确不做 |
| `jinaai/jina-embeddings-v3` | ✅ 在册 | 1024 | 2.29 GB | **CC-BY-NC-4.0** | ❌ **许可不允许商用**，直接排除 |
| `jinaai/jina-reranker-v2` | ✅ 在册 | — | — | **CC-BY-NC-4.0** | ❌ 同上（reranker 侧） |

关于 `Qwen3-Embedding` 为什么取 `-Q`（量化）而不是全精度：
两者是同一个权重族的 ONNX 导出，`-Q` 体积 1.12 GB（全精度 2.38 GB）。
本机 **RTX 4060 Laptop 8 GB 显存 + 无 CUDA 版 onnxruntime**，实际是在 CPU 上跑，
全精度只会更慢、更占内存，`-Q` 是更有代表性的最优点。**没有**因为"全精度应该更好"就假设它更好。

---

## 2. 检索质量（dev 163 题 / 160 道有答案，top_k=10，同一套 gold 页）

### 2.1 纯向量检索（没有任何 BM25 参与）

| 模型 | R@1 | R@3 | **R@5** | R@10 | **MRR@10** | 全证据@5 | 全证据@10 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `bge-small-zh-v1.5` | 0.162 | 0.425 | **0.581** | 0.775 | **0.404** | 0.506 | 0.725 |
| `jina-v2-base-zh` | 0.222 | 0.444 | 0.606 | 0.853 | 0.432 | 0.563 | 0.838 |
| `Qwen3-Embedding-0.6B-Q` | 0.206 | 0.469 | 0.681 | 0.838 | 0.452 | 0.625 | 0.806 |
| `multilingual-e5-large` | **0.291** | **0.516** | **0.697** | 0.831 | **0.513** | **0.650** | 0.794 |

纯向量场景下 `e5-large` 明显最好（R@5 +11.6 pt、MRR +11 pt），`jina-v2-zh` 次之，
`Qwen3-Q` 在 R@5/MRR 上优于默认但在 R@1 只比默认高 4.4 pt。

### 2.2 混合检索（BM25 + 向量，RRF k=60）—— 这是产品实际使用的模式

| 模型 | R@1 | R@3 | **R@5** | R@10 | **MRR@10** | 全证据@5 | 全证据@10 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `bge-small-zh-v1.5` | 0.350 | 0.603 | **0.716** | 0.872 | 0.572 | 0.688 | 0.856 |
| `jina-v2-base-zh` | 0.338 | 0.584 | 0.738 | 0.884 | 0.550 | **0.731** | 0.881 |
| `Qwen3-Embedding-0.6B-Q` | 0.300 | 0.569 | 0.713 | **0.897** | 0.542 | 0.681 | 0.894 |
| `multilingual-e5-large` | **0.447** | **0.619** | 0.722 | **0.900** | **0.632** | 0.713 | **0.894** |

**这是本节最重要的一张表**：换成 10 倍大的模型，Hybrid 的 R@5 只从 0.716 变到 0.722
（+0.6 pt，在 160 道题上约等于多命中 1 道题，**在噪声范围内**），
真正稳定提升的是 **MRR@10（0.572 → 0.632）** 和 **R@1（0.350 → 0.447）**，
也就是"把正确答案顶到第一位的能力"。

> 测量噪声说明：同一条 `bge-small` 三次独立运行，MRR@10 出现过 0.401 / 0.404 / 0.404（纯向量），
> 差异来自 Qdrant HNSW 的近似检索（非确定性）。因此**小于约 0.01 的差异不应被解读为模型优劣**。

---

## 3. 工程成本（同一台机器、同一批 500 chunk，冷缓存）

| 指标 | `bge-small-zh` | `jina-v2-zh` | `Qwen3-0.6B-Q` | `e5-large` |
| --- | --- | --- | --- | --- |
| 模型加载（离线，权重已在本地） | **0.77 s** | 1.41 s | 2.28 s | 2.31 s |
| 加载后 RSS | **250 MB** | 790 MB | 1,280 MB | 1,566 MB |
| 索引 500 chunk（冷，无缓存） | **11.2 s** | 34.9 s | **189.8 s** | 89.6 s |
| 索引吞吐 | **44.8 texts/s** | 14.3 | **2.63** | 5.58 |
| 单条查询嵌入 P50 | **11.6 ms** | 24.0 ms | **591 ms** | 47.0 ms |
| 纯向量查询端到端 P50 | **24.6 ms** | 32.6 ms | 599.8 ms | 55.4 ms |
| Hybrid 查询端到端 P50（嵌入命中缓存） | 16.8 ms | 17.1 ms | 16.6 ms | 17.1 ms |
| 峰值内存（峰值工作集） | **427 MB** | 984 MB | 1,848 MB | 1,782 MB |
| 索引 75,000 chunk 的外推耗时 | **≈28 min** | ≈87 min | **≈475 min** | ≈224 min |

关于这张表的三点方法论说明（避免误读）：

1. **模型加载时间必须离线测**。带网络时同一模型会出现 0.77 s / 19.6 s / 100 s / 317 s 的乱数 ——
   那是 HuggingFace Hub 的网络探测，不是模型加载。
   所以 `emb_load_*.json` 全部在 `HF_HUB_OFFLINE=1` 下采集，权重已在本地。
2. **索引速度必须冷缓存测**。首次跑时 `bge-small` 显示 91 texts/s、`Qwen3-Q` 显示 85 texts/s，
   看 `vector_sync.computed` 才发现 500 条全是 `cache_hits`（历史实验留下的缓存）——
   这两个数字是**假的**。本表用的是 `--fresh-cache`（只清结果缓存、保留模型文件）后的真实值。
3. **Hybrid 行的嵌入延迟≈0 是缓存命中**，不是模型变快。同一批查询文本在纯向量阶段已算过一次。
   真实查询嵌入成本看 `单条查询嵌入 P50` 那一行。

---

## 4. 决策

| 选项 | 结论 |
| --- | --- |
| 是否更换默认模型 | ❌ **不换**，继续 `BAAI/bge-small-zh-v1.5` |
| 是否新增运行时依赖 | ❌ 不新增（三个候选都在 `fastembed` 注册表内，随时可切） |
| 是否需要调向量参数 | ❌ 不调。所有模型都用同一套默认参数，没有为任何模型单独调参 |
| 如何让用户自己选 | `AIEB_EMBEDDING_MODEL` 环境变量；换模型会改变 `model_version`（`name@dim`），向量缓存与 payload 自动区分 |

**为什么不在索引成本上妥协**：本产品的入库路径是"用户点上传 → 立刻可用"。
`bge-small` 让 75,000 chunk 的冷索引在半小时内跑完，`Qwen3-0.6B-Q` 需要约 8 小时 ——
后者在单机部署里是不可接受的等待，而它带来的 Hybrid R@5 增益是 **−0.3 pt**。

**什么条件下应该换**：如果后续把 embedding 迁到 GPU（`onnxruntime-gpu` 或 torch），
`e5-large` 的成本结构会完全不同，届时值得重跑一次本对比再决策 —— 但**不能用今天的 CPU 数据假装 GPU 结论**。

---

## 5. 复现

```powershell
# 每个模型一个独立进程（这样加载时间与峰值内存才是真实口径）
foreach ($m in @('BAAI/bge-small-zh-v1.5','jinaai/jina-embeddings-v2-base-zh',
                 'Qwen/Qwen3-Embedding-0.6B-Q','intfloat/multilingual-e5-large')) {
  python -m benchmarks.ablations.embedding_compare --model $m --split dev `
    --cache-dir benchmarks/.work/emb_cache_cold --fresh-cache
}

# 离线加载时间（隔离 HF 网络探测）
$env:HF_HUB_OFFLINE="1"
python -m benchmarks.ablations.embedding_compare --model <name> --load-only `
  --cache-dir benchmarks/.work/emb_cache_cold
```

限制：

1. 只用了 **dev 切分**；冻结留出集的模型对比**没有做**（也不应该为了选模型去看留出集）。
2. 只在 CPU（ONNX Runtime）上测；未测 GPU。
3. 未做领域微调（fine-tune），只比较现成权重。
4. 向量维度不同意味着 Qdrant 存储体积不同（512 / 768 / 1024 维），本表未单列，按 `n×dim×4B` 可自行换算。
