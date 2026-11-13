"""``aieb.brain.knowledge.prune_citations`` 的单元测试：引用校验的边界。"""

from __future__ import annotations

from aieb.brain import knowledge

HITS = [
    {
        "doc_id": "d1",
        "doc_name": "DOC-A-内部管理手册.pdf",
        "page": 2,
        "text": "单笔采购金额超过 300 万元的，还须提交总经理办公会形成书面决议。",
        "score": 1.0,
    },
    {
        "doc_id": "d1",
        "doc_name": "DOC-A-内部管理手册.pdf",
        "page": 6,
        "text": "合同用印须由法务部门审核，用印申请至少提前 2 个工作日提交。",
        "score": 0.9,
    },
]


def test_drops_unsupported_marker():
    answer = (
        "超过 300 万元须提交总经理办公会决议 [来源：DOC-A-内部管理手册.pdf · 第2页]；"
        "另外请留意流程 [来源：DOC-A-内部管理手册.pdf · 第6页]。"
    )
    out, meta = knowledge.prune_citations(answer, HITS)
    assert "第6页" not in out
    assert "第2页" in out
    assert meta == [{"doc_name": "DOC-A-内部管理手册.pdf", "page": 2, "support": 1.0}]


def test_keeps_marker_when_page_supports_sentence():
    answer = "用印申请至少提前 2 个工作日提交 [来源：DOC-A-内部管理手册.pdf · 第6页]。"
    out, meta = knowledge.prune_citations(answer, HITS)
    assert "第6页" in out
    assert meta[0]["page"] == 6


def test_same_doc_different_pages_are_not_confused():
    """同名文档不同页必须分别比对（早先只按文档名建索引会串页）。"""
    answer = (
        "单笔采购超过 300 万元须提交总经理办公会决议 [来源：DOC-A-内部管理手册.pdf · 第2页]。"
        "合同用印须由法务部门审核 [来源：DOC-A-内部管理手册.pdf · 第6页]。"
    )
    out, meta = knowledge.prune_citations(answer, HITS)
    pages = {item["page"] for item in meta}
    assert pages == {2, 6}
    assert "第6页" in out


def test_keeps_everything_when_all_markers_unsupported():
    """全部判无依据时不能把答案的出处全删光。"""
    answer = "完全无关的一句话 [来源：DOC-A-内部管理手册.pdf · 第6页]。"
    out, meta = knowledge.prune_citations(answer, HITS)
    assert out == answer
    assert meta == []


def test_unknown_page_is_kept():
    """页码不在检索结果里时保持原样，避免误删可能正确的引用。"""
    answer = "某结论 [来源：DOC-Z-未知文档.pdf · 第9页]。"
    out, meta = knowledge.prune_citations(answer, HITS)
    assert "第9页" in out
    assert meta[0]["support"] is None


def test_whitespace_in_doc_name_is_tolerated():
    answer = "单笔采购超过 300 万元的还须提交总经理办公会决议 [来源：DOC-A-内部 管理手册.pdf · 第2页]。"
    out, _meta = knowledge.prune_citations(answer, HITS)
    assert "第2页" in out
