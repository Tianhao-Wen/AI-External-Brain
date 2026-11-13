"""Secrets 扫描工具与仓库卫生（P1-3 / P1-4）。"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts.audit_secrets import mask, scan_file, scan_repo  # noqa: E402


def test_mask_never_returns_full_secret():
    secret = "sk-abcdefghijklmnopqrstuvwxyz0123456789"  # audit:ignore (fake fixture)
    masked = mask(secret)
    assert masked != secret
    assert secret not in masked
    assert masked.startswith("sk-a")
    assert masked.endswith("6789")


def _fake_key(prefix: str = "sk", tail: str = "abcdefghijklmnop1234567890") -> str:
    """生成一个明显是测试用的假 key（避免仓库扫描自命中）。"""
    return f"{prefix}-{tail}"


def test_detects_openai_style_key(tmp_path):
    target = tmp_path / "cfg.yaml"
    target.write_text(f'api_key: "{_fake_key("sk-proj")}"\n', encoding="utf-8")
    findings = scan_file(target)
    assert findings, "应当命中 sk- 前缀密钥"
    assert findings[0]["confidence"] == "high"
    assert _fake_key("sk-proj") not in findings[0]["masked"]


def test_detects_private_key_header(tmp_path):
    target = tmp_path / "id.txt"
    header = "-----BEGIN RSA " + "PRIVATE KEY-----"  # audit:ignore (fake fixture)
    target.write_text(header + "\nabc\n", encoding="utf-8")
    assert scan_file(target)


def test_detects_bearer_and_github_tokens(tmp_path):
    target = tmp_path / "notes.md"
    target.write_text(
        "Authorization: Bearer abcdefghijklmnopqrstuvwxyz012345\n"  # audit:ignore (fake fixture)
        "token: ghp_abcdefghijklmnopqrstuvwxyz0123456789\n"  # audit:ignore (fake fixture)
        ,
        encoding="utf-8",
    )
    types = {f["type"] for f in scan_file(target)}
    assert any("bearer" in t for t in types)
    assert any("github" in t for t in types)


def test_placeholders_are_not_reported(tmp_path):
    target = tmp_path / "cfg.yaml"
    target.write_text(
        'api_key: "${DEEPSEEK_API_KEY}"\n'
        'other: "your-api-key-here"\n'
        "# pragma: allowlist secret\n",
        encoding="utf-8",
    )
    assert scan_file(target) == []


def test_repository_has_no_high_confidence_secret():
    findings = scan_repo(REPO_ROOT)
    high = [f for f in findings if f["confidence"] == "high"]
    assert high == [], [(f["file"], f["line"], f["masked"]) for f in high]


def test_scanner_cli_masks_output(tmp_path):
    sample = tmp_path / "leak.yaml"
    sample.write_text(f'api_key: "{_fake_key("sk-live")}"\n', encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "audit_secrets.py"), "--root", str(tmp_path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert _fake_key("sk-live") not in proc.stdout
    assert "sk-l" in proc.stdout


def test_gitignore_covers_runtime_data_and_credentials():
    text = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    for entry in (
        ".env",
        ".aieb/config.yaml",
        ".aieb/sessions/",
        ".aieb/memory/",
        ".aieb/history",
        "*.pem",
        "*.key",
    ):
        assert entry in text, entry


def test_config_uses_env_placeholder_not_literal_key():
    cfg = REPO_ROOT / ".aieb" / "config.yaml"
    if not cfg.exists():
        return  # 未创建本地配置时跳过
    text = cfg.read_text(encoding="utf-8")
    assert "sk-" not in text or "${" in text
    for line in text.splitlines():
        if "api_key" in line:
            assert "${" in line, f"配置文件仍有明文 api_key: {line.split(':')[0]}"
