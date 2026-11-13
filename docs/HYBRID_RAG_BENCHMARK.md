# Hybrid RAG 基准评测报告

> 数据目录：`benchmarks/results/hybrid_rag/`
> 复现命令见文末第 9 节。**所有数字均来自真实运行，未做任何手工填写。**

---

## 1. 结论摘要

| 结论 | 数据支撑 |
| --- | --- |
| 纯向量检索在本企业语料上**明显弱于 BM25** | Recall@1：0.109 vs 0.384 |
| RRF 融合把 R@10 提到最高（0.601），但**牺牲了 R@1**（0.277 vs 0.384） | 见 §5 表 |
| 加 Reranker 后 **R@5 超过 BM25**（0.521 vs 0.490），排序质量最好（MRR 0.498） | 见 §5 表 |
| 到**回答层**，Hybrid 比 BM25 高 5.5pt 答对率，**但 rerank 没有进一步带来答对率提升** | 0.746 vs 0.691；rerank 仍 0.746 |
| Rerank 的代价是**检索延迟从 21 ms 涨到 ~500 ms**（P50） | 见 §5 表 |
| 因此：**默认仍保持 BM25**；混合检索通过配置启用 | `AIEB_RETRIEVAL_MODE` 默认 `bm25` |

一句话：**混合检索不是"更好"的同义词** —— 它在"证据是否进入 Top-5/10"上有收益，在"第一名是否命中"上没有，
而且延迟高出 1~2 个数量级。这个结论与"引入向量库一定能提效"的直觉相反，但它是本次实测的结果。

> ⚠️ **本节的表格全部来自已暴露数据（dev/validation/test 都被跑过，见 §1.5）。
> 权威结论请读 §1.5 的冻结留出集结果；§5~§6 的数字只能作为"同分布内部对比"参考，不能当泛化能力。**

---

## 1.5 冻结留出集结果（权威，优先读）

**为什么需要这一节**：原数据集的 `test` 切分（49 题）虽然号称冻结，但事后审计发现
它已经在四模式消融与 embedding 对比中被完整跑过（49/49 题，见
`benchmarks/results/hybrid_rag/split_exposure_audit.json`），**因此该 test 切分不能当作"未见过的数据"**。

为此新建了**独立留出集**并**一次性冻结**：

| 项目 | 数值 |
| --- | --- |
| 文档 | **120 份新 PDF**（6 家全新公司 × 10 部门 × V1/V2） |
| 页数 / 字符 | 600 页 / 94,160 字符 |
| 题目 | **320 道**（314 可答 + 6 无答案负例），qid 前缀 `HQB-` |
| 问法 | 与主数据集**不同措辞**（如"逾期→交付逾期时，违约金的累计封顶比例是多少？"） |
| 冻结凭证 | `benchmarks/datasets/hybrid_rag/holdout/FREEZE.json`，`questions_sha256=f1e4ebbe…`，`used_for_tuning=false` |
| 运行前校验 | 脚本会重算 sha256 并与 FREEZE 比对，**不一致直接中止** |
| 校验结果 | `benchmarks/results/hybrid_rag/holdout_validation.json`：**0 错误** |

### 检索层（320 题全量，其中 314 道有答案 → **分母 = 314**）

| 模式 | R@1 | R@3 | R@5 | R@10 | MRR@10 | 全证据@5 | 全证据@10 | P50 | P95 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `bm25` | **0.532** | 0.656 | 0.723 | 0.838 | **0.691** | 0.675 | 0.790 | **0.6 ms** | **1.0 ms** |
| `vector` | 0.221 | 0.476 | 0.629 | 0.798 | 0.445 | 0.596 | 0.780 | 16.8 ms | 29.2 ms |
| `hybrid` | 0.389 | 0.594 | 0.721 | **0.887** | 0.600 | **0.688** | **0.876** | 17.0 ms | 29.0 ms |
| `hybrid_rerank` | 0.521 | **0.680** | **0.734** | **0.887** | **0.692** | 0.678 | **0.876** | 543 ms | 600 ms |

