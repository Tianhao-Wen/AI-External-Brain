"""前端端到端测试：用 jsdom 真跑产品页面的生产 JS。

与 ``test_brain_ui.py``（源码静态契约）互补：这里会真正执行
渲染、导航切换、RPC 回包与流式事件，验证界面不是"看起来有"而是真的能用。
缺少 node/npm 时自动跳过，不会伪造通过。
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from aieb.brain.ui import render_index  # noqa: E402

NODE_SCRIPT = REPO_ROOT / "tests" / "js" / "ui_smoke.mjs"
DEPS_DIR = Path.home() / ".cache" / "aieb_ui_deps"


def _ensure_deps() -> Path:
    node_modules = DEPS_DIR / "node_modules" / "jsdom"
    if node_modules.exists():
        return DEPS_DIR
    npm = shutil.which("npm")
    if npm is None:
        pytest.skip("npm 不可用，跳过前端端到端测试")
    DEPS_DIR.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [npm, "install", "--silent", "--no-fund", "--no-audit", "jsdom"],
        cwd=str(DEPS_DIR), capture_output=True, text=True, timeout=600,
    )
    if proc.returncode != 0:
        pytest.skip(f"npm install 失败（可能离线）: {proc.stderr[-200:]}")
    return DEPS_DIR


def test_ui_end_to_end(tmp_path: Path) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node 不可用，跳过前端端到端测试")
    deps = _ensure_deps()
    shutil.copy2(NODE_SCRIPT, deps / NODE_SCRIPT.name)
    html_file = tmp_path / "index.html"
    html_file.write_text(render_index("0.1.0"), encoding="utf-8")

    proc = subprocess.run(
        [node, NODE_SCRIPT.name, str(html_file)],
        cwd=str(deps), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=300,
    )
    output = proc.stdout + proc.stderr
    passed = len(re.findall(r"^PASS\t", output, re.MULTILINE))
    assert proc.returncode == 0, output
    assert passed >= 15, f"通过的前端断言过少：\n{output}"
