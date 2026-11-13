"""邮件发送：真实 SMTP，凭证只从环境变量读取，默认不自动发送。

安全约束
--------
* 配置（尤其是密码）只来自环境变量，绝不写进数据库、日志或前端响应；
* 没有配置 SMTP 时，**明确返回失败**并说明缺少哪些变量，不假装发送成功；
* 发送前后都记状态：``draft`` → ``sending`` → ``sent`` / ``failed``，失败原因人话化。
"""

from __future__ import annotations

import os
import smtplib
import ssl
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.message import EmailMessage
from email.utils import formataddr, getaddresses

ENV_PREFIX = "AIEB_SMTP_"

# 保护模式：置为 1 时拦截一切真实发送（自动化测试 / CI / 公开展示时使用）。
# 拦截时返回明确失败，**不伪造成功**。
DRY_RUN_ENV = "AIEB_EMAIL_DRY_RUN"
_TRUTHY = {"1", "true", "yes", "on"}


def dry_run_enabled() -> bool:
    """当前是否启用发送保护模式。"""
    return os.environ.get(DRY_RUN_ENV, "").strip().lower() in _TRUTHY


@dataclass
class SMTPConfig:
    host: str = ""
    port: int = 465
    user: str = ""
    password: str = ""
    sender: str = ""
    security: str = "ssl"  # ssl | starttls | none
    timeout: float = 20.0
    missing: list[str] = field(default_factory=list)

    @property
    def configured(self) -> bool:
        return bool(self.host and self.user and self.password and self.sender)

    def public_view(self) -> dict[str, object]:
        """前端可见的配置状态（不含任何凭证）。"""
        return {
            "configured": self.configured,
            "host": self.host,
            "port": self.port,
            "sender": self.sender,
            "security": self.security,
            "missing": self.missing,
            "dry_run": dry_run_enabled(),
        }


def load_config() -> SMTPConfig:
    def env(name: str, default: str = "") -> str:
        return os.environ.get(ENV_PREFIX + name, default).strip()

    security = env("SECURITY", "ssl").lower()
    if security not in {"ssl", "starttls", "none"}:
        security = "ssl"
    try:
        port = int(env("PORT", "465" if security == "ssl" else "587") or 0)
    except ValueError:
        port = 465 if security == "ssl" else 587
    try:
        timeout = float(env("TIMEOUT", "20") or 20)
    except ValueError:
        timeout = 20.0

    cfg = SMTPConfig(
        host=env("HOST"),
        port=port,
        user=env("USER"),
        password=env("PASSWORD"),
        sender=env("FROM") or env("USER"),
        security=security,
        timeout=timeout,
    )
    missing: list[str] = []
    for name, value in (("HOST", cfg.host), ("USER", cfg.user), ("PASSWORD", cfg.password), ("FROM", cfg.sender)):
        if not value:
            missing.append(ENV_PREFIX + name)
    cfg.missing = missing
    return cfg


def _friendly_error(exc: Exception) -> str:
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return "SMTP 认证失败：请检查账号与授权码（很多邮箱需要单独生成应用授权码）"
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        return "收件人被服务器拒绝，请确认邮箱地址是否正确"
    if isinstance(exc, smtplib.SMTPSenderRefused):
        return "发件人被服务器拒绝，请确认发件地址是否与账号匹配"
    if isinstance(exc, smtplib.SMTPConnectError):
        return "无法连接 SMTP 服务器，请检查主机与端口"
    if isinstance(exc, ssl.SSLError):
        return "SMTP TLS 握手失败，请检查 security 配置（ssl / starttls）"
    if isinstance(exc, TimeoutError) or isinstance(exc, smtplib.SMTPServerDisconnected):
        return "SMTP 连接超时或中断，请稍后重试"
    return f"发送失败：{exc}"


def send_email(*, to_addr: str, subject: str, body: str, cfg: SMTPConfig | None = None) -> dict[str, object]:
    """真实发送。返回 {ok, message, sent_at, error_code}。

    收件人不限于自己：填谁的地址就发给谁，多个地址用逗号/分号分隔均可。
    这里会先把地址拆开逐个校验（格式不对就当场报错，而不是让 SMTP 报一句
    看不懂的“收件人被拒”），再交给 smtplib 投递。
    """
    cfg = cfg or load_config()
    # 收件人分隔符统一成逗号：中文逗号/顿号/分号在中文输入法下很常见，
    # 用户不该因为标点不同就被判成"格式错误"。
    normalized = (
        (to_addr or "")
        .replace("；", ";").replace("，", ",").replace("、", ",")
        .replace(";", ",")
    )
    # getaddresses 同时支持 "a@x.com, b@y.com" 与 "Name <a@x.com>"
    recipients = [addr for _, addr in getaddresses([normalized]) if addr]
    if not recipients:
        return {"ok": False, "message": "请填写有效的收件人邮箱", "error_code": "invalid_recipient"}
    invalid = [a for a in recipients if "@" not in a or "." not in a.rsplit("@", 1)[-1]]
    if invalid:
        return {
            "ok": False,
            "message": "收件人地址格式不正确：" + "、".join(invalid),
            "error_code": "invalid_recipient",
        }
    if not cfg.configured:
        missing = "、".join(cfg.missing) or f"{ENV_PREFIX}HOST/USER/PASSWORD/FROM"
        return {
            "ok": False,
            "message": f"未配置 SMTP 邮件服务，缺少环境变量：{missing}",
            "error_code": "not_configured",
        }
    if dry_run_enabled():
        # 自动化验收 / CI / 公开展示：绝不真的把信投出去，也绝不假装成功。
        return {
            "ok": False,
            "message": (
                "已拦截真实发送：当前是发送保护模式"
                f"（{DRY_RUN_ENV}=1）。如需真实投递，请先取消该环境变量并重试。"
            ),
            "error_code": "send_blocked",
        }

    to_header = ", ".join(recipients)
    message = EmailMessage()
    message["From"] = formataddr(("AI 外脑 Demo", cfg.sender))
    message["To"] = to_header
    message["Subject"] = subject.strip() or "会议提醒"
    message.set_content(body)

    try:
        if cfg.security == "ssl":
            context = ssl.create_default_context()
            with smtplib.SMTP_SSL(cfg.host, cfg.port, timeout=cfg.timeout, context=context) as server:
                server.login(cfg.user, cfg.password)
                server.send_message(message)
        else:
            with smtplib.SMTP(cfg.host, cfg.port, timeout=cfg.timeout) as server:
                server.ehlo()
                if cfg.security == "starttls":
                    server.starttls(context=ssl.create_default_context())
                    server.ehlo()
                server.login(cfg.user, cfg.password)
                server.send_message(message)
    except Exception as exc:  # noqa: BLE001 - 所有 SMTP 异常都要转成人话
        return {
            "ok": False,
            "message": _friendly_error(exc),
            "error_code": "send_failed",
        }

    return {
        "ok": True,
        "message": (
            f"SMTP 已接受（{len(recipients)} 个收件人）：{to_header}"
            if len(recipients) > 1
            else f"SMTP 已接受：{to_header}（不代表已送达对方邮箱）"
        ),
        "recipients": recipients,
        "sent_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
