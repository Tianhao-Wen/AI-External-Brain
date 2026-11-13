"""PPTX 生成：真实、可编辑的 .pptx，统一版式（python-pptx）。

版式约定
--------
* 16:9，标题栏用品牌深蓝（#1E3A5F），强调色用品牌蓝（#2563EB）；
* 每页最多 6 条要点，单条自动截断，绝不把整段文字堆进幻灯片；
* 风险/待办支持表格页，表格与卡片风格一致；
* 文件写入 ``.aieb/brain/exports``，由 ArtifactStore 复制进产物区后提供签名下载。
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Any

from aieb.brain import llm, paths
from aieb.brain.store import BrainStore
from aieb.config import ProviderConfig

FONT = "Microsoft YaHei"
NAVY = (0x1E, 0x3A, 0x5F)
BLUE = (0x25, 0x63, 0xEB)
TEAL = (0x0D, 0x94, 0x88)
SLATE = (0x64, 0x74, 0x8B)
WHITE = (0xFF, 0xFF, 0xFF)

OUTLINE_SYSTEM = """你是企业级汇报材料撰写专家。请输出一份中文 PPT 大纲，只输出 JSON：
{
  "title": "封面主标题",
  "subtitle": "封面副标题（一句话说明汇报目的）",
  "slides": [
    {"title": "页面标题", "bullets": ["要点1", "要点2"], "notes": "演讲备注（可选）"}
  ]
}
要求：
- 每页 3-5 条要点，每条不超过 40 字，是结论而不是长句；
- 页面之间逻辑递进（背景 → 现状 → 风险 → 行动）；
- 不要编造具体数字、人名或结论，除非资料中明确给出；
- 总页数控制在用户要求的范围内。"""


class PPTError(RuntimeError):
    """PPT 生成失败。"""


def _rgb(color, g=None, b=None):
    """支持 (r,g,b) 元组、_rgb(r,g,b) 或 0xRRGGBB 整数三种写法。"""
    from pptx.dml.color import RGBColor

    if g is not None and b is not None:
        return RGBColor(int(color), int(g), int(b))
    if isinstance(color, (tuple, list)):
        return RGBColor(*color)
    value = int(color)
    return RGBColor((value >> 16) & 0xFF, (value >> 8) & 0xFF, value & 0xFF)


def _trim(text: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _add_textbox(slide, left, top, width, height, text, *, size=18, bold=False,
                 color=NAVY, align=None, line_spacing=1.25):
    from pptx.util import Pt

    box = slide.shapes.add_textbox(left, top, width, height)
    frame = box.text_frame
    frame.word_wrap = True
    para = frame.paragraphs[0]
    run = para.add_run()
    run.text = text
    run.font.size = Pt(size)
    run.font.bold = bold
    run.font.name = FONT
    run.font.color.rgb = _rgb(color)
    para.line_spacing = line_spacing
    if align is not None:
        para.alignment = align
    return box


def _add_rect(slide, left, top, width, height, color, *, line=False):
    from pptx.enum.shapes import MSO_SHAPE

    shape = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, left, top, width, height)
    shape.fill.solid()
    shape.fill.fore_color.rgb = _rgb(color)
    if not line:
        shape.line.fill.background()
    shape.shadow.inherit = False
    return shape


def _slide_title(prs, title: str, subtitle: str = ""):
    from pptx.util import Inches, Pt

    slide = prs.slides.add_slide(prs.slide_layouts[6])
    _add_rect(slide, 0, 0, prs.slide_width, Inches(0.12), NAVY)
    box = slide.shapes.add_textbox(Inches(0.7), Inches(0.45), prs.slide_width - Inches(1.4), Inches(0.8))
    frame = box.text_frame
    frame.word_wrap = True
    para = frame.paragraphs[0]
    run = para.add_run()
    run.text = _trim(title, 60)
    run.font.size = Pt(28)
    run.font.bold = True
    run.font.name = FONT
    run.font.color.rgb = _rgb(NAVY)
    if subtitle:
        _add_textbox(slide, Inches(0.7), Inches(1.25), prs.slide_width - Inches(1.4), Inches(0.5),
                     _trim(subtitle, 90), size=13, color=SLATE)
    return slide


def _bullet_slide(prs, title: str, bullets: list[str], subtitle: str = ""):
    from pptx.util import Inches, Pt

    slide = _slide_title(prs, title, subtitle)
    top = Inches(1.9)
    for bullet in bullets[:6]:
        dot = slide.shapes.add_shape(1, Inches(0.75), top + Inches(0.12), Inches(0.12), Inches(0.12))
        dot.fill.solid()
        dot.fill.fore_color.rgb = _rgb(BLUE)
        dot.line.fill.background()
        dot.shadow.inherit = False
        box = slide.shapes.add_textbox(Inches(1.05), top, prs.slide_width - Inches(1.9), Inches(0.75))
        frame = box.text_frame
        frame.word_wrap = True
        para = frame.paragraphs[0]
        run = para.add_run()
        run.text = _trim(bullet, 60)
        run.font.size = Pt(17)
        run.font.name = FONT
        run.font.color.rgb = _rgb(0x0F, 0x17, 0x2A)
        top += Inches(0.78)
    return slide


def _table_slide(prs, title: str, headers: list[str], rows: list[list[str]], subtitle: str = ""):
    from pptx.util import Inches, Pt

    slide = _slide_title(prs, title, subtitle)
    shape_rows = min(len(rows[:8]) + 1, 9)
    shape_cols = len(headers)
    table_shape = slide.shapes.add_table(
        shape_rows, shape_cols,
        Inches(0.7), Inches(1.85),
        prs.slide_width - Inches(1.4), Inches(0.45) * shape_rows,
    )
    table = table_shape.table
    for col, header in enumerate(headers):
        cell = table.cell(0, col)
        cell.text = header
        cell.fill.solid()
        cell.fill.fore_color.rgb = _rgb(NAVY)
        for para in cell.text_frame.paragraphs:
            for run in para.runs:
                run.font.size = Pt(13)
                run.font.bold = True
                run.font.name = FONT
                run.font.color.rgb = _rgb(WHITE)
    for r, row in enumerate(rows[:8], start=1):
        for c, value in enumerate(row[:shape_cols]):
            cell = table.cell(r, c)
            cell.text = _trim(value, 42)
            for para in cell.text_frame.paragraphs:
                for run in para.runs:
                    run.font.size = Pt(12)
                    run.font.name = FONT
                    run.font.color.rgb = _rgb(0x0F, 0x17, 0x2A)
    return slide


def _cover(prs, title: str, subtitle: str, meta: str):
    from pptx.enum.text import PP_ALIGN
    from pptx.util import Inches, Pt

    slide = prs.slides.add_slide(prs.slide_layouts[6])
    _add_rect(slide, 0, 0, prs.slide_width, prs.slide_height, NAVY)
    _add_rect(slide, 0, Inches(4.55), prs.slide_width, Inches(0.06), TEAL)
    _add_textbox(slide, Inches(0.9), Inches(2.1), prs.slide_width - Inches(1.8), Inches(1.4),
                 _trim(title, 60), size=40, bold=True, color=WHITE, align=PP_ALIGN.LEFT)
    _add_textbox(slide, Inches(0.9), Inches(3.35), prs.slide_width - Inches(1.8), Inches(0.9),
                 _trim(subtitle, 90), size=17, color=(0xC7, 0xD2, 0xFE))
    _add_textbox(slide, Inches(0.9), Inches(4.85), prs.slide_width - Inches(1.8), Inches(0.6),
                 meta, size=12, color=(0x94, 0xA3, 0xB8))
    return slide


def _footer(prs, index: int, total: int) -> None:
    """给除封面外的每页加页码与品牌页脚。"""
    from pptx.enum.text import PP_ALIGN
    from pptx.util import Inches, Pt

    slide = prs.slides[-1]
    _add_textbox(slide, Inches(0.7), prs.slide_height - Inches(0.55), Inches(6), Inches(0.35),
                 "AI 外脑 Demo · Enterprise AI Workspace", size=9, color=SLATE)
    box = slide.shapes.add_textbox(prs.slide_width - Inches(1.6), prs.slide_height - Inches(0.55),
                                   Inches(0.9), Inches(0.35))
    para = box.text_frame.paragraphs[0]
    run = para.add_run()
    run.text = f"{index}/{total}"
    run.font.size = Pt(9)
    run.font.name = FONT
    run.font.color.rgb = _rgb(SLATE)
    para.alignment = PP_ALIGN.RIGHT


async def build_outline(
    provider: ProviderConfig,
    *,
    topic: str,
    slide_count: int,
    focus: str = "",
    audience: str = "",
    context: str = "",
) -> dict[str, Any]:
    prompt = [
        f"主题：{topic}",
        f"页数：{slide_count} 页（含封面，请输出约 {max(slide_count - 2, 1)} 个内容页）",
        f"汇报用途/受众：{audience or '企业管理层'}",
    ]
    if focus:
        prompt.append(f"内容重点：{focus}")
    if context:
        prompt.append(f"可参考的已有资料（只能使用其中出现的事实）：\n{context[:6000]}")
    data = await llm.complete_json(provider, OUTLINE_SYSTEM, "\n".join(prompt), timeout=150.0)
    if not isinstance(data, dict) or not isinstance(data.get("slides"), list):
        raise PPTError("大纲生成失败：返回结构不符合要求")
    return data


def render(
    outline: dict[str, Any],
    *,
    out_dir: Path,
    topic: str,
    meta_lines: list[list[str]] | None = None,
    meta_title: str = "",
    meta_headers: list[str] | None = None,
    total_slides: int | None = None,
) -> tuple[Path, int]:
    """按统一版式渲染成 .pptx，返回 ``(文件路径, 实际页数)``。

    ``total_slides`` 是"用户明确要求的页数"。以前封面页与总结页是固定追加的，
    用户要 8 页会得到 9 页（大纲 7 页 + 封面 + 总结 + 表格），而且返回值把表格页
    漏算在内，导致"回复说 8 页、打开是 9 页"。现在把页数当成**硬约束**：
    先扣掉封面与总结，再决定表格页能不能放下，最后把多出来的内容页裁掉。
    """
    try:
        from pptx import Presentation
        from pptx.util import Inches
    except ImportError as exc:  # pragma: no cover
        raise PPTError("服务端未安装 PPT 生成组件（python-pptx）") from exc

    prs = Presentation()
    prs.slide_width = Inches(13.333)
    prs.slide_height = Inches(7.5)

    title = _trim(outline.get("title") or topic, 60)
    subtitle = _trim(outline.get("subtitle") or "由 AI 外脑自动生成", 90)
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    _cover(prs, title, subtitle, f"AI 外脑 Demo 自动生成 · {stamp}")

    slides = [s for s in outline.get("slides", []) if isinstance(s, dict)]
    want_table = bool(meta_lines and meta_headers)
    if total_slides:
        target = max(3, int(total_slides))
        # 封面 1 + 总结 1 固定，剩给内容页与表格页
        use_table = want_table and (target - 3) >= 2  # 至少还要留 1 页内容
        capacity = max(1, target - 2 - (1 if use_table else 0))
        if len(slides) > capacity:
            slides = slides[:capacity]
    else:
        use_table = want_table
    meta_lines = meta_lines if use_table else None

    total = len(slides) + (1 if use_table else 0) + 1
    index = 0
    for item in slides:
        bullets = [str(b) for b in (item.get("bullets") or []) if str(b).strip()]
        section_title = str(item.get("title") or "内容页")
        subtitle_text = str(item.get("subtitle") or "")
        _bullet_slide(prs, section_title, bullets[:6], subtitle_text)
        index += 1
        _footer(prs, index, total)

    if meta_lines and meta_headers:
        _table_slide(prs, meta_title or "风险与待办", meta_headers, meta_lines)
        index += 1
        _footer(prs, index, total)

    # 结尾只放一页：标题即"总结与下一步"，避免出现一页只有标题的空页
    _bullet_slide(prs, "总结与下一步", [
        "确认风险等级与责任分工",
        "按截止时间推进待办事项",
        "如需补充材料，可在知识库继续上传",
    ], "以上内容由 AI 外脑基于既有资料生成，请核对后使用")
    index += 1
    _footer(prs, index, total)

    out_dir.mkdir(parents=True, exist_ok=True)
    safe_topic = re.sub(r"[\\/:*?\"<>|\s]+", "_", topic).strip("_")[:40] or "汇报"
    filename = f"{safe_topic}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pptx"
    target_file = out_dir / filename
    prs.save(str(target_file))
    if not target_file.is_file() or target_file.stat().st_size < 5000:
        raise PPTError("PPT 文件生成异常（文件过小），请重试")
    # 以文件为准重新数页数：报告里的页数必须等于用户打开后看到的页数
    try:
        actual = len(Presentation(str(target_file)).slides)
    except Exception as exc:  # noqa: BLE001
        raise PPTError(f"PPT 文件校验失败：{exc}") from exc
    return target_file, actual


def collect_context(store: BrainStore, topic: str, index: Any = None) -> str:
    """从知识库与最近会议里取可引用的事实，避免 PPT 内容凭空编造。"""
    blocks: list[str] = []
    if index is not None:
        try:
            hits = index.search(topic, top_k=4)
        except Exception:  # noqa: BLE001 - 检索失败不应阻塞 PPT 生成
            hits = []
        for hit in hits:
            blocks.append(f"[{hit['doc_name']} 第{hit['page']}页] {hit['text'][:600]}")
    for meeting in store.list_meetings(limit=3):
        blocks.append(
            f"[会议 {meeting['title']}] 结论：{meeting.get('summary') or '（无）'}；"
            f"决策：{'；'.join(meeting.get('decisions') or []) or '（无）'}"
        )
    return "\n\n".join(blocks)


async def generate(
    provider: ProviderConfig,
    store: BrainStore,
    *,
    topic: str,
    slide_count: int = 8,
    focus: str = "",
    audience: str = "",
    index: Any = None,
    work_dir: str | Path | None = None,
) -> dict[str, Any]:
    context = collect_context(store, topic, index)
    # 页数口径：优先用显式参数；只有参数缺省时才尝试从主题文字里解析"8 页"。
    requested = int(slide_count or 0)
    if requested <= 0:
        match = re.search(r"(\d+)\s*页", topic)
        requested = max(3, min(20, int(match.group(1)))) if match else 8
    requested = max(3, min(20, requested))
    outline = await build_outline(
        provider,
        topic=topic,
        slide_count=requested,
        focus=focus,
        audience=audience,
        context=context,
    )

    # 若最近会议有风险/待办，补一页表格，让汇报更可执行
    level_text = {"high": "高", "medium": "中", "low": "低"}
    meta_rows: list[list[str]] = []
    meetings = store.list_meetings(limit=1)
    if meetings:
        detail = store.get_meeting(meetings[0]["id"]) or {}
        for risk in (detail.get("risks") or [])[:3]:
            meta_rows.append([
                risk["title"][:30], "风险",
                level_text.get(risk["level"], risk["level"]),
                risk.get("suggestion", "")[:40],
            ])
        for todo in (detail.get("todos") or [])[:5]:
            meta_rows.append([
                todo["content"][:30], "待办",
                todo.get("owner") or "待确认", todo.get("due_date") or "待确认",
            ])

    out_dir = paths.exports_dir(work_dir)
    path, actual = render(
        outline,
        out_dir=out_dir,
        topic=topic,
        meta_lines=meta_rows or None,
        meta_title="风险与待办跟踪",
        meta_headers=["事项", "类型", "负责人/等级", "截止/建议"],
        total_slides=requested,
    )
    store.add_activity("presentation", f"生成汇报 PPT：{path.name}", f"{topic}")
    return {
        "path": path,
        "filename": path.name,
        "slide_count": actual,
        "requested_slide_count": requested,
        "page_count_matches": actual == requested,
        "outline": outline,
    }
