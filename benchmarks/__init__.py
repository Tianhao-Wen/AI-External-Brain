"""AI 外脑 Demo 的可复现 Benchmark 评测体系。

三条原则（与 docs/BENCHMARK_REPORT.md 第 1 节一致）：

1. **只测真实实现**：所有指标都由 ``aieb`` 包的真实业务链路（PyMuPDF 解析、
   字符 n-gram + BM25 检索、会议结构化抽取、Agent 工具层）产生，不另写
   一套"评测专用"的简化算法来抬分。
2. **绝不碰演示环境**：每次运行都在 ``benchmarks/.work/<run_id>/`` 下新建
   独立 workspace，``BrainService`` 会把 SQLite、上传目录、会话与记忆都写在
   该目录的 ``.aieb/`` 里；正式 ``.aieb/`` 只做只读指纹校验，不写入。
3. **不美化数字**：失败样本一律保留并计入分母；Mock / Fixture 与真实模型
   调用分表统计，不混成一个成功率。
"""

__all__ = ["__version__"]

__version__ = "1.0.0"