### 回答层（分层 80 题 = 74 可答 + 6 负例，共 **320 次真实 LLM 调用，0 次报错**）

| 模式 | 答对率 | 端到端 | 引用 P | 引用 R | 拒答正确率 | 依据度 | ctx tokens | 回答 P50 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `bm25` | 0.635 | 0.663 | 0.534 | 0.561 | **1.000** | 1.000 | 781 | **1,496 ms** |
| `vector` | **0.770** | **0.788** | 0.524 | 0.655 | **1.000** | 1.000 | 742 | 1,561 ms |
| `hybrid` | **0.770** | **0.788** | 0.529 | 0.622 | **1.000** | 1.000 | 761 | 1,511 ms |
| `hybrid_rerank` | 0.743 | 0.763 | **0.576** | **0.696** | **1.000** | 1.000 | 757 | 2,055 ms |

### 留出集上得到的四条真实结论（与 dev 上的结论**不完全一致**）

1. **BM25 是一个极强的基线**：R@1 0.532 / MRR 0.691，**高于 hybrid（0.389 / 0.600）**，
   而延迟只有 0.6 ms。之前在 dev 上得到的"hybrid 优于 bm25"**在未见数据上不成立**（至少对 top-1 排序不成立）。
2. **Hybrid 的收益在长尾召回**：R@10 0.887 vs 0.838（+4.9 pt）、全证据@10 0.876 vs 0.790（+8.6 pt）。
   它能"把该找到的证据找齐"，但**排不到第一位**。
3. **Rerank 把 top-1 拉回 BM25 水平并超过它**（MRR 0.692 vs 0.691，R@5 0.734 vs 0.723），
   同时保住 Hybrid 的 R@10，而且**引用准确率/召回率最高**（0.576 / 0.696）。
   代价是 **543 ms P50**（约为 hybrid 的 32 倍）。
4. **回答层看，纯 BM25 明显差一截**（0.635 vs 0.770），原因看分题型表：
   `numeric` 型（80 题）向量 R@5 **0.988** vs BM25 **0.904** —— 数值/条款类问题语义召回更强，
   而它占了题目的大头。

### 必须同时给出的统计口径

* 检索层分母 **314**，比例指标的 95% 置信区间半宽约 **±5.5 pt** ——
  所以 R@1 0.532 vs 0.521（bm25 vs rerank）**是平手**，0.532 vs 0.389（vs hybrid）是**真实差距**。
* 回答层分母只有 **74**，置信区间半宽约 **±11.4 pt** ——
  **0.635 vs 0.770（+13.5 pt）只在临界显著**，引用指标同理。**不要把它当 13 个百分点的确定收益。**
* 6 道负例题上，四种模式**全部正确拒答**（`abstention_accuracy = 1.000`），
  但这只说明"没有编造"，样本量太小，不足以支撑泛化结论。
* **这一轮跑完之后没有做任何调参**；上表就是这一版代码在未见数据上的成绩。

---

## 2. 数据集（质量评测专用）

| 项目 | 数值 |
| --- | --- |
| 文档 | **100 份真实 PDF**（10 部门 × 5 主体 × V1/V2 两版） |
| 页数 | 500 页（每份 5 页：3 页正文 + 2 页干扰内容） |
| 字符数 | 78,343 |
| 题目 | **267 道**（262 可答 + 5 无答案负例） |
| 切分 | dev 163 / validation 55 / **test 49（冻结）** |
| 答案键组 | 317 组，**全部经独立校验确认出现在标注页上** |
| 重复文档 | 0 组（脚本按内容哈希检查） |

覆盖的 12 类场景与题型映射：

