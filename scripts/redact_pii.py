"""交付前 PII/本机路径脱敏工具。

处理两类内容：
1. 文档（docs/*.md、docs/*.txt）里的真实邮箱、本机绝对路径、用户名；
2. 截图（docs/screenshots/*.png）里显示出来的收件邮箱（在提示条上做遮罩）。

用法::

    python scripts/redact_pii.py --check    # 只检查，不修改
    python scripts/redact_pii.py            # 执行脱敏
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOC_GLOBS = ("docs/*.md", "docs/*.txt")

# 需要脱敏的文本替换（顺序敏感：先长后短）
RULES: list[tuple[str, str]] = [
    # 任何形如 AIEB_SMTP_PASSWORD=xxxx 的行都要打码（防止日志/文档里带上授权码）
    (r"(AIEB_SMTP_PASSWORD\s*=\s*)\S+", r"\1***"),
    (r"[A-Za-z0-9._%+-]+@163\.com", "w***@***.com"),
    (r"[A-Za-z0-9._%+-]+@qq\.com", "u***@***.com"),
    (r"<repo>", "."),
    (r"C:\\Users\\[A-Za-z0-9_.-]+\\AppData\\Local\\Programs\\Python\\Python\d+\\Lib\\site-packages",
     "<site-packages>"),
    (r"C:\\Users\\([A-Za-z0-9_.-]+)", r"<user-home>"),
    (r"pytest-of-([A-Za-z0-9_.-]+)", "pytest-of-<user>"),
    (r"<repo>", "<project-root>"),
]

# 截图里的邮箱遮罩区域（相对坐标，按 1440x900 的浏览器视口估算）
SHOT_TARGETS = {
    # 整条"邮件已发送至 xxx"提示条重绘为脱敏文案（底色取实测的提示条绿色）
    "17-email-sent.png": [(1162, 840, 1426, 869, "邮件已发送（收件地址已脱敏）", (6, 95, 70))],
}


def _read_text(path: Path) -> str:
    """读文本：兼容 UTF-8 / UTF-8-BOM / UTF-16（PowerShell Tee-Object 默认写 UTF-16）/ GBK。

    早期用 PowerShell ``Tee-Object`` 生成的日志是 UTF-16LE，直接按 UTF-8 读会变成
    NUL 分隔的乱码，脱敏规则匹配不到——所以这里做一次编码探测。
    """
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-16", "gb18030"):
        try:
            text = raw.decode(encoding)
        except (UnicodeDecodeError, UnicodeError):
            continue
        # 解码后如果存在大量替换字符或 NUL，说明选错编码了
        if "\ufffd" in text or "\x00" in text:
            continue
        return text
    return raw.decode("utf-8", errors="replace")


def redact_text(path: Path, apply: bool) -> list[str]:
    text = _read_text(path)
    original = text
    for pattern, repl in RULES:
        text = re.sub(pattern, repl, text)
    needs_rewrite = path.read_bytes()[:2] in (b"\xff\xfe", b"\xfe\xff")
    if text == original and not needs_rewrite:
        return []
    changed = sorted({m for pattern, _ in RULES for m in re.findall(pattern, original)})
    extra = "（兼转 UTF-8）" if needs_rewrite else ""
    if apply:
        path.write_text(text, encoding="utf-8")
    return [f"{path.relative_to(ROOT)}: {len(changed)} 类命中{extra}"]


def redact_image(path: Path, boxes: list[tuple], apply: bool) -> list[str]:
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return [f"{path.name}: 跳过（未安装 Pillow）"]
    if not apply:
        return [f"{path.name}: 需要遮罩 {len(boxes)} 处"]
    image = Image.open(path).convert("RGB")
    draw = ImageDraw.Draw(image)
    for box in boxes:
        left, top, right, bottom, label, fill = box
        draw.rectangle([left, top, right, bottom], fill=fill)
        try:
            font = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", 13)
        except OSError:
            font = ImageFont.load_default()
        draw.text((left + 8, top + 7), label, fill=(255, 255, 255), font=font)
    image.save(path)
    return [f"{path.name}: 已重绘 {len(boxes)} 处"]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="只检查不修改")
    args = parser.parse_args()
    apply = not args.check
    messages: list[str] = []

    for pattern in DOC_GLOBS:
        for path in sorted(ROOT.glob(pattern)):
            messages.extend(redact_text(path, apply))

    for name, boxes in SHOT_TARGETS.items():
        path = ROOT / "docs" / "screenshots" / name
        if path.is_file():
            messages.extend(redact_image(path, boxes, apply))

    if messages:
        print("\n".join(messages))
    else:
        print("未发现需要脱敏的内容。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
