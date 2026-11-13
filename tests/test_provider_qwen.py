"""阿里云百炼 Token Plan（OpenAI 兼容）接入的回归测试。

只验证配置解析、Web 展示与错误翻译；真实 API 调用在验收脚本里完成
（单元测试不联网，避免 CI 依赖外部服务）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from aieb.client import describe_provider_http_error  # noqa: E402
from aieb.config import load_config  # noqa: E402
from aieb.validator import ConfigError, validate_providers  # noqa: E402

WEB = (REPO_ROOT / "aieb" / "web_content.py").read_text(encoding="utf-8")
REMOTE = (REPO_ROOT / "aieb" / "remote.py").read_text(encoding="utf-8")

QWEN_BASE = "https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"


def _provider(**extra):
    base = {
        "name": "qwen3.8-max",
        "protocol": "openai-compat",
        "base_url": QWEN_BASE,
        "model": "qwen3.8-max",
        "api_key": "${ALIYUN_BAILIAN_API_KEY}",
        "max_output_tokens": 8192,
    }
    base.update(extra)
    return base


# ---------------------------------------------------------------- 配置


def test_display_name_is_optional():
    """没有 display_name 时不影响旧配置。"""
    out = validate_providers([
        {"name": "ds", "protocol": "openai-compat", "base_url": "https://x", "model": "m"}
    ])
    assert out[0]["display_name"] == ""


def test_display_name_is_parsed_and_stripped():
    out = validate_providers([_provider(display_name="  千问 Qwen3.8-Max（Token Plan）  ")])
    assert out[0]["display_name"] == "千问 Qwen3.8-Max（Token Plan）"


def test_display_name_must_be_string():
    with pytest.raises(ConfigError):
        validate_providers([_provider(display_name=123)])


def test_qwen_provider_keeps_others_intact(tmp_path):
    """新增千问不能覆盖 DeepSeek：三个 provider 都要在。"""
    cfg = (tmp_path / "config.yaml")
    cfg.write_text(
        f"""
providers:
  - name: deepseek-flash
    protocol: openai-compat
    base_url: https://api.deepseek.com
    model: deepseek-flash
    api_key: "${{DEEPSEEK_API_KEY}}"
  - name: deepseek-v4-pro
    protocol: openai-compat
    base_url: https://api.deepseek.com
    model: deepseek-v4-pro
    api_key: "${{DEEPSEEK_API_KEY}}"
  - name: qwen3.8-max
    display_name: "千问 Qwen3.8-Max（Token Plan）"
    protocol: openai-compat
    base_url: {QWEN_BASE}
    model: qwen3.8-max
    api_key: "${{ALIYUN_BAILIAN_API_KEY}}"
    thinking: false
    max_output_tokens: 8192
""",
        encoding="utf-8",
    )
    config = load_config(cfg)
    names = [p.name for p in config.providers]
    assert names == ["deepseek-flash", "deepseek-v4-pro", "qwen3.8-max"]

    qwen = config.providers[-1]
    assert qwen.model == "qwen3.8-max"
    assert qwen.protocol == "openai-compat"
    assert qwen.base_url == QWEN_BASE
    assert qwen.display_name == "千问 Qwen3.8-Max（Token Plan）"
    # thinking=false → 不沿用 64k 默认输出上限
    assert qwen.get_max_output_tokens() == 8192


def test_qwen_key_resolves_from_environment(tmp_path, monkeypatch):
    """密钥只从环境变量读取，配置文件里只有占位符。"""
    monkeypatch.setenv("ALIYUN_BAILIAN_API_KEY", "sk-test-value")
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        f"""
providers:
  - name: qwen3.8-max
    protocol: openai-compat
    base_url: {QWEN_BASE}
    model: qwen3.8-max
    api_key: "${{ALIYUN_BAILIAN_API_KEY}}"
""",
        encoding="utf-8",
    )
    provider = load_config(cfg).providers[0]
    assert provider.resolve_api_key() == "sk-test-value"


def test_unresolved_placeholder_is_not_treated_as_key(tmp_path, monkeypatch):
    """环境变量缺失时不能把字面量 ${...} 当 key 发出去。"""
    monkeypatch.delenv("ALIYUN_BAILIAN_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        f"""
providers:
  - name: qwen3.8-max
    protocol: openai-compat
    base_url: {QWEN_BASE}
    model: qwen3.8-max
    api_key: "${{ALIYUN_BAILIAN_API_KEY}}"
""",
        encoding="utf-8",
    )
    assert load_config(cfg).providers[0].resolve_api_key() == ""


# ---------------------------------------------------------------- Web 展示


def test_providers_payload_has_display_name_but_no_key():
    block = REMOTE[REMOTE.index('"type": "providers"'):]
    block = block[: block.index('"active"')]
    assert '"display_name"' in block
    # 绝不把密钥发给前端
    assert "api_key" not in block
    assert "resolve_api_key" not in block


def test_provider_select_uses_display_name():
    """选择器优先展示 display_name，并用 DOM 构建 option（不用 innerHTML 拼接）。"""
    start = WEB.index("function renderProviderSelect")
    block = WEB[start: WEB.index("providerSelect.addEventListener", start)]
    assert "p.display_name" in block
    assert "createElement('option')" in block
    assert ".innerHTML" not in block
    assert "opt.value = p.name" in block


# ---------------------------------------------------------------- 错误翻译


def test_error_401_points_to_env_var():
    msg = describe_provider_http_error("qwen", 401, "invalid api key", model="qwen3.8-max")
    assert "401" in msg
    assert "API Key" in msg
    assert "环境变量" in msg


def test_error_403_permission_vs_quota():
    plain = describe_provider_http_error("qwen", 403, "Forbidden")
    assert "没有访问权限" in plain
    assert "未开通" in plain

    quota = describe_provider_http_error("qwen", 403, "insufficient quota for this model")
    assert "额度" in quota


def test_error_404_model_not_found():
    msg = describe_provider_http_error("qwen", 404, "model_not_found")
    assert "模型不存在或未开通" in msg
    assert "model id" in msg


def test_error_429_rate_limit_and_quota():
    limited = describe_provider_http_error("qwen", 429, "rate limit exceeded")
    assert "限流" in limited
    assert "额度已用尽" not in limited

    exhausted = describe_provider_http_error("qwen", 429, "quota exceeded for today")
    assert "额度已用尽" in exhausted


def test_error_5xx_is_retryable_message():
    msg = describe_provider_http_error("qwen", 503, "unavailable")
    assert "暂时不可用" in msg


def test_error_messages_do_not_leak_key():
    """错误文本里不能出现完整 key。"""
    fake = "sk-sp-SECRETVALUE"
    msg = describe_provider_http_error("qwen", 401, f"invalid key {fake}")
    assert fake not in msg