| 任务书要求 | 本数据集中的题型 | 题数 |
| --- | --- | --- |
| 企业制度和审批流程 | `numeric`（审批金额、时限） | 67 |
| 供应商质量管理 | `single_page`（注册资本门槛） | 20 |
| 采购与合同条款 | `numeric`（质保金比例） | — |
| 项目交付和进度 | `multi_page`（里程碑 + 纠偏） | 50 |
| 财务及预算 | `numeric`（预算上限） | — |
| 多文档交叉引用 | `cross_document` | 5 |
| 中文同义词和语义改写 | `paraphrase` | 30 |
| 产品编号相似但含义不同 | `product_code` | 50 |
| 需要多条证据联合回答 | `multi_page` / `cross_document` | 55 |
| 存在干扰内容的复杂检索 | `long_distractor` | 15 |
| 没有答案的问题 | `negative` | 5 |
| 新旧文档内容冲突 | `conflict`（V1 20% vs V2 25%） | 25 |

**如何保证答案可核验**（这是最容易造假的地方，所以做了三道校验）：

1. 文档正文与标准答案来自**同一个参数表**（`build_corpus.py` 的 `facts`），不是被测系统的输出；
2. `validate_dataset.py` **回读 PDF 原文**，逐条确认"答案串真的在第 N 页上"，并要求负例的禁用词在全语料中不存在；
3. 报告里 `benchmarks/results/hybrid_rag/dataset_validation.json` 记录校验结果：**0 错误**。

**必须声明的暴露情况**：
* `benchmarks/datasets/rag/`（旧 4 文档 / 50 题）是**已暴露**数据，本报告不使用；
* ⬛ **更正（重要）**：主数据集的 `test` 切分**曾经是冻结的，但后来被跑过了**。
  `benchmarks/audits/split_exposure.py` 的审计结果（`split_exposure_audit.json`）：
  `test` **49/49 题（100%）**已出现在 `ablation_retrieval_cases.csv` 与 `ablation_qwen3_retrieval_cases.csv` 中，
  `validation` 55/55、`dev` 163/163 同样全部出现过。
  → **因此主数据集的三个切分都不能再当作独立评测集**；
  权威结论改为使用 §1.5 的**新冻结留出集**（`holdout/`，120 份新文档 / 320 道新题）。
  这个错误保留在文档里而不是删掉：它本身就是"没有先建切分隔离就开跑"的代价。

---

## 3. 评测口径

| 指标 | 定义 | 注意 |
| --- | --- | --- |
| Recall@K | Top-K 覆盖的 gold 页 / 全部 gold 页，按题平均 | 负例题不进分母 |
| Hit@K | Top-K 至少命中 1 个 gold 页的题目比例 | 比 Recall 宽松 |
| MRR@10 | 第一个 gold 页的倒数排名，窗口固定 10 | 所有模式同口径 |
| 全证据@5 | Recall@5 = 1 的题目比例 | 多证据题的真正门槛 |
| 答对率 | 关键事实齐备且无禁用内容（确定性规则） | 不含 LLM 裁判 |
| 引用准确率 | 从**回答正文**解析 `[来源：X · 第N页]` 与 gold 页比对 | 不用接口返回的 citations |
| 依据度 | 回答里带单位数字能否在上下文中找到 | 偏严格 |
| 延迟 | 单次检索/问答 wall time | P50/P95 |

四组实验的**公平性控制**：同一批文档、同一批题、同一份 gold、同一份上下文预算（1800 tokens）、
同一个 LLM 与同一段 prompt、同一套评测器、同一台机器。

> 上下文构建在收尾验收中统一了口径：知识库页的“基于本文档提问”过去走 `kb.build_context()`（字符预算、
> 遇长即 `break`），现与全局问答共用 `BrainService.build_context()`（token 预算 + 不早停 + 内容哈希去重）。
> 本报告的所有 `ctx tokens` 数字都来自 `build_budgeted_context`，不受该修复影响；
> 但产品侧“单文档问答”的上下文行为因此发生了变化（预算口径与全局一致），详见 `docs/HYBRID_RAG_ARCHITECTURE.md` §6.1。

