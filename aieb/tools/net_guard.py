"""联网工具的安全底座：URL 校验（SSRF 防护）与受限的 HTTP 抓取。

## 为什么单独一层

联网工具把"外部世界"接进 Agent，风险不在模型，而在地址：

* ``http://127.0.0.1:18888/``、``http://192.168.1.1/`` —— 内网服务；
* ``http://169.254.169.254/latest/meta-data/`` —— 云主机元数据（可拿到临时凭证）；
* ``file:///C:/Users/...`` —— 本地文件；
* 一个公网域名解析到私网 IP（DNS rebinding）；
* 302 跳转到上面这些地址。

所以这里做三件事：

1. **解析前后各校验一次**：先看字面主机名，再把 DNS 解析出的每个 IP 都过一遍
   ``ipaddress`` 判定——只要有一个不是全球可路由地址就拒绝。
2. **重定向手动跟随**：每一跳都重新走完整校验，最多 5 跳。
3. **资源上限**：单次响应字节数、总耗时、跟随跳数都有硬上限。

本模块不做任何"绕过"：不解验证码、不带 Cookie、不自动登录。
"""

from __future__ import annotations

import gzip
import gzip
import io
import ipaddress
import socket
import time
import urllib.error
import urllib.request
import zlib
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

__all__ = [
    "FetchResult",
    "UrlRejected",
    "MAX_REDIRECTS",
    "validate_url",
    "resolve_public_addresses",
    "fetch_url",
    "decode_body",
    "html_to_text",
]


class UrlRejected(Exception):
    """URL 不满足安全要求（SSRF/协议/格式）。"""


MAX_REDIRECTS = 5
DEFAULT_MAX_BYTES = 2 * 1024 * 1024  # 2MB
DEFAULT_TIMEOUT = 20.0
DEFAULT_MAX_CHARS = 6000

# 明确拒绝的主机名（大小写不敏感）。这些通常解析到内网或元数据服务。
_BLOCKED_HOSTNAMES = {
    "localhost",
    "localhost.localdomain",
    "ip6-localhost",
    "ip6-loopback",
    "metadata",
    "metadata.google.internal",
    "metadata.goog",
    "instance-data",
    "instance-data.ec2.internal",
}

# 云厂商元数据服务的常见地址（IPv4 / IPv6）
_BLOCKED_IPS = {
    ipaddress.ip_address("169.254.169.254"),
    ipaddress.ip_address("169.254.170.2"),
    ipaddress.ip_address("100.100.100.200"),
    ipaddress.ip_address("fd00:ec2::254"),
}

_ALLOWED_SCHEMES = {"http", "https"}
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0 Safari/537.36 AIEB/1.0"
)
_USER_AGENT = USER_AGENT


@dataclass
class FetchResult:
    url: str            # 最终地址（跟随重定向之后）
    status: int
    content_type: str
    text: str
    truncated: bool


def _check_ip(ip: ipaddress._BaseAddress, host: str) -> None:
    if ip in _BLOCKED_IPS:
        raise UrlRejected(f"拒绝访问云元数据地址：{host}")
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        _check_ip(ip.ipv4_mapped, host)
        return
    if (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    ):
        raise UrlRejected(f"拒绝访问内网/回环地址：{host}（{ip}）")


def validate_url(url: str) -> str:
    """只做**字面**校验（协议、主机名、端口、凭据）。不解析 DNS。"""
    text = (url or "").strip()
    if not text:
        raise UrlRejected("URL 不能为空")
    if len(text) > 2000:
        raise UrlRejected("URL 过长")

    parts = urlsplit(text)
    scheme = (parts.scheme or "").lower()
    if scheme not in _ALLOWED_SCHEMES:
        raise UrlRejected(
            f"只支持 http/https 网页地址，收到的是：{scheme or '（无协议）'}"
        )
    if parts.username or parts.password:
        raise UrlRejected("URL 中不能包含账号密码")

    host = (parts.hostname or "").strip().lower().rstrip(".")
    if not host:
        raise UrlRejected("URL 缺少主机名")
    if host in _BLOCKED_HOSTNAMES:
        raise UrlRejected(f"拒绝访问本机地址：{host}")
    if host.endswith(".localhost") or host.endswith(".internal") or host.endswith(".local"):
        raise UrlRejected(f"拒绝访问本地域：{host}")

    # 直接给 IP 的情况：这里也要拦一次（DNS 解析对字面 IP 也会返回同一地址，
    # 但那一步在 validate_url 之外，单独调用本函数时仍需保证安全）
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        _check_ip(literal, host)

    port = parts.port
    if port is not None and not (0 < port < 65536):
        raise UrlRejected(f"端口不合法：{port}")

    return text


