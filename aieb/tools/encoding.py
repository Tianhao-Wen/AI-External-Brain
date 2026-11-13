"""子进程输出解码：尽量**正确还原**真实 stdout/stderr，而不是抹掉乱码。

问题背景（Phase 2B-1 实测）：
  Windows 中文环境下 `cmd.exe` 的内部命令（dir/type/echo 出错信息等）输出 **OEM 代码页**
  （本机 OEMCP=936）编码的字节。原实现直接 `stdout.decode()`（默认 UTF-8）→
  几乎每个字节都变成 U+FFFD，模型完全读不懂工具返回了什么。

策略（顺序由实测确定，不硬编码 GBK）：
  1. UTF-8 strict —— 绝大多数自建脚本/现代工具走这条，最可靠；
  2. 控制台 OEM 代码页（`GetOEMCP()`，例如 936 / 437）—— cmd.exe 内部命令；
  3. 系统 ANSI 代码页（`GetACP()`）/ locale 首选编码 —— 部分程序按 ANSI 输出；
  4. 最后才 `errors="replace"` 兜底，并标记 `fallback_used`。

`encoding_used` / `had_decode_error` / `fallback_used` 都会返回，便于观测与回归测试。
"""

from __future__ import annotations

import codecs
import locale
import sys
from dataclasses import dataclass

__all__ = ["DecodeResult", "candidate_encodings", "decode_subprocess_output"]


@dataclass(frozen=True)
class DecodeResult:
    text: str
    encoding_used: str
    had_decode_error: bool
    fallback_used: bool

    def as_metadata(self) -> dict[str, object]:
        return {
            "encoding_used": self.encoding_used,
            "had_decode_error": self.had_decode_error,
            "fallback_used": self.fallback_used,
        }


def _console_codepages() -> list[int]:
    """查询当前系统的 OEM / ANSI 代码页（仅 Windows 有意义）。"""
    if sys.platform != "win32":
        return []
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        return [int(kernel32.GetOEMCP()), int(kernel32.GetACP())]
    except Exception:  # pragma: no cover - 非典型 Windows 环境
        return []


def _has_codec(name: str) -> bool:
    try:
        codecs.lookup(name)
        return True
    except LookupError:
        return False


def candidate_encodings() -> list[str]:
    """返回按优先级排列的解码候选（去重、去掉不可用项）。"""
    candidates: list[str] = ["utf-8"]

    for cp in _console_codepages():
        if cp:
            candidates.append(f"cp{cp}")

    try:
        preferred = locale.getpreferredencoding(False)
    except Exception:  # pragma: no cover
        preferred = ""
    if preferred:
        candidates.append(preferred)

    # 常见 UTF-8 别名放在最后作为等价补充（避免某些环境只认别名）
    candidates.append("utf-8-sig")

    ordered: list[str] = []
    for name in candidates:
        if name and name not in ordered and _has_codec(name):
            ordered.append(name)
    return ordered


def decode_subprocess_output(data: bytes | bytearray | None) -> DecodeResult:
    """把子进程原始字节解码成文本。

    返回的 ``text`` 一定是**最可信**的那一份：
    优先选择“严格解码成功”的候选，其中 UTF-8 排第一。
    """
    if not data:
        return DecodeResult(text="", encoding_used="utf-8", had_decode_error=False, fallback_used=False)

    raw = bytes(data)
    # 去掉 UTF-8 BOM，避免它泄漏进模型可见文本
    if raw.startswith(codecs.BOM_UTF8):
        raw = raw[len(codecs.BOM_UTF8) :]

    for index, encoding in enumerate(candidate_encodings()):
        try:
            text = raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
        return DecodeResult(
            text=text,
            encoding_used=encoding,
            # 第一个候选（UTF-8）失败后换用其它编码，就算发生过解码失败
            had_decode_error=index > 0,
            fallback_used=index > 0,
        )

    # 全部严格解码失败：只在此时才做有损兜底
    preferred = "utf-8"
    ordered = candidate_encodings()
    if ordered:
        preferred = ordered[min(1, len(ordered) - 1)]
    try:
        text = raw.decode(preferred, errors="replace")
    except LookupError:  # pragma: no cover
        text = raw.decode("utf-8", errors="replace")
        preferred = "utf-8"
    return DecodeResult(text=text, encoding_used=preferred, had_decode_error=True, fallback_used=True)