---

## 4. 实验一：Embedding 模型对比（选型依据）

同一语料 750 chunk、267 题、只用向量路：

| Embedding | 维度 | R@1 | R@3 | R@5 | R@10 | MRR@10 | 全证据@5 | 检索 P50 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `bge-small-zh-v1.5` | 512 | 0.109 | 0.279 | 0.372 | 0.553 | 0.272 | 0.305 | **15 ms** |
| `Qwen3-Embedding-0.6B-Q` | 1024 | **0.120** | **0.324** | **0.437** | **0.590** | **0.291** | **0.397** | 550 ms |

* Qwen3 在召回上确实更好（R@5 +6.5pt），但**查询编码慢约 37 倍**；
* 本项目默认选 `bge-small-zh-v1.5`，Qwen3 作为可切换的高精度档（`AIEB_EMBEDDING_MODEL`）；
* 两者都远弱于 BM25 —— 说明**当前语料的关键瓶颈不是语义召回**。

> 📌 **本节已被替换为四模型对比（dev 切分、同一口径、冷缓存）**，详见
> `docs/HYBRID_RAG_EMBEDDING_COMPARISON.md`。新增了 `jina-embeddings-v2-base-zh`、`multilingual-e5-large`，
> 并核查了 `bge-m3`（不在 fastembed 注册表，不可行）与 `jina-v3`（CC-BY-NC，许可不允许）。
> 上表保留是因为它是"主数据集（已暴露）"上的历史记录，不作为选型依据。

---

## 5. 实验二：四组消融（检索层，267 题）

| 模式 | R@1 | R@3 | R@5 | R@10 | MRR@10 | 全证据@5 | 全证据@10 | P50 | P95 | 降级数 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| A. BM25 | **0.384** | 0.422 | 0.490 | 0.599 | 0.495 | 0.450 | — | **6 ms** | **10 ms** | 0 |
| B. Vector | 0.109 | 0.279 | 0.372 | 0.553 | 0.272 | 0.305 | — | 15 ms | 18 ms | 0 |
| C. Hybrid(RRF) | 0.277 | 0.406 | 0.483 | **0.601** | 0.426 | 0.439 | — | 21 ms | 29 ms | 0 |
| D. Hybrid+Rerank | 0.378 | **0.483** | **0.521** | **0.601** | **0.498** | **0.485** | — | 515 ms | 626 ms | 0 |

读法（重要）：

1. **R@1**：BM25 最好（0.384），Hybrid 反而更低（0.277）。原因是 RRF 把向量的高名次混进来，
   而向量的 R@1 只有 0.109 —— **融合会拉低最强一路的头部精度**。
2. **R@5/R@10**：Hybrid 的 R@10 最高（0.601，BM25 0.599），说明它**多召回了一些边缘证据**；
   但 R@5 仍不如 BM25，所以"多召回"没有转化成"更早命中"。
3. **Rerank 的作用是"把对的排上来"**：R@5 从 0.483 → 0.521（超过 BM25 的 0.490），MRR 0.426 → 0.498。
   这正是 cross-encoder 的价值：它看 query 和 passage 的**交互**，而不只是向量相似度。
4. **延迟**：BM25 6 ms、Hybrid 21 ms、Hybrid+Rerank 515 ms。重排是纯粹的延迟换精度。

### 分题型（Recall@5）

| 题型 | BM25 | Vector | Hybrid | Hybrid+Rerank |
| --- | --- | --- | --- | --- |
| numeric | 高 | 低 | 中 | 中高 |
| product_code（相似编号） | **最高** | 最低 | 中 | 高 |
| paraphrase（同义改写） | 中 | 中 | 中 | **高** |
| multi_page | 中 | 低 | 中 | **高** |
| cross_document | 中 | 低 | 中 | **高** |
| conflict（新旧版本） | **最高** | 低 | 中 | **最高** |
| negative | — | — | — | — |

