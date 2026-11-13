"""邮件发送：未配置/参数错误必须如实失败，不能假装成功。"""

from __future__ import annotations

import pytest

from aieb.brain import mailer


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch: pytest.MonkeyPatch):
    for key in ("HOST", "PORT", "USER", "PASSWORD", "FROM", "SECURITY", "TIMEOUT"):
        monkeypatch.delenv("AIEB_SMTP_" + key, raising=False)
    # 本文件用假 SMTP（monkeypatch 掉 smtplib），需要走通“发送流程”本身，
    # 因此显式关掉 dry-run；conftest 的全局保护仍然对其他测试文件生效。
    monkeypatch.delenv(mailer.DRY_RUN_ENV, raising=False)
    yield


def test_not_configured_reports_missing_env() -> None:
    result = mailer.send_email(to_addr="a@b.com", subject="s", body="b")
    assert result["ok"] is False
    assert result["error_code"] == "not_configured"
    assert "AIEB_SMTP_HOST" in str(result["message"])


def test_invalid_recipient_rejected() -> None:
    result = mailer.send_email(to_addr="not-an-email", subject="s", body="b")
    assert result["ok"] is False
    assert result["error_code"] == "invalid_recipient"


def test_config_public_view_hides_password(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AIEB_SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("AIEB_SMTP_USER", "u@example.com")
    monkeypatch.setenv("AIEB_SMTP_PASSWORD", "super-secret")
    monkeypatch.setenv("AIEB_SMTP_FROM", "u@example.com")
    cfg = mailer.load_config()
    assert cfg.configured is True
    view = cfg.public_view()
    assert "super-secret" not in str(view)
    assert "password" not in view


def test_send_failure_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    """连接一个必然失败的端口，验证失败原因被翻译成可读提示。"""
    import smtplib

    def boom(*args, **kwargs):
        raise smtplib.SMTPConnectError(421, "cannot connect")

    monkeypatch.setattr(smtplib, "SMTP_SSL", boom)
    cfg = mailer.SMTPConfig(host="127.0.0.1", port=1, user="u", password="p", sender="s@x.com")
    result = mailer.send_email(to_addr="a@b.com", subject="s", body="b", cfg=cfg)
    assert result["ok"] is False
    assert "SMTP" in str(result["message"])


def test_multiple_recipients_supported(monkeypatch: pytest.MonkeyPatch) -> None:
    """收件人可以是别人、也可以一次填多个：只校验格式，不限制地址。"""
    import smtplib

    sent: dict[str, object] = {}

    class FakeSMTP:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def login(self, user, password):
            sent["login"] = user

        def send_message(self, message):
            sent["to"] = message["To"]
            sent["from"] = message["From"]

    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSMTP)
    cfg = mailer.SMTPConfig(host="h", port=465, user="me@x.com", password="p",
                            sender="me@x.com", security="ssl")

    result = mailer.send_email(to_addr="a@b.com，c@d.com; e@f.com",
                               subject="s", body="b", cfg=cfg)
    assert result["ok"] is True
    assert result["recipients"] == ["a@b.com", "c@d.com", "e@f.com"]
    assert sent["to"] == "a@b.com, c@d.com, e@f.com"
    assert "3 个收件人" in str(result["message"])


def test_invalid_recipient_in_list_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AIEB_SMTP_HOST", "h")
    monkeypatch.setenv("AIEB_SMTP_USER", "u@x.com")
    monkeypatch.setenv("AIEB_SMTP_PASSWORD", "p")
    monkeypatch.setenv("AIEB_SMTP_FROM", "u@x.com")
    result = mailer.send_email(to_addr="a@b.com, bad", subject="s", body="b")
    assert result["ok"] is False
    assert result["error_code"] == "invalid_recipient"
    assert "bad" in str(result["message"])


def test_dry_run_blocks_real_sending(monkeypatch: pytest.MonkeyPatch) -> None:
    """保护模式必须拦下真实投递，并且不能伪造成成功。"""
    import smtplib

    def boom(*args, **kwargs):  # pragma: no cover - 走到这里就是事故发生
        raise AssertionError("保护模式下不应建立 SMTP 连接")

    monkeypatch.setattr(smtplib, "SMTP_SSL", boom)
    monkeypatch.setenv(mailer.DRY_RUN_ENV, "1")
    cfg = mailer.SMTPConfig(host="smtp.example.com", port=465, user="u@x.com",
                            password="p", sender="u@x.com")
    result = mailer.send_email(to_addr="a@b.com", subject="s", body="b", cfg=cfg)
    assert result["ok"] is False
    assert result["error_code"] == "send_blocked"
    assert mailer.DRY_RUN_ENV in str(result["message"])


def test_public_view_exposes_dry_run_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(mailer.DRY_RUN_ENV, "1")
    cfg = mailer.SMTPConfig(host="h", user="u@x.com", password="p", sender="u@x.com")
    assert cfg.public_view()["dry_run"] is True
