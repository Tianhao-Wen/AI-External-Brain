# Benchmark 评测体系

一套**可复现、隔离运行、拒绝注水**的量化评测，用来测量 AI 外脑（WenDream / AI-External-Brain）三个核心能力的真实水平：

| 套件 | 测什么 | 数据规模 |
| --- | --- | --- |
| `rag` | 文档入库 → BM25 检索 → 模型作答 → 引用 | 4 份 PDF（20 页）/ 50 题 / 9 种题型 |
| `meeting` | 会议记录 → 风险/待办/决策结构化抽取 | 33 场虚构会议 / 40 条风险 / 75 条待办 / 65 条决策 |
| `agent` | 自然语言 → 工具调用 → 真实产出（PDF 问答、会议、记忆、搜索、邮件、PPT） | 25 个端到端任务 / 7 类能力 |

## 快速开始

```bash
# 只跑检索层，不调用模型（不需要 API Key，完全可复现）
python -m benchmarks.run --suite rag --mode fixture

# 真实模型全链路（AI 问答）
python -m benchmarks.run --suite rag --mode live --max-calls 90

# 会议风险抽取
python -m benchmarks.run --suite meeting --mode live --max-calls 60

# Agent 端到端（--repeat 3 会对标记过的任务重复三次，全轮次都进结果）
python -m benchmarks.run --suite agent --mode live --repeat 3 --max-calls 220

# 全部套件
python -m benchmarks.run --suite all --mode live --max-calls 400

# 只跑指定题目 / 前 N 条（Pilot 用）
python -m benchmarks.run --suite rag --mode live --only RAG-001,RAG-032 --max-calls 5

# 从结果 JSON 重新生成图表
python -m benchmarks.charts
```

重建数据集（一般不需要，数据集已提交）：

```bash
python -m benchmarks.datasets.rag.build_documents      # 重新生成 4 份 PDF
python -m benchmarks.datasets.rag.validate_dataset     # 数据集静态自检
python -m benchmarks.datasets.meetings.build_dataset   # 生成会议 txt + gold.jsonl
```

## 目录结构

```
benchmarks/
├── run.py                  # 统一 CLI（--suite / --mode / --max-calls / --output / --only / --limit / --repeat）
├── common.py               # 环境隔离、调用预算、Git 元信息、.aieb/ 指纹、脱敏、输出
├── charts.py               # 纯 Python 生成 SVG 图表（不依赖 matplotlib）
├── datasets/
│   ├── rag/                # build_documents.py + questions.jsonl + validate_dataset.py + documents/*.pdf
│   ├── meetings/           # corpus_{a,b,c}.py + build_dataset.py + meetings/*.txt + gold.jsonl
│   └── agent/tasks.py      # 25 个端到端任务与断言
├── evaluators/
│   ├── metrics.py          # precision / recall / F1 / mean / 分位数
│   ├── rag.py              # Recall@K、Hit@K、MRR、引用解析、Groundedness、拒答判定
│   └── meeting.py          # 一对一贪心匹配、per-type P/R/F1、等级混淆矩阵、待办/决策指标
├── runners/
│   ├── rag_runner.py
│   ├── meeting_runner.py
│   └── agent_runner.py     # Agent 装配 + Fake SMTP + 工具调用记录 + 断言执行
├── ablations/              # 参数消融实验（与三套件分开，避免改动已冻结的基准）
│   ├── topk.py             # RAG Top-K 消融：把 K 变成真实生产参数
│   ├── topk_audit.py       # 分块粒度 / 6000 字符上下文预算实证
│   └── report_topk.py      # 从结果 JSON 生成 SVG 图表 + 机器生成表格
├── results/                # 结果（JSON / CSV / MD，已提交，可核对每条样本）
└── reports/                # 图表 SVG
```

## 隔离与安全设计

* **不碰正式数据**：`BrainService(work_dir=X)` 让每次运行在 `benchmarks/.work/<run_id>/workspaces/` 下拥有独立的
  SQLite、会话、记忆、上传与产物目录。运行前后各取一次项目 `.aieb/` 的目录指纹，一旦变化直接报警并以退出码 2 结束。
* **不发真实邮件**：`common.neutralise_env()` 清掉进程内全部 `*_SMTP_*` 变量并强制 `AIEB_EMAIL_DRY_RUN=1`；
  `agent` 套件再把 `aieb.brain.mailer.send_email` 替换成 Fake SMTP 记录器。两层保险下不可能真的投递。
* **不泄露密钥**：报告元信息只记录 provider 名、模型名、端点主机；所有落盘文本过 `common.redact()`。
* **不超预算**：`--max-calls` 同时拦截业务层 `aieb.brain.llm.complete` 与 Agent 主循环的 `LLMClient.stream`，
  超限抛 `BudgetExceeded`，已产生的样本照常写入结果。