def resolve_public_addresses(host: str) -> list[str]:
    """解析主机名，**所有**返回地址都必须是公网可路由地址。"""
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError) as exc:
        raise UrlRejected(f"无法解析域名：{host}") from exc

    addresses: list[str] = []
    for info in infos:
        sockaddr = info[4]
        if not sockaddr:
            continue
        raw = str(sockaddr[0])
        try:
            ip = ipaddress.ip_address(raw)
        except ValueError:
            continue
        _check_ip(ip, host)
        if raw not in addresses:
            addresses.append(raw)

    if not addresses:
        raise UrlRejected(f"无法解析域名：{host}")
    return addresses


def check_url(url: str) -> str:
    """完整校验：字面 + DNS。返回规范化后的 URL。"""
    text = validate_url(url)
    host = urlsplit(text).hostname or ""
    try:
        ipaddress.ip_address(host)
    except ValueError:
        resolve_public_addresses(host)
    return text


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """禁止 urllib 自动跟随重定向——我们要自己逐跳校验。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102, ANN001
        return None


def _open(url: str, timeout: float, max_bytes: int) -> tuple[int, str, bytes, str]:
    """发起一次不带重定向的请求，返回 (status, content_type, body, location)。"""
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": _USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.5",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            # 只声明 gzip/deflate：brotli 需要额外依赖，声明了也解不开
            "Accept-Encoding": "gzip, deflate",
        },
    )
    opener = urllib.request.build_opener(_NoRedirect())
    try:
        response = opener.open(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        # 3xx 会被 _NoRedirect 转成 HTTPError
        if 300 <= exc.code < 400:
            return exc.code, exc.headers.get("Content-Type", ""), b"", exc.headers.get("Location", "") or ""
        raise
    with response:
        status = getattr(response, "status", 200)
        content_type = response.headers.get("Content-Type", "")
        content_encoding = (response.headers.get("Content-Encoding", "") or "").lower()
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = response.read(64 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total >= max_bytes:
                break
    body = _decompress(b"".join(chunks)[:max_bytes], content_encoding, max_bytes)
    return status, content_type, body, ""


def _decompress(body: bytes, encoding: str, max_bytes: int) -> bytes:
    """处理 Content-Encoding；解压后仍按 max_bytes 截断（防压缩炸弹）。"""
    if not body:
        return body
    try:
        if "gzip" in encoding or body[:2] == b"\x1f\x8b":
            with gzip.GzipFile(fileobj=io.BytesIO(body)) as fh:
                return fh.read(max_bytes)
        if "deflate" in encoding:
            try:
                return zlib.decompress(body, max_length=max_bytes)
            except zlib.error:
                return zlib.decompress(body, -zlib.MAX_WBITS, max_length=max_bytes)
        if "br" in encoding or "zstd" in encoding:
            raise RuntimeError("服务器返回了不支持的压缩格式（br/zstd），无法解码正文")
    except (OSError, EOFError, zlib.error):
        # 解压失败就把原始字节交回去，由上层判断是否可读
        return body
    return body


def fetch_url(
    url: str,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    max_bytes: int = DEFAULT_MAX_BYTES,
    max_redirects: int = MAX_REDIRECTS,
) -> FetchResult:
    """抓取一个公开网页。每一跳都重新做 SSRF 校验，并限制跳数与字节数。"""
    current = check_url(url)
    deadline = time.monotonic() + timeout
    redirects = 0

    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("请求超时")
        try:
            status, content_type, body, location = _open(current, remaining, max_bytes)
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"HTTP {exc.code}: {exc.reason}") from exc
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, socket.timeout):
                raise TimeoutError("请求超时") from exc
            raise RuntimeError(f"网络错误：{reason}") from exc
        except socket.timeout as exc:
            raise TimeoutError("请求超时") from exc

        if 300 <= status < 400:
            if not location:
                raise RuntimeError(f"重定向缺少 Location（HTTP {status}）")
            redirects += 1
            if redirects > max_redirects:
                raise RuntimeError(f"重定向次数过多（超过 {max_redirects} 次）")
            current = check_url(urljoin(current, location))
            continue

        truncated = len(body) >= max_bytes
        return FetchResult(
            url=current,
            status=status,
            content_type=content_type,
            text=decode_body(body, content_type),
            truncated=truncated,
        )


def _sniff_charset(body: bytes) -> str:
    head = body[:4096].lower()
    for marker in (b"charset=", b"charset =\""):
        idx = head.find(marker)
        if idx < 0:
            continue
        tail = head[idx + len(marker): idx + len(marker) + 40]
        tail = tail.split(b">")[0].split(b"\"")[0].split(b"'")[0].split(b";")[0].strip()
        try:
            return tail.decode("ascii").strip()
        except UnicodeDecodeError:
            continue
    return ""


def decode_body(body: bytes, content_type: str) -> str:
    """尽量正确地解码网页字节流（含常见中文编码）。

    顺序：HTTP 头里的 charset → HTML meta 里的 charset → utf-8 → gb18030 →
    big5 → latin-1（保底，绝不抛异常）。
    """
    candidates: list[str] = []
    lower = (content_type or "").lower()
    idx = lower.find("charset=")
    if idx >= 0:
        raw = content_type[idx + len("charset="):].split(";")[0].strip().strip('"\'')
        if raw:
            candidates.append(raw)
    sniffed = _sniff_charset(body)
    if sniffed:
        candidates.append(sniffed)
    candidates.extend(["utf-8", "gb18030", "big5", "latin-1"])

    seen: set[str] = set()
    for encoding in candidates:
        key = encoding.lower().replace("_", "-")
        if key in seen:
            continue
        seen.add(key)
        if key in ("gb2312", "gbk"):
            key = "gb18030"  # gb18030 是超集，解码更宽容
        try:
            return body.decode(key, errors="strict")
        except (UnicodeDecodeError, LookupError):
            continue
    return body.decode("utf-8", errors="replace")


# 这些标签里的文字不是"正文"，直接丢弃
_SKIP_TAGS = {
    "script", "style", "noscript", "svg", "canvas", "template",
    "nav", "footer", "aside", "form", "iframe", "video", "audio",
    "button", "select", "option", "header",
}
_BLOCK_TAGS = {
    "p", "div", "section", "article", "br", "li", "tr", "h1", "h2", "h3",
    "h4", "h5", "h6", "blockquote", "pre", "table", "ul", "ol", "dd", "dt",
}


class _TextExtractor:
    """把 HTML 变成可读正文：去脚本/样式/导航，按块级标签断行。"""

    def __init__(self) -> None:
        self.parts: list[str] = []
        self.title = ""
        self._skip_depth = 0
        self._in_title = False

    def handle(self, tag: str, attrs, start: bool, data: str = "") -> None:  # noqa: ANN001
        if tag == "title":
            self._in_title = start
            return
        if tag in _SKIP_TAGS:
            self._skip_depth += 1 if start else -1
            self._skip_depth = max(0, self._skip_depth)
            return
        if self._skip_depth:
            return
        if tag in _BLOCK_TAGS:
            self.parts.append("\n")
        if data:
            if self._in_title:
                self.title += data
            else:
                self.parts.append(data)

    def text(self) -> str:
        raw = "".join(self.parts)
        lines = []
        for line in raw.splitlines():
            collapsed = " ".join(line.split())
            if collapsed:
                lines.append(collapsed)
        # 合并过短的碎片行（导航残留常是几个字一行）
        merged: list[str] = []
        for line in lines:
            if merged and len(line) < 20 and not line.endswith(("。", ".", "！", "!", "？", "?")):
                merged[-1] = merged[-1] + " " + line
            else:
                merged.append(line)
        return "\n".join(merged)


def html_to_text(html: str) -> tuple[str, str]:
    """返回 (标题, 正文)。纯文本内容（非 HTML）原样返回。"""
    from html.parser import HTMLParser

    lowered = html[:2048].lower()
    if "<html" not in lowered and "<body" not in lowered and "<p" not in lowered:
        return "", " ".join(html.split())

    extractor = _TextExtractor()

    class _Parser(HTMLParser):
        def handle_starttag(self, tag, attrs):  # noqa: ANN001
            extractor.handle(tag, attrs, True)

        def handle_startendtag(self, tag, attrs):  # noqa: ANN001
            extractor.handle(tag, attrs, True)
            extractor.handle(tag, attrs, False)

        def handle_endtag(self, tag):  # noqa: ANN001
            extractor.handle(tag, None, False)

        def handle_data(self, data):  # noqa: ANN001
            extractor.handle("", None, True, data)

    parser = _Parser(convert_charrefs=True)
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # noqa: BLE001 - 畸形 HTML 不应该让工具崩掉
        return "", " ".join(html.split())
    title = " ".join(extractor.title.split())
    return title, extractor.text()
