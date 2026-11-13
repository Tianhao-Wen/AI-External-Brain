"""PPTX 生成：必须是真实可打开的文件，且版式有统一结构。"""

from __future__ import annotations

from pathlib import Path

import pytest

from aieb.brain import llm, pptgen
from aieb.brain.store import BrainStore

OUTLINE = {
    "title": "第三季度供应商风险复盘",
    "subtitle": "面向管理层的风险与行动汇报",
    "slides": [
        {"title": "背景", "bullets": ["核心品类 62% 依赖单一供应商", "不合格率 4.7%"]},
        {"title": "风险", "bullets": ["断供风险", "质量风险", "合同条款缺失"]},
        {"title": "行动", "bullets": ["引入备选供应商", "限期整改", "补充合同条款"]},
    ],
}


def test_render_creates_openable_pptx(tmp_path: Path) -> None:
    path, pages = pptgen.render(OUTLINE, out_dir=tmp_path, topic="供应商风险复盘")
    assert path.is_file()
    assert path.suffix == ".pptx"
    assert path.stat().st_size > 5000
    assert pages >= 3

    from pptx import Presentation

    prs = Presentation(str(path))
    assert len(prs.slides) >= 5  # 封面 + 3 内容页 + 总结
    text = "\n".join(
        shape.text_frame.text
        for slide in prs.slides
        for shape in slide.shapes
        if shape.has_text_frame
    )
    assert "第三季度供应商风险复盘" in text
    assert "AI 外脑 Demo" in text


def test_render_with_risk_table(tmp_path: Path) -> None:
    rows = [["断供风险", "风险", "high", "引入备选供应商"]]
    path, _pages = pptgen.render(
        OUTLINE, out_dir=tmp_path, topic="带表格的汇报",
        meta_lines=rows, meta_title="风险与待办跟踪",
        meta_headers=["事项", "类型", "负责人/等级", "截止/建议"],
    )
    from pptx import Presentation

    prs = Presentation(str(path))
    has_table = any(shape.has_table for slide in prs.slides for shape in slide.shapes)
    assert has_table, "风险/待办应渲染成表格页"


def test_bullets_are_truncated_not_dumped(tmp_path: Path) -> None:
    long = "很长的要点" * 40
    outline = {"title": "T", "subtitle": "S",
               "slides": [{"title": "页", "bullets": [long] * 10}]}
    path, _pages = pptgen.render(outline, out_dir=tmp_path, topic="长文本")
    from pptx import Presentation

    prs = Presentation(str(path))
    texts = [
        shape.text_frame.text
        for slide in prs.slides
        for shape in slide.shapes
        if shape.has_text_frame
    ]
    assert any(t.endswith("…") for t in texts), "超长要点应被截断"
    assert all(len(t) < 200 for t in texts)


@pytest.mark.asyncio
async def test_generate_uses_llm_outline(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def fake_complete_json(*args, **kwargs):
        return OUTLINE

    monkeypatch.setattr(llm, "complete_json", fake_complete_json)
    store = BrainStore(tmp_path)
    try:
        from aieb.config import ProviderConfig

        provider = ProviderConfig(name="t", protocol="openai-compat", base_url="http://x",
                                  model="m", api_key="k")
        result = await pptgen.generate(provider, store, topic="供应商风险复盘", slide_count=6,
                                       work_dir=tmp_path)
        assert Path(result["path"]).is_file()
        assert result["slide_count"] >= 4
        assert store.list_activities()[0]["kind"] == "presentation"
    finally:
        store.close()
