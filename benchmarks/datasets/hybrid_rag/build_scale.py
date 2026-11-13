"""大规模性能测试语料的生成器（1k / 10k / 50k / 100k chunk）。

与质量评测集的区别（报告里必须区分清楚）：

* 这里生成的是**合成语料**，用来测**索引与检索的工程性能**；
* 它**不能**用来证明企业问答准确率——那由 `benchmarks/datasets/hybrid_rag`
  的 100 份人工可核验文档 + 267 道题负责。

生成内容刻意包含真实工程里会遇到的难点，而不是"同一段话复制 N 份"：

* 每份文档主题不同（部门 × 主体 × 年度 × 制度类型）；
* 同一规则在不同文档里用不同措辞（同义改写）；
* 项目编号高度相似但不同（`PRJ-2024-00123` vs `PRJ-2024-001230`）；
* 同一规则跨页拆开（跨页证据）；
* 每页都有无关的干扰段落（食堂、班车、办公用品）。

用法：
    python -m benchmarks.datasets.hybrid_rag.build_scale --chunks 10000
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Iterator

from benchmarks import common

SCALE_DIR = common.WORK_ROOT / "scale_corpus"

TOPICS: tuple[str, ...] = (
    "采购管理", "供应商质量", "项目交付", "财务预算", "差旅报销", "信息安全",
    "固定资产", "人力资源", "合同评审", "仓储物流", "研发立项", "审计整改",
    "客户投诉", "生产安全", "环境保护", "知识产权", "培训管理", "绩效考评",
    "印章管理", "档案管理",
)

VERBS: tuple[str, ...] = ("完成", "提交", "审批", "归档", "复核", "确认", "备案", "上报")

NOISE_PARAGRAPHS: tuple[str, ...] = (
    "第{n}条 后勤保障。园区食堂供应时间为 11:30 至 13:00，节假日另行通知；班车每日 8:00 自地铁站发车。",
    "第{n}条 办公用品。打印纸按月申领，单次申领不超过 5 包；硒鼓以旧换新。",
    "第{n}条 会议室预约。会议室需提前 2 小时在系统中预约，超时 15 分钟未使用自动释放。",
    "第{n}条 绿植养护。办公区绿植由行政统一养护，每周浇水两次。",
)


def _clause(topic: str, idx: int, company: str, seed: int) -> str:
    """生成一条带具体数字的条款（数字由 seed 决定，保证可复现且有差异）。"""
    amount = 50 + (seed * 37) % 400
    days = 3 + (seed * 11) % 25
    ratio = 3 + (seed * 7) % 15
    verb = VERBS[seed % len(VERBS)]
    template = seed % 6
    if template == 0:
        body = (
            f"第{idx}条 {topic}审批。单一事项金额超过 {amount} 万元的，"
            f"须由{company}负责人与财务负责人联合审批，并在 {days} 个工作日内{verb}。"
        )
    elif template == 1:
        body = (
            f"第{idx}条 {topic}时限。相关材料齐备后应在 {days} 个工作日内{verb}，"
            f"逾期需书面说明原因；抽检比例不低于 {ratio}%。"
        )
    elif template == 2:
        body = (
            f"第{idx}条 {topic}违约处理。发生延期且超过 {days} 日的，"
            f"按合同总额每日 0.{ratio}% 计收违约金，累计不超过合同总额的 {ratio + 5}%。"
        )
    elif template == 3:
        body = (
            f"第{idx}条 {topic}编号。编号格式为 PRJ-2024-{seed % 900 + 100:03d}，"
            f"与 PRJ-2024-{seed % 900 + 100:03d}0 属不同批次，不得混用。"
        )
    elif template == 4:
        body = (
            f"第{idx}条 {topic}复核。每季度抽查不少于 {ratio} 个样本，"
            f"发现问题须在 {days} 个工作日内整改完毕并{verb}。"
        )
    else:
        body = (
            f"第{idx}条 {topic}归档。归档材料保存期限不少于 {days} 个月，"
            f"电子件与纸质件须一致，金额超过 {amount} 万元的另附审计意见。"
        )
    return body


def build_document(doc_index: int, *, pages: int, chars_per_page: int) -> dict[str, Any]:
    """构造一份文档：指定页数、每页大致字符数。"""
    topic = TOPICS[doc_index % len(TOPICS)]
    company = f"{topic[:2]}事业部{chr(ord('A') + doc_index % 20)}"
    doc_code = f"SCALE-{doc_index:05d}"
    page_texts: list[str] = []
    article = 1
    for page_no in range(1, pages + 1):
        parts: list[str] = [f"{doc_code} {company}{topic}管理办法 — 第 {page_no} 页 / 共 {pages} 页"]
        length = len(parts[0])
        clause_no = 0
        while length < chars_per_page:
            if clause_no % 5 == 4:
                piece = NOISE_PARAGRAPHS[(doc_index + clause_no) % len(NOISE_PARAGRAPHS)]
                piece = piece.format(n=article)
            else:
                piece = _clause(
                    topic, article, company, seed=doc_index * 131 + page_no * 17 + clause_no
                )
            parts.append(piece)
            length += len(piece)
            article += 1
            clause_no += 1
        page_texts.append("\n".join(parts))
    return {
        "doc_id": doc_code,
        "filename": f"{doc_code}_{company}_{topic}.md",
        "topic": topic,
        "company": company,
        # 用 \f 分页，与 knowledge.split_pages 的分页符一致
        "text": "\f".join(page_texts),
        "pages": pages,
        "chars": sum(len(p) for p in page_texts),
    }


def generate(
    target_chunks: int,
    *,
    out_dir: Path | None = None,
    pages_per_doc: int = 50,
    chars_per_page: int = 1400,
    chunk_chars: int = 700,
) -> dict[str, Any]:
    """生成用于性能测试的语料目录，返回清单。"""
    chunks_per_doc = max(1, (chars_per_page // chunk_chars) * pages_per_doc)
    doc_count = max(1, round(target_chunks / chunks_per_doc))
    out_dir = out_dir or (SCALE_DIR / f"n{target_chunks}")
    out_dir.mkdir(parents=True, exist_ok=True)

    docs = [build_document(i, pages=pages_per_doc, chars_per_page=chars_per_page) for i in range(doc_count)]
    total_chars = 0
    for doc in docs:
        path = out_dir / doc["filename"]
        path.write_text(doc["text"], encoding="utf-8")
        total_chars += doc["chars"]

    manifest = {
        "target_chunks": target_chunks,
        "documents": doc_count,
        "pages_per_doc": pages_per_doc,
        "chars_per_page": chars_per_page,
        "estimated_chunks_per_doc": chunks_per_doc,
        "chars_total": total_chars,
        "output_dir": str(out_dir),
        "note": "实际 chunk 数由 knowledge.chunk_pages 决定，运行时以实测为准",
    }
    (out_dir / "_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return manifest


def iter_documents(out_dir: Path) -> Iterator[Path]:
    return sorted(p for p in out_dir.glob("*.md"))


def main() -> int:
    parser = argparse.ArgumentParser(description="生成大规模性能测试语料")
    parser.add_argument("--chunks", type=int, default=1000, help="目标 chunk 数（1k/10k/50k/100k）")
    parser.add_argument("--pages-per-doc", type=int, default=50)
    parser.add_argument("--chars-per-page", type=int, default=1400)
    parser.add_argument("--out", default="")
    args = parser.parse_args()
    manifest = generate(
        args.chunks,
        out_dir=Path(args.out) if args.out else None,
        pages_per_doc=args.pages_per_doc,
        chars_per_page=args.chars_per_page,
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
