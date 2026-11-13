"""用真实 PowerPoint（COM）校验生成的 PPTX：页数、越界、文字溢出、导出预览图。

用法::

    python scripts/verify_pptx.py docs/e2e_download.pptx

输出：
    docs/pptx_verify_report.md
    docs/screenshots/ppt-slide-*.png
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPORT = ROOT / "docs" / "pptx_verify_report.md"
SHOTS = ROOT / "docs" / "screenshots"


def verify(path: Path, export: bool = True) -> dict:
    import win32com.client as win32  # type: ignore[import-untyped]

    result: dict = {"file": str(path), "issues": [], "slides": []}
    app = win32.Dispatch("PowerPoint.Application")
    presentation = None
    try:
        # ReadOnly + WithWindow=False：不弹窗、不修改源文件
        presentation = app.Presentations.Open(str(path), ReadOnly=True, WithWindow=False)
        result["slide_count"] = int(presentation.Slides.Count)
        slide_w = float(presentation.PageSetup.SlideWidth)
        slide_h = float(presentation.PageSetup.SlideHeight)
        result["slide_size"] = (slide_w, slide_h)

        for index in range(1, presentation.Slides.Count + 1):
            slide = presentation.Slides(index)
            info = {"index": index, "shapes_with_text": 0, "overflow": [], "out_of_bounds": [],
                    "titles": []}
            for shape in slide.Shapes:
                left, top = float(shape.Left), float(shape.Top)
                width, height = float(shape.Width), float(shape.Height)
                if (left < -1 or top < -1 or left + width > slide_w + 1
                        or top + height > slide_h + 1):
                    info["out_of_bounds"].append(shape.Name)
                try:
                    frame = shape.TextFrame2
                    has_text = frame.HasText
                except Exception:  # noqa: BLE001 - 图片等没有文本框
                    continue
                if not has_text:
                    continue
                info["shapes_with_text"] += 1
                text = frame.TextRange.Text
                if info["titles"] == [] and text.strip() and len(text) < 60:
                    info["titles"].append(text.strip()[:40])
                try:
                    bound = float(frame.TextRange.BoundHeight)
                    usable = height - 4  # 4pt 容差
                    # msoAutoSizeTextToFitShape=2 时 PowerPoint 会自动缩放，不算溢出
                    if frame.AutoSize != 2 and bound > usable and height > 20:
                        info["overflow"].append(
                            {"shape": shape.Name, "text_h": round(bound, 1),
                             "box_h": round(height, 1), "text": text[:40]}
                        )
                except Exception:  # noqa: BLE001
                    pass
            if export:
                SHOTS.mkdir(parents=True, exist_ok=True)
                target = SHOTS / f"ppt-slide-{index:02d}.png"
                slide.Export(str(target), "PNG", 1600, 900)
                info["image"] = str(target.relative_to(ROOT)).replace("\\", "/")
            result["slides"].append(info)
    finally:
        if presentation is not None:
            presentation.Close()
        try:
            app.Quit()
        except Exception:  # noqa: BLE001
            pass
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("pptx", nargs="?", default=str(ROOT / "docs" / "e2e_download.pptx"))
    parser.add_argument("--no-export", action="store_true")
    args = parser.parse_args()
    path = Path(args.pptx)
    if not path.is_file():
        print(f"找不到文件：{path}")
        return 2

    try:
        data = verify(path, export=not args.no_export)
    except Exception as exc:  # noqa: BLE001 - 机器没装 PowerPoint 时要明确说清
        print(f"无法用 PowerPoint 打开：{exc}")
        return 1

    lines = [
        "# PPTX 真实打开校验（Microsoft PowerPoint COM）",
        "",
        f"- 时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 文件：`{path.name}`（{path.stat().st_size} 字节）",
        f"- 页数：{data['slide_count']}",
        f"- 页面尺寸：{data['slide_size'][0]:.0f} × {data['slide_size'][1]:.0f} pt（16:9）",
        "",
        "| 页 | 含文字形状 | 首个标题 | 越界形状 | 文字溢出 | 预览图 |",
        "|---|---|---|---|---|---|",
    ]
    problems = 0
    for slide in data["slides"]:
        overflow = slide["overflow"]
        out = slide["out_of_bounds"]
        if overflow or out:
            problems += 1
        title = slide["titles"][0] if slide["titles"] else "—"
        lines.append(
            f"| {slide['index']} | {slide['shapes_with_text']} | {title} | "
            f"{'、'.join(out) or '无'} | "
            f"{'、'.join(o['shape'] for o in overflow) or '无'} | "
            f"`{slide.get('image', '—')}` |"
        )
    lines += ["", f"**存在问题页数：{problems} / {data['slide_count']}**", ""]
    REPORT.write_text("\n".join(lines), encoding="utf-8")

    print(f"页数={data['slide_count']} 尺寸={data['slide_size']} 问题页={problems}")
    for slide in data["slides"]:
        print(f"  p{slide['index']}: title={slide['titles'][:1]} overflow={slide['overflow']} "
              f"oob={slide['out_of_bounds']} img={slide.get('image')}")
    print(f"报告：{REPORT}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
