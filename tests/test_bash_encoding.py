"""Phase 2B-1：子进程输出解码的固定回归测试。

要求：尽量**正确还原**真实 stdout/stderr，而不是用 errors="ignore" 抹掉乱码。
"""

from __future__ import annotations

import asyncio
import locale
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from aieb.tools.bash import Bash  # noqa: E402
from aieb.tools.bash import Params as BashParams  # noqa: E402
from aieb.tools.encoding import candidate_encodings, decode_subprocess_output  # noqa: E402

IS_WINDOWS = sys.platform == "win32"


# ---------------------------------------------------------------- 纯解码单元测试


def test_ascii_roundtrip():
    result = decode_subprocess_output(b"hello world\n")
    assert result.text == "hello world\n"
    assert result.encoding_used == "utf-8"
    assert result.had_decode_error is False
    assert result.fallback_used is False


def test_utf8_chinese_roundtrip():
    payload = "中文输出：完成\n".encode("utf-8")
    result = decode_subprocess_output(payload)
    assert result.text == "中文输出：完成\n"
    assert result.encoding_used == "utf-8"
    assert "\ufffd" not in result.text


def test_local_codepage_chinese_is_recovered():
    """Windows 本地代码页（本机 OEMCP=936）的中文必须能被还原，而不是变成 U+FFFD。"""
    if not IS_WINDOWS:
        pytest.skip("本地代码页场景仅 Windows")
    codepage = f"cp{__import__('ctypes').windll.kernel32.GetOEMCP()}"
    sample = "驱动器 C 中的卷是 Windows-SSD"
    payload = sample.encode(codepage, errors="strict")
    result = decode_subprocess_output(payload)
    assert "\ufffd" not in result.text, f"应正确解码 {codepage}，实际 encoding_used={result.encoding_used}"
    assert "Windows-SSD" in result.text
    assert result.encoding_used != "utf-8"


def test_mixed_ascii_and_local_codepage():
    if not IS_WINDOWS:
        pytest.skip("本地代码页场景仅 Windows")
    codepage = f"cp{__import__('ctypes').windll.kernel32.GetOEMCP()}"
    payload = b"path: C:\\tmp\n" + "中文目录".encode(codepage)
    result = decode_subprocess_output(payload)
    assert "path: C:\\tmp" in result.text
    assert "\ufffd" not in result.text


def test_empty_and_bom():
    assert decode_subprocess_output(b"").text == ""
    assert decode_subprocess_output(None).text == ""
    assert decode_subprocess_output(b"\xef\xbb\xbfhello").text == "hello"


def test_undecodable_bytes_still_return_text():
    result = decode_subprocess_output(b"\xff\xfe\xfd")
    assert isinstance(result.text, str)
    assert result.had_decode_error is True
    assert result.fallback_used is True


def test_candidate_order_prefers_utf8():
    candidates = candidate_encodings()
    assert candidates[0] == "utf-8"
    assert len(candidates) == len(set(candidates))


# ---------------------------------------------------------------- 真实子进程测试


def _run(command: str, cwd: Path) -> tuple[object, object]:
    tool = Bash()
    tool.work_dir = str(cwd)
    result = asyncio.run(tool.execute(BashParams(command=command, timeout=30)))
    return result, tool


def test_bash_utf8_child_output_is_clean(tmp_path):
    result, tool = _run('python -c "print(\'中文 UTF-8 输出\')"', tmp_path)
    assert not result.is_error, result.output
    assert "\ufffd" not in result.output
    assert "UTF-8" in result.output
    assert tool.last_decode is not None
    assert tool.last_decode.encoding_used == "utf-8"


def test_bash_local_codepage_child_output_is_clean(tmp_path):
    if not IS_WINDOWS:
        pytest.skip("本地代码页场景仅 Windows")
    # 用 chcp 指定的 OEM 代码页输出中文，模拟 cmd 内部命令
    result, tool = _run('dir', tmp_path)
    assert tool.last_decode is not None
    assert result.output.count("\ufffd") == 0, (
        f"dir 输出不应有乱码，encoding_used={tool.last_decode.encoding_used}"
    )
    # dir 在空目录里会输出 "File Not Found"/"找不到文件" 之类；只要可读即可
    assert result.output.strip()


def test_invalid_unix_command_error_is_readable(tmp_path):
    """历史问题场景：`ls -la` 在 cmd.exe 下失败，错误信息必须可读。

    修复前整段是 U+FFFD，模型无法判断"命令不存在"，只能反复换 shell 方言重试。
    """
    if not IS_WINDOWS:
        pytest.skip("该场景依赖 Windows cmd.exe")
    result, tool = _run("ls -la", tmp_path)
    assert result.is_error
    assert tool.last_decode is not None
    assert "\ufffd" not in result.output, f"错误信息不应乱码，实际为 {result.output!r}"
    # cmd.exe 的典型提示包含 "不是内部或外部命令"，英文系统为 "not recognized"
    lowered = result.output.lower()
    assert ("不是内部或外部命令" in result.output) or ("not recognized" in lowered) or ("not found" in lowered)


def test_nonzero_exit_and_stderr_preserved(tmp_path):
    result, tool = _run('python -c "import sys; sys.stderr.write(\'boom\'); sys.exit(3)"', tmp_path)
    assert result.is_error
    assert "boom" in result.output
    assert "3" in result.output
    assert tool.last_decode is not None
    assert "\ufffd" not in result.output


def test_metadata_exposes_encoding(tmp_path):
    result, _ = _run('python -c "print(1)"', tmp_path)
    assert result.metadata is not None
    assert result.metadata.get("encoding_used") == "utf-8"
    assert result.metadata.get("fallback_used") is False
