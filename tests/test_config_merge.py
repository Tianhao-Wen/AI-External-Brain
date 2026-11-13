"""配置层：boolean 合并语义（P1-1）与 remote 段（P0-4）。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from aieb.config import load_config  # noqa: E402

BASE_PROVIDER = """
providers:
  - name: {name}
    protocol: openai-compat
    base_url: https://example.invalid
    model: test-model
"""


def _write(path: Path, name: str, extra: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(BASE_PROVIDER.format(name=name) + extra, encoding="utf-8")
    return path


def test_explicit_false_overrides_true(tmp_path, monkeypatch):
    """user true + project false -> false（旧实现会被 truthiness 吃掉）。"""
    home = tmp_path / "home"
    project = tmp_path / "project"
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    _write(home / ".aieb" / "config.yaml", "home", "\nenable_fork: true\n")
    _write(project / ".aieb" / "config.yaml", "project", "\nenable_fork: false\n")
    monkeypatch.chdir(project)

    config = load_config()
    assert config.enable_fork is False


def test_unspecified_keeps_previous_layer(tmp_path, monkeypatch):
    home = tmp_path / "home"
    project = tmp_path / "project"
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    _write(home / ".aieb" / "config.yaml", "home", "\nenable_fork: true\n")
    _write(project / ".aieb" / "config.yaml", "project")  # 未指定
    monkeypatch.chdir(project)

    assert load_config().enable_fork is True


def test_default_false_then_project_true(tmp_path, monkeypatch):
    home = tmp_path / "home"
    project = tmp_path / "project"
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    _write(home / ".aieb" / "config.yaml", "home")  # 未指定 -> 默认 false
    _write(project / ".aieb" / "config.yaml", "project", "\nenable_verification_agent: true\n")
    monkeypatch.chdir(project)

    assert load_config().enable_verification_agent is True


def test_sandbox_fields_merge_independently(tmp_path, monkeypatch):
    home = tmp_path / "home"
    project = tmp_path / "project"
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    _write(home / ".aieb" / "config.yaml", "home", "\nsandbox:\n  enabled: true\n  auto_allow: true\n")
    _write(
        project / ".aieb" / "config.yaml",
        "project",
        "\nsandbox:\n  auto_allow: false\n",
    )
    monkeypatch.chdir(project)

    config = load_config()
    assert config.sandbox.enabled is True       # 未在 project 里写，保留 user
    assert config.sandbox.auto_allow is False   # 显式 false 生效
    assert config.sandbox.network_enabled is False


def test_permission_mode_unspecified_does_not_clobber(tmp_path, monkeypatch):
    home = tmp_path / "home"
    project = tmp_path / "project"
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    _write(home / ".aieb" / "config.yaml", "home", "\npermission_mode: plan\n")
    _write(project / ".aieb" / "config.yaml", "project")
    monkeypatch.chdir(project)

    assert load_config().permission_mode == "plan"


def test_remote_section_parsed(tmp_path):
    cfg = _write(
        tmp_path / "config.yaml",
        "solo",
        "\nremote:\n  host: 127.0.0.1\n  port: 19000\n  auth_token: abc\n  allowed_origins: ['http://x.test']\n",
    )
    config = load_config(cfg)
    assert config.remote.host == "127.0.0.1"
    assert config.remote.port == 19000
    assert config.remote.auth_token == "abc"
    assert config.remote.allowed_origins == ["http://x.test"]


def test_api_key_env_placeholder(tmp_path, monkeypatch):
    cfg = _write(tmp_path / "config.yaml", "solo", '\n')
    text = cfg.read_text(encoding="utf-8").replace(
        "model: test-model", 'model: test-model\n    api_key: "${MY_TEST_KEY}"'
    )
    cfg.write_text(text, encoding="utf-8")

    monkeypatch.setenv("MY_TEST_KEY", "sk-from-env-1234567890")  # audit:ignore (fake fixture)
    config = load_config(cfg)
    assert config.providers[0].resolve_api_key() == "sk-from-env-1234567890"  # audit:ignore (fake fixture)


def test_unexpanded_placeholder_falls_back_to_protocol_env(tmp_path, monkeypatch):
    cfg = _write(tmp_path / "config.yaml", "solo", "\n")
    text = cfg.read_text(encoding="utf-8").replace(
        "model: test-model", 'model: test-model\n    api_key: "${MISSING_KEY}"'
    )
    cfg.write_text(text, encoding="utf-8")

    monkeypatch.delenv("MISSING_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-fallback-key")  # audit:ignore (fake fixture)
    config = load_config(cfg)
    assert config.providers[0].resolve_api_key() == "sk-fallback-key"


def test_invalid_remote_port_rejected(tmp_path):
    from aieb.validator import ConfigError

    cfg = _write(tmp_path / "config.yaml", "solo", "\nremote:\n  port: 70000\n")
    with pytest.raises(ConfigError):
        load_config(cfg)