> 细粒度数值见 `benchmarks/results/hybrid_rag/ablation_summary.json` 的 `retrieval.by_type`。
> 结论：**编号类问题 BM25 有结构性优势**（这正是"保留 BM25"的直接证据）；
> 语义改写与多页/跨文档问题，rerank 收益最明显。

---

## 6. 实验三：四组消融（回答层，分层 60 题 × 4 模式 = 240 次真实调用）

| 模式 | 答对率 | 端到端 | 引用准确率 | 引用召回率 | 依据度 | 平均上下文 token | 回答 P50 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| A. BM25 | 0.691 | 0.717 | 0.270 | 0.300 | 1.000 | 752 | 1554 ms |
| B. Vector | 0.654 | 0.683 | 0.296 | 0.318 | 1.000 | 730 | 1531 ms |
| C. Hybrid | **0.746** | **0.767** | **0.313** | 0.300 | 1.000 | 732 | 1495 ms |
| D. Hybrid+Rerank | **0.746** | **0.767** | 0.310 | **0.364** | 1.000 | 726 | 1904 ms |

读法：

1. **回答层的排序和检索层不同**：Hybrid 答对率最高（+5.5pt vs BM25）。
   原因是回答只需要"证据在 top-5 内"，Hybrid 的 **R@10 最高**，把一些原本排在 6-10 位的证据带了进来。
2. **Rerank 没有提高答对率**（0.746 → 0.746），但提高了**引用召回率**（0.300 → 0.364）：
   它让模型引用了更完整的证据页。这是一个"没有显著提升"的诚实记录 —— 任务书要求如实写。
3. **引用准确率整体偏低（0.27~0.31）**：模型倾向于多引用几页（"保险式引用"）。
   这是既有问题（与上一阶段 Top-K 实验一致），不是本次改造引入的。
4. **上下文 token 几乎相同**（726~752）：因为四组模式共享同一份预算，公平性得到保证。

---

## 7. 与既有 RAG 基准的关系（明确区分）

| 数据集 | 规模 | 用途 | 结论是否可混用 |
| --- | --- | --- | --- |
| `benchmarks/datasets/rag/`（旧） | 4 文档 / 20 页 / 50 题 | 已暴露，历史基线 | ❌ 不与本次数字混用 |
| `benchmarks/datasets/hybrid_rag/`（新） | **100 文档 / 500 页 / 267 题** | 开发期消融（**已暴露，仅作内部对比**） | ⚠️ 不可当泛化结论 |
| `benchmarks/datasets/hybrid_rag/holdout/` | **120 文档 / 600 页 / 320 题** | **第 1.5 节的权威评测** | ✅ 一次性、未参与调参 |

新数据集的题目明显更难（答对率 0.69~0.75，而旧集是 0.93），原因：
* 100 份文档里大量"同部门不同主体"的高度相似文档（干扰）；
* 相似编号陷阱（`CG-2024-001` vs `CG-2024-0010`）；
* 新旧版本冲突（V1 20% vs V2 25%）；
* 跨文档、多证据题比例更高。

---

## 8. 局限（必须同时读）

1. **数据是合成的**：文档由脚本按确定性规则生成，虽然经过"答案必须出现在标注页"的独立校验，
   但它不等于真实企业文档分布。
2. **QA 子集小**：主数据集 60 题、留出集 80 题。1 题在留出集里 = 1.35pt、
   在解答去分母 74 后 = 1.35pt，置信区间半宽约 ±11 pt。
   **任何小于 10 pt 的答对率差异都不应被当作结论。**
