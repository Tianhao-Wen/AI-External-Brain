"""生成示例 PDF（供知识库演示与测试使用）。

用法::

    python scripts/make_sample_pdf.py

输出：``samples/产品需求说明书.pdf``（3 页，含可检索的具体条款与页码）。
"""

from __future__ import annotations

from pathlib import Path

import fitz

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "samples" / "产品需求说明书.pdf"

PAGES = [
    (
        "1. 项目背景与目标",
        [
            "本项目建设一套面向企业内部的知识与会议助手，代号「AI 外脑」。",
            "核心目标：把分散在 PDF 文档、会议记录与即时讨论中的信息，整理成可检索、可预警、可追踪的知识资产。",
            "一期覆盖知识库问答、会议分析、待办跟踪三项能力，二期扩展汇报材料自动生成。",
            "项目负责人为王海，业务对接人为李静。",
        ],
    ),
    (
        "2. 验收标准",
        [
            "验收条款 A：知识库单次检索（含答案生成）响应时间不得超过 2 秒，冷启动除外。",
            "验收条款 B：支持 PDF 与 TXT 两类文件上传，单个文件不超过 15MB。",
            "验收条款 C：回答必须给出文档名称与页码引用，无法检索时必须明确说明，不得编造内容。",
            "验收条款 D：会议分析结果必须包含主题、时间、地点、参与人、决策、风险与待办七类字段。",
            "验收条款 E：发送邮件必须经过用户确认，且发送结果需要如实反馈成功或失败原因。",
        ],
    ),
    (
        "3. 交付与运维",
        [
            "计划交付时间：2025 年 12 月 20 日，分两个里程碑验收。",
            "里程碑一（12 月 5 日）：知识库与会议分析可用，支持内部试运行。",
            "里程碑二（12 月 20 日）：待办跟踪、邮件提醒与 PPT 生成上线。",
            "运维联系人：张强，值班电话 010-8888 6666。",
            "安全要求：所有 API 密钥只能通过环境变量注入，禁止写入代码仓库。",
        ],
    ),
]


def build() -> Path:
    doc = fitz.open()
    for index, (title, lines) in enumerate(PAGES, start=1):
        page = doc.new_page()
        page.insert_text((72, 90), title, fontsize=20, fontname="china-s")
        y = 140
        for line in lines:
            page.insert_textbox(
                fitz.Rect(72, y, 520, y + 60),
                line,
                fontsize=12,
                fontname="china-s",
                lineheight=1.4,
            )
            y += 68
        page.insert_text((72, 760), f"第 {index} 页 / 共 {len(PAGES)} 页", fontsize=9, fontname="china-s")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(OUT))
    doc.close()
    return OUT


if __name__ == "__main__":
    path = build()
    print(f"已生成示例 PDF：{path}（{path.stat().st_size} 字节）")
