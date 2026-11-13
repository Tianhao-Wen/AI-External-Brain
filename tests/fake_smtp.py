"""本地 Fake SMTP 服务器：让测试走**真实 smtplib 路径**，但信件只会落到内存里。

为什么不直接用 `AIEB_EMAIL_DRY_RUN=1`：项目里的保护模式是"拦截并返回失败"，
不会伪造成功（这是对的）。但那样就没法验证"发送成功 → 邮件状态变 sent"这条链路。
所以测试用这个假服务器接住 SMTP 会话：

* 走的是生产同一套 `smtplib` 代码（EHLO / AUTH LOGIN / MAIL / RCPT / DATA）；
* 邮件内容只保存在内存，**不会真的投递**；
* 支持统计"发了几封"、"发给谁"，用于断言"没有重复副作用"。

用法：
    server = FakeSMTPServer()
    port = server.start()
    ... os.environ["AIEB_SMTP_PORT"] = str(port) ...
    server.messages  # [{"to": [...], "subject": "...", "body": "..."}]
    server.stop()
"""

from __future__ import annotations

import base64
import re
import socketserver
import threading
from dataclasses import dataclass, field
from email import message_from_bytes, policy
from typing import Any


@dataclass
class CapturedMail:
    mail_from: str = ""
    rcpt_to: list[str] = field(default_factory=list)
    subject: str = ""
    body: str = ""
    raw: bytes = b""

    def as_dict(self) -> dict[str, Any]:
        return {
            "from": self.mail_from,
            "to": list(self.rcpt_to),
            "subject": self.subject,
            "body": self.body,
        }


class _Handler(socketserver.StreamRequestHandler):
    def _send(self, text: str) -> None:
        self.wfile.write((text + "\r\n").encode())

    def handle(self) -> None:  # noqa: C901 - SMTP 状态机，直白写更好读
        server: FakeSMTPServer = self.server.fake  # type: ignore[attr-defined]
        self._send("220 fake-smtp ready")
        mail = CapturedMail()
        while True:
            line = self.rfile.readline()
            if not line:
                return
            command = line.decode("utf-8", "replace").strip()
            upper = command.upper()

            if upper.startswith("EHLO"):
                self.wfile.write(b"250-fake-smtp\r\n250-AUTH LOGIN PLAIN\r\n250 OK\r\n")
            elif upper.startswith("HELO"):
                self._send("250 fake-smtp")
            elif upper.startswith("AUTH LOGIN"):
                self._send("334 VXNlcm5hbWU6")  # Username:
                self.rfile.readline()
                self._send("334 UGFzc3dvcmQ6")  # Password:
                self.rfile.readline()
                self._send("235 authentication succeeded")
            elif upper.startswith("AUTH PLAIN"):
                maybe = command.split(" ", 2)
                if len(maybe) < 3:
                    self._send("334 ")
                    self.rfile.readline()
                self._send("235 authentication succeeded")
            elif upper.startswith("MAIL FROM"):
                match = re.search(r"<([^>]*)>", command)
                mail.mail_from = match.group(1) if match else ""
                self._send("250 OK")
            elif upper.startswith("RCPT TO"):
                match = re.search(r"<([^>]*)>", command)
                if match:
                    mail.rcpt_to.append(match.group(1))
                self._send("250 OK")
            elif upper == "DATA":
                self._send("354 end with <CRLF>.<CRLF>")
                chunks: list[bytes] = []
                while True:
                    chunk = self.rfile.readline()
                    if not chunk or chunk in {b".\r\n", b".\n"}:
                        break
                    chunks.append(chunk[1:] if chunk.startswith(b"..") else chunk)
                raw = b"".join(chunks)
                mail.raw = raw
                parsed = message_from_bytes(raw, policy=policy.default)
                mail.subject = str(parsed.get("Subject") or "")
                try:
                    payload = parsed.get_content()
                    mail.body = payload if isinstance(payload, str) else str(payload)
                except Exception:  # noqa: BLE001
                    mail.body = raw.decode("utf-8", "replace")
                server.messages.append(mail)
                self._send("250 queued")
                mail = CapturedMail()
            elif upper == "RSET":
                mail = CapturedMail()
                self._send("250 OK")
            elif upper == "NOOP":
                self._send("250 OK")
            elif upper == "QUIT":
                self._send("221 bye")
                return
            else:
                self._send("250 OK")


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class FakeSMTPServer:
    """线程化的假 SMTP 服务器（可重复 start/stop，端口随机）。"""

    def __init__(self, host: str = "127.0.0.1", port: int = 0) -> None:
        self.host = host
        self._requested_port = port
        self._server: _Server | None = None
        self._thread: threading.Thread | None = None
        self.messages: list[CapturedMail] = []
        self.port = 0

    def start(self) -> int:
        self._server = _Server((self.host, self._requested_port), _Handler)
        self._server.fake = self  # type: ignore[attr-defined]
        self.port = int(self._server.server_address[1])
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="fake-smtp", daemon=True
        )
        self._thread.start()
        return self.port

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    # -- 环境变量（让 mailer.load_config 读到本服务器） -----------------

    def env(self, sender: str = "bot@example.com") -> dict[str, str]:
        return {
            "AIEB_SMTP_HOST": self.host,
            "AIEB_SMTP_PORT": str(self.port),
            "AIEB_SMTP_USER": "bot@example.com",
            "AIEB_SMTP_PASSWORD": base64.b64encode(b"fake-pass").decode(),
            "AIEB_SMTP_FROM": sender,
            "AIEB_SMTP_SECURITY": "none",
            "AIEB_EMAIL_DRY_RUN": "0",
        }

    def __enter__(self) -> "FakeSMTPServer":
        self.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.stop()
