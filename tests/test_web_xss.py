"""Web UI XSS 防护测试（P0-6）。

分两层：
  1. 静态契约：页面必须引入 DOMPurify、markdown 必须经消毒后才进 innerHTML；
  2. 真跑：用 node + jsdom + DOMPurify 执行 web_content.py 里**真实的** renderMarkdown，
     对一组经典 XSS payload 验证 event handler / javascript: URI / script 标签全部无效。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from aieb.web_content import INDEX_HTML  # noqa: E402

NODE_SCRIPT = REPO_ROOT / "tests" / "js" / "xss_sanitizer_check.mjs"
DEPS_DIR = Path.home() / ".cache" / "aieb_xss_deps"


# ---------------------------------------------------------------- 静态契约


def test_dompurify_is_loaded():
    assert "dompurify" in INDEX_HTML.lower()
    assert "DOMPurify.sanitize" in INDEX_HTML


def test_markdown_never_goes_straight_to_innerHTML():
    assert "innerHTML = marked.parse" not in INDEX_HTML
    assert "marked.parse(" in INDEX_HTML  # 仍在用 marked
    assert "SANITIZE_OPTIONS" in INDEX_HTML
    assert "renderer.html" in INDEX_HTML  # 原始 HTML token 被转义


def test_dangerous_tags_and_attrs_are_forbidden():
    for token in ("'script'", "'iframe'", "'svg'", "'onerror'", "'onload'"):
        assert token in INDEX_HTML, token


def test_token_never_goes_into_url_query():
    """token 走 fragment + subprotocol，不进 query（避免进日志/历史）。"""
    assert "aieb-token." in INDEX_HTML
    assert "?token=" not in INDEX_HTML
    assert "sessionStorage" in INDEX_HTML


# ---------------------------------------------------------------- 真实执行


def _ensure_node() -> str:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node 不可用，跳过真实 XSS 执行测试")
    return node


def _ensure_deps() -> Path:
    node_modules = DEPS_DIR / "node_modules"
    if (node_modules / "jsdom").exists() and (node_modules / "dompurify").exists() and (
        node_modules / "marked"
    ).exists():
        return DEPS_DIR
    DEPS_DIR.mkdir(parents=True, exist_ok=True)
    npm = shutil.which("npm")
    if npm is None:
        pytest.skip("npm 不可用，跳过真实 XSS 执行测试")
    proc = subprocess.run(
        [npm, "install", "--silent", "--no-fund", "--no-audit", "jsdom", "dompurify", "marked"],
        cwd=str(DEPS_DIR),
        capture_output=True,
        text=True,
        timeout=600,
    )
    if proc.returncode != 0:
        pytest.skip(f"npm install 失败（可能离线）: {proc.stderr[-200:]}")
    return DEPS_DIR


def test_xss_payloads_do_not_execute():
    node = _ensure_node()
    deps = _ensure_deps()
    shutil.copy2(NODE_SCRIPT, deps / NODE_SCRIPT.name)

    proc = subprocess.run(
        [node, NODE_SCRIPT.name, str(REPO_ROOT / "aieb" / "web_content.py")],
        cwd=str(deps),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
    )
    assert proc.stdout.strip(), proc.stderr[-2000:]
    result = json.loads(proc.stdout)

    failed = [item for item in result["payloads"] if not item["pass"]]
    assert not failed, json.dumps(failed, ensure_ascii=False, indent=2)
    assert result["ok"] is True

    # 明确核对题目要求的四类 payload 都在覆盖列表里
    payloads = " ".join(item["payload"] for item in result["payloads"])
    for expected in ("<script>", "onerror=", "javascript:", "<svg onload="):
        assert expected in payloads

    # 正常 markdown 仍然可用
    assert all(item["pass"] for item in result["benign"]), result["benign"]
