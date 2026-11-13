"""一次性迁移脚本：把仓库里的真实 API key 替换为 ${ENV} 引用 / <redacted>。

行为：
  1. 从 .aieb/config.yaml 读出当前真实 key（只在内存里用）；
  2. 把 config.yaml 中的 api_key 值改为 ${DEEPSEEK_API_KEY}（保留其它字段）；
  3. 扫描仓库其它文件（benchmark traces 等），把出现的 key 原文替换为 <redacted-secret>。

输出只包含“文件名 + 次数”，绝不打印 key 内容。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG = REPO_ROOT / ".aieb" / "config.yaml"
SKIP_DIRS = {".git", ".conda", "__pycache__", "node_modules", ".venv", "site-packages", "pkgs"}
SKIP_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".pdf", ".zip", ".pyc", ".so", ".dll", ".exe"}


def main() -> None:
    if not CONFIG.exists():
        print("no .aieb/config.yaml")
        return
    original = CONFIG.read_text(encoding="utf-8")
    keys = sorted(set(re.findall(r"sk-[A-Za-z0-9_\-]{16,}", original)))
    if not keys:
        print("no api key literal found in .aieb/config.yaml")
    else:
        print(f"found {len(keys)} distinct key(s) in .aieb/config.yaml")

    rewritten = re.sub(
        r'(?m)^(\s*api_key:\s*)(["\']?)sk-[A-Za-z0-9_\-]{16,}\2\s*$',
        r'\1"${DEEPSEEK_API_KEY}"',
        original,
    )
    if rewritten != original:
        CONFIG.write_text(rewritten, encoding="utf-8")
        print("rewrote .aieb/config.yaml -> api_key: ${DEEPSEEK_API_KEY}")

    redacted_files = 0
    redacted_hits = 0
    for path in REPO_ROOT.rglob("*"):
        if not path.is_file():
            continue
        if set(path.parts) & SKIP_DIRS:
            continue
        if path.suffix.lower() in SKIP_SUFFIXES:
            continue
        if path.resolve() == Path(__file__).resolve():
            continue
        try:
            if path.stat().st_size > 8 * 1024 * 1024:
                continue
            text = path.read_text(encoding="utf-8", errors="strict")
        except (OSError, UnicodeDecodeError):
            continue
        count = sum(text.count(k) for k in keys)
        if not count:
            continue
        for k in keys:
            text = text.replace(k, "<redacted-secret>")
        path.write_text(text, encoding="utf-8")
        redacted_files += 1
        redacted_hits += count
        print(f"  redacted {count} occurrence(s) in {path.relative_to(REPO_ROOT)}")

    print(f"done: {redacted_files} file(s), {redacted_hits} occurrence(s) redacted")
    if redacted_hits:
        print("NOTE: rotate/revoke this key at the provider — redaction does not undo exposure.")


if __name__ == "__main__":
    sys.exit(main())