* **不改冻结项**：Benchmark 全程只读原始产品代码；`.aieb/` 与录屏 Demo 环境不被写入。

## 反作弊规则（写进代码而不是写在文档里）

1. 数据集与评测器**不含任何模型输出**：`questions.jsonl` / `gold.jsonl` 的判分短语都在题目或原文里，静态自检会逐条验证。
2. 失败样本**永远留在分母里**：调用异常会产出一条全 0 的记录，而不是被 `except: continue` 掉。
3. 重复运行**全部上报**：`--repeat` 的每一轮都是独立记录，同时给出"全部轮次都通过"的比例。
4. `fixture` 与 `live` **严格分离**：fixture 不调用模型，其分数只用于验证链路，不得作为模型能力引用。
5. 工具调用成功率与任务成功率**分开统计**，不在报告里互相替代。

## 主要结果
见 `docs/BENCHMARK_REPORT.md`（完整 12 节报告）、`docs/RESUME_BENCHMARK_SUMMARY.md`（简历/面试口径），
逐条数据见 `benchmarks/results/*_cases.csv` 与 `*_results.json`。

混合检索（Hybrid RAG）见 `docs/HYBRID_RAG_ARCHITECTURE.md`、`docs/HYBRID_RAG_BENCHMARK.md`、
`docs/HYBRID_RAG_SCALE_TEST.md`、`docs/HYBRID_RAG_INTERVIEW_SUMMARY.md`，
数据在 `benchmarks/results/hybrid_rag/`。复现命令：

```bash
python -m benchmarks.datasets.hybrid_rag.build_corpus          # 100 份 PDF（确定性）
python -m benchmarks.datasets.hybrid_rag.build_questions       # 267 道可核验题
python -m benchmarks.datasets.hybrid_rag.validate_dataset      # 独立校验（答案是否在标注页）
python -m benchmarks.ablations.hybrid_modes --split all --skip-qa   # 四组检索层消融
python -m benchmarks.ablations.hybrid_modes --split all --qa-limit 60 --max-calls 260  # 四组问答消融
python -m benchmarks.datasets.hybrid_rag.build_scale --chunks 50000
python -m benchmarks.runners.scale_runner --chunks 50000 --qdrant-url http://127.0.0.1:6335
```

参数消融（RAG Top-K）见 `docs/RAG_TOPK_ABLATION_REPORT.md` 与 `docs/RAG_TOPK_INTERVIEW_SUMMARY.md`，
数据在 `benchmarks/results/topk/`，图表在 `benchmarks/reports/chart_topk_*.svg`。复现命令：

```bash
python -m benchmarks.ablations.topk_audit                                        # 分块/预算审计（不调模型）
python -m benchmarks.ablations.topk --mode fixture --dataset full --k 1,3,5,10   # 检索层消融（不调模型）
python -m benchmarks.ablations.topk --mode live --dataset full --k 1,3,5,10 --max-calls 200
python -m benchmarks.ablations.report_topk                                       # 图表 + 表格
```

## 验收清单

| # | 检查项 | 判定方式 | 当前状态 |
| --- | --- | --- | --- |
| 1 | 同一命令可重复运行 | 连续执行两次，指标一致（fixture 完全一致，live 抖动见报告 10.3） | ✅ |
| 2 | 所有指标都能追到原始样本 | 每个数字在 `*_cases.csv` 有对应行、在 `*_results.json` 有结构化字段 | ✅ |
| 3 | 失败样本未被剔除 | 分母 = 数据集全量（RAG 50 / 会议 33 / Agent 25 任务 39 次执行） | ✅ |
| 4 | Mock 与真实能力分离 | fixture 模式不调模型；Agent 套件在 fixture 下直接报错拒绝；联网任务标注为 fixture | ✅ |
| 5 | 未发送任何真实邮件 | 清空 SMTP 环境变量 + 强制 dry-run + Fake SMTP；三轮共 0 封 | ✅ |
| 6 | 正式 `.aieb/` 未被改动 | 运行前后目录指纹比对，三次运行均「未变化」 | ✅ |
| 7 | 原始 WenDream 项目未被修改 | `git status` 仅新增 `benchmarks/`、两个 docs 文件与 `.gitignore` 条目 | ✅ |
| 8 | 产品业务逻辑未被改动 | 无任何 `aieb/` 源码改动；仅对工具实例与 LLM 客户端做只记录、不改语义的插桩 | ✅ |
| 9 | 调用量有上限 | `--max-calls`；本次正式运行 50 / 33 / 151 次 | ✅ |
| 10 | 无密钥、无隐私数据落盘 | 元信息只记 provider/模型/端点主机，正文统一 `redact()` | ✅ |
| 11 | 数据集自检通过 | `validate_dataset` 50/50、`build_dataset` 33 场 / 40 条风险全通过 | ✅ |
| 12 | README 提供可复制命令 | 见上文「快速开始」 | ✅ |