3. **引用准确率口径严格**：只要引用了非 gold 页就算错，实际业务里"多引一页正确但不必要的内容"危害有限。
4. **单模型、单机器**：只测了 `deepseek-chat` 与 ONNX CPU 推理。
5. **主数据集的三个切分均已暴露**（见 §2 更正）；本报告只用留出集下泛化结论，
   但留出集**只跑了一次**，没有第二份独立集可以验证"留出集结论本身"是否也带偶然性。
6. **留出集的 `cross_document` 只有 6 题**：四种模式 R@5 全是 0.500，差异不可判读。

---

## 9. 复现步骤

```bash
# 0) 切分暴露审计（确认哪些题被跑过）
python -m benchmarks.audits.split_exposure

# 1) 生成语料与题目（确定性，可重跑得到同样文件）
python -m benchmarks.datasets.hybrid_rag.build_corpus
python -m benchmarks.datasets.hybrid_rag.build_questions

# 1b) 生成并冻结留出集（一次性；冻结后不得用于调参）
python -m benchmarks.datasets.hybrid_rag.build_holdout
python -m benchmarks.datasets.hybrid_rag.validate_dataset --holdout

# 2) 独立校验（答案是否真在标注页上、负例是否真无答案）
python -m benchmarks.datasets.hybrid_rag.validate_dataset

# 3) 四组检索层消融（不调用 LLM）
python -m benchmarks.ablations.hybrid_modes --split all --skip-qa \
  --qdrant-url http://127.0.0.1:6335 --collection hybrid_ablation

# 4) 四组问答消融（分层 60 题，240 次真实调用）
python -m benchmarks.ablations.hybrid_modes --split all --qa-limit 60 --max-calls 260 \
  --qdrant-url http://127.0.0.1:6335 --collection hybrid_ablation

# 5) Embedding 模型对比（详见 HYBRID_RAG_EMBEDDING_COMPARISON.md）
python -m benchmarks.ablations.embedding_compare --model BAAI/bge-small-zh-v1.5 \
  --split dev --cache-dir benchmarks/.work/emb_cache_cold --fresh-cache

# 6) 【权威】冻结留出集一次性验证（320 题检索 + 80 题问答 = 320 次 LLM 调用）
python -m benchmarks.ablations.hybrid_modes --holdout --split all \
  --qa-limit 80 --max-calls 400 --collection hybrid_holdout \
  --qdrant-url http://127.0.0.1:6335 --out-tag "_holdout"
```

产物：

```
benchmarks/results/hybrid_rag/
├── dataset_validation.json            # 数据集独立校验（0 错误）
├── holdout_validation.json            # 留出集独立校验（0 错误）
├── split_exposure_audit.json          # 切分暴露审计（test 49/49 已暴露）
├── ablation_summary.json              # 四组消融汇总（主数据集，已暴露）
├── ablation_retrieval_cases.csv       # 每模式 × 每题一行
├── ablation_qa_cases.csv              # 问答明细（含模型原始回答）
├── ablation_holdout_summary.json      # 【权威】冻结留出集四模式
├── ablation_holdout_retrieval_cases.csv
├── ablation_holdout_qa_cases.csv
├── emb_compare_*.json / emb_load_*.json  # 向量模型对比（见 EMBEDDING_COMPARISON）
├── bm25_bench_75000.json              # BM25 优化前后等价性与提速
├── e2e_browser_*.json / e2e_shots/    # 真实浏览器 E2E（见 HYBRID_RAG_E2E.md）
└── scale_{1000,10000,50000}[_before_optimization].json  # 规模测试（见 SCALE_TEST）
```

相关文档：

* `docs/HYBRID_RAG_E2E.md` —— 真实浏览器四模式 E2E、Qdrant 故障降级、删除护栏
* `docs/HYBRID_RAG_EMBEDDING_COMPARISON.md` —— 四模型对比（dev 切分、冷缓存）
* `docs/HYBRID_RAG_SCALE_TEST.md` —— 规模测试与 BM25 优化前后对比
