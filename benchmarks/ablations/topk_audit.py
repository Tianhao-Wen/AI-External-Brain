"""上下文预算审计：用一份"长页文档"验证分块粒度与 6000 字符预算的真实行为。

回答三个只能用实验证明、不能只看代码就断言的问题：

1. BM25 返回的是**分块**还是整页？同一页会不会返回多个分块？
2. Top-K 个分块是否都会被送进 prompt？``build_context`` 的 6000 字符预算
   在长分块下会不会截断？
3. 检索结果里会不会出现重复分块？

不调用任何模型，纯检索层，结果可完全复现::

    python -m benchmarks.ablations.topk_audit
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from benchmarks import common

OUT = common.RESULTS_DIR / "topk" / "topk_context_budget.json"

#: 每页约 2400 字符 —— 远大于 700 字符的分块目标，必然一页多块。
PAGE_BODY = (
    "第十六条 供应商准入与年度复核。申请成为合格供应商，须提交营业执照复印件、"
    "ISO9001 质量管理体系证书、近三年无重大质量事故声明，以及上一年度审计报告。"
    "采购部在收到完整材料后 10 个工作日内完成初审，初审通过后由质量部在 5 个工作日内"
    "完成现场审核，现场审核采用百分制评分，80 分以上为合格。"
    "第十七条 合同与付款。单一合同金额超过 50 万元的，须经法务与财务双签；"
    "付款方式原则上采用 3:5:2 分期，即预付款 30%、验收款 50%、质保金 20%，"
    "质保期为验收合格之日起 12 个月。"
    "第十八条 交付与违约。供应商逾期交付的，按合同总额每日 0.5% 计收违约金，"
    "累计不超过合同总额的 20%；连续两次逾期超过 15 日的，采购部有权终止合作并列入观察名单。"
    "第十九条 保密与合规。供应商不得泄露本项目相关的技术资料与商务信息，"
    "保密义务在合同终止后继续有效 3 年；违反保密约定的，须承担由此造成的全部直接损失。"
)


def build_long_page_document() -> bytes:
    """6 页 × 约 1300 字符：每页被拆成 2 个分块，Top-10 约 6800 字符，必然超过 6000 预算。"""
    pages = []
    for index in range(1, 7):
        pages.append(f"第 {index} 章 采购管理办法\n{PAGE_BODY * 3}")
    # 纯文本用 \f 分页（与 knowledge.split_pages 的分页符一致）
    return "\f".join(pages).encode("utf-8")


def main() -> int:
    from aieb.brain import knowledge
    from aieb.brain.service import BrainService

    workspace = common.WORK_ROOT / "topk_audit_workspace"
    # 每次重跑都用全新工作区，避免上一次的文档混进索引
    if workspace.exists():
        import shutil

        shutil.rmtree(workspace, ignore_errors=True)
    workspace.mkdir(parents=True, exist_ok=True)
    # offline provider：本审计只走检索层，绝不触发任何模型调用
    provider, _model = common.resolve_provider(offline=True)
    service = BrainService(work_dir=workspace, provider=provider)
    payload: dict[str, Any] = {}
    try:
        raw = build_long_page_document()
        record = service.upload("采购管理办法_长页测试.md", raw)
        if record.get("status") != "ready":
            raise RuntimeError(f"入库失败：{record.get('status')} {record.get('error')}")

        chunks = service.store.all_chunks()
        per_page: dict[int, int] = {}
        for chunk in chunks:
            per_page[int(chunk["page"])] = per_page.get(int(chunk["page"]), 0) + 1

        payload["document"] = {
            "pages": record.get("pages"),
            "chars": record.get("chars"),
            "chunks": len(chunks),
            "chunks_per_page": {str(k): v for k, v in sorted(per_page.items())},
            "avg_chunk_chars": round(
                sum(len(c["text"]) for c in chunks) / len(chunks), 1
            )
            if chunks
            else 0,
        }

        query = "供应商逾期交付的违约金怎么算，上限是多少"
        hits = service.search(query, top_k=10)
        pairs = [(h["doc_id"], int(h["page"]), h["text"]) for h in hits]
        page_counts: dict[str, int] = {}
        for h in hits:
            key = f"p{h['page']}"
            page_counts[key] = page_counts.get(key, 0) + 1
        payload["retrieval"] = {
            "query": query,
            "returned_chunks": len(hits),
            "distinct_pages": len({(h["doc_id"], int(h["page"])) for h in hits}),
            "duplicate_chunk_texts": len(pairs) - len(set(pairs)),
            "hits_per_page": page_counts,
            "max_chunks_same_page": max(page_counts.values()) if page_counts else 0,
        }

        context = knowledge.build_context(hits)
        blocks = context.count("[来源：")
        payload["context_budget"] = {
            "max_chars": 6000,
            "retrieved_chunks": len(hits),
            "blocks_in_prompt": blocks,
            "dropped_chunks": len(hits) - blocks,
            "context_chars": len(context),
            "truncated": blocks < len(hits),
        }

        # 与消融实验里的复刻逻辑保持一致
        from benchmarks.ablations.topk import context_blocks

        payload["context_budget"]["replica_matches"] = len(context_blocks(hits)) == blocks
    finally:
        service.store.close()

    common.write_json(OUT, payload)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    print(f"\n已写入：{OUT}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
