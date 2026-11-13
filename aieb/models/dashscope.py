"""百炼 Token Plan 的 HTTP 客户端。

只做三件事：拼地址、发请求、把各种错误翻译成 ``ErrorCode``。

地址约定（实测，不是猜的）：
* 兼容模式（OpenAI 语义）：``{root}/compatible-mode/v1/...``
* 百炼原生 API：``{root}/api/v1/...``
* 实时语音 WebSocket：``wss://{host}/api-ws/v1/realtime?model=...``

配置里现有 provider 的 ``base_url`` 是兼容模式地址，这里把它规约成 ``root``，
既能复用同一份 Key，又不用新增一套配置。
"""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aieb.models.result import ErrorCode

DEFAULT_TIMEOUT = 60.0
#: 单个下载文件的上限（生成的图片/视频/音频都远小于此值）
MAX_DOWNLOAD_BYTES = 200 * 1024 * 1024
USER_AGENT = "AIEB/1.0 (+media-capability)"


@dataclass
class ProviderResponse:
    status: int
    payload: dict[str, Any]
    request_id: str = ""

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def error_code(self) -> ErrorCode:
        return classify_http_status(self.status)

    def provider_message(self) -> str:
        """从各种形状的错误体里挖出官方 message（只进 detail）。"""
        for key in ("message", "code"):
            value = self.payload.get(key)
            if isinstance(value, str) and value:
                return value
        error = self.payload.get("error")
        if isinstance(error, dict):
            for key in ("message", "code"):
                value = error.get(key)
                if isinstance(value, str) and value:
                    return value
        return ""


def classify_http_status(status: int) -> ErrorCode:
    if status in (401,):
        return ErrorCode.AUTH_FAILED
    if status == 403:
        # 百炼对"套餐不覆盖 / 不支持该调用方式"也返回 403
        return ErrorCode.QUOTA_EXCEEDED
    if status == 429:
        return ErrorCode.RATE_LIMITED
    if status == 404:
        return ErrorCode.MODEL_NOT_SUPPORTED
    if status in (400, 422):
        return ErrorCode.INVALID_ARGUMENT
    if status in (408, 504):
        return ErrorCode.TIMEOUT
    if 500 <= status < 600:
        return ErrorCode.PROVIDER_ERROR
    return ErrorCode.PROVIDER_ERROR


def normalize_error(message: str) -> ErrorCode:
    """从官方错误文案里再细分一次（比 HTTP 码更准）。"""
    text = (message or "").lower()
    # 内容审核优先：百炼对 IP/版权/敏感内容的返回值是 400 +
    # "Output data is suspected of being involved in IP infringement" 之类，
    # 这类失败重试没有意义，必须让用户知道是描述被判违规了。
    if any(
        key in text
        for key in (
            "ip infringement",
            "datainspectionfailed",
            "data inspection",
            "inappropriate content",
            "sensitive content",
            "content policy",
            "内容审核",
            "涉嫌违规",
        )
    ):
        return ErrorCode.CONTENT_BLOCKED
    if "model not exist" in text or "model_not_found" in text or "model not found" in text:
        return ErrorCode.MODEL_NOT_SUPPORTED
    if "url error" in text:
        # 百炼的万能错误：端点/模型组合不对，或该模型不支持这种调用
        return ErrorCode.MODEL_NOT_SUPPORTED
    if "does not support synchronous calls" in text or "does not support asynchronous calls" in text:
        return ErrorCode.MODEL_NOT_SUPPORTED
    if "quota" in text or "insufficient" in text or "arrearage" in text:
        return ErrorCode.QUOTA_EXCEEDED
    if "throttl" in text or "rate limit" in text or "too many requests" in text:
        return ErrorCode.RATE_LIMITED
    if "invalid api key" in text or "unauthorized" in text or "apikey" in text:
        return ErrorCode.AUTH_FAILED
    if "timeout" in text or "timed out" in text:
        return ErrorCode.TIMEOUT
    return ErrorCode.PROVIDER_ERROR


def split_base_url(base_url: str) -> tuple[str, str]:
    """把 ``https://host/compatible-mode/v1`` 拆成 ``(root, compat_prefix)``。"""
    raw = (base_url or "").strip().rstrip("/")
    for suffix in ("/compatible-mode/v1", "/compatible-mode"):
        if raw.endswith(suffix):
            return raw[: -len(suffix)], raw
    return raw, raw + "/compatible-mode/v1"


class DashScopeClient:
    """极简同步 HTTP 客户端（在线程里跑，绝不阻塞事件循环）。"""

    def __init__(self, base_url: str, api_key: str, *, timeout: float = DEFAULT_TIMEOUT) -> None:
        self.root, self.compat_base = split_base_url(base_url)
        self._api_key = (api_key or "").strip()
        self.timeout = timeout

    # -- 状态 -------------------------------------------------------------
    @property
    def configured(self) -> bool:
        return bool(self.root and self._api_key)

    def __repr__(self) -> str:
        # 日志/异常里可能出现 repr：这里绝不带出 Key
        return (
            f"DashScopeClient(root={self.root!r}, "
            f"has_key={self.has_key()}, timeout={self.timeout})"
        )

    def has_key(self) -> bool:
        """只暴露"有没有"，绝不返回 Key 本身。"""
        return bool(self._api_key)

    @property
    def realtime_url(self) -> str:
        host = urllib.parse.urlsplit(self.root).netloc
        scheme = "wss" if urllib.parse.urlsplit(self.root).scheme == "https" else "ws"
        return f"{scheme}://{host}/api-ws/v1/realtime"

    def auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}"}

    # -- 请求 -------------------------------------------------------------
    def _request(
        self,
        method: str,
        url: str,
        *,
        body: dict[str, Any] | None = None,
        async_mode: bool = False,
        timeout: float | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> ProviderResponse:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"User-Agent": USER_AGENT, **self.auth_headers()}
        if data is not None:
            headers["Content-Type"] = "application/json"
        if async_mode:
            headers["X-DashScope-Async"] = "enable"
        headers.update(extra_headers or {})
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=timeout or self.timeout) as resp:
                raw = resp.read(4 * 1024 * 1024).decode("utf-8", "replace")
                payload = _safe_json(raw)
                return ProviderResponse(
                    status=resp.status,
                    payload=payload,
                    request_id=str(payload.get("request_id") or ""),
                )
        except urllib.error.HTTPError as exc:
            raw = exc.read(2000).decode("utf-8", "replace")
            payload = _safe_json(raw)
            return ProviderResponse(
                status=exc.code,
                payload=payload,
                request_id=str(payload.get("request_id") or ""),
            )
        except urllib.error.URLError as exc:
            return ProviderResponse(
                status=-1,
                payload={"error": {"message": f"network: {getattr(exc, 'reason', exc)}"}},
            )
        except TimeoutError:
            return ProviderResponse(status=-1, payload={"error": {"message": "timeout"}})

    def post_service(self, path: str, body: dict[str, Any], *, async_mode: bool = False,
                     timeout: float | None = None) -> ProviderResponse:
        return self._request(
            "POST", f"{self.root}/api/v1/{path.lstrip('/')}", body=body,
            async_mode=async_mode, timeout=timeout,
        )

    def get_task(self, task_id: str, *, timeout: float | None = None) -> ProviderResponse:
        return self._request("GET", f"{self.root}/api/v1/tasks/{task_id}", timeout=timeout)

    def cancel_task(self, task_id: str, *, timeout: float | None = None) -> ProviderResponse:
        """取消一个异步任务（百炼：POST /api/v1/tasks/{id}/cancel）。

        只有异步任务（视频等）能真正取消；同步生成的图片/语音请求无法撤销。
        """
        return self._request(
            "POST", f"{self.root}/api/v1/tasks/{task_id}/cancel", timeout=timeout
        )

    def list_models(self) -> ProviderResponse:
        return self._request("GET", f"{self.compat_base}/models", timeout=15)

    # -- 文件下载 ---------------------------------------------------------
    def download(self, url: str, dest: Path, *, timeout: float = 180.0) -> tuple[bool, str]:
        """把生成结果下载到本地（签名地址只在这里出现，绝不回传给前端）。"""
        if not url.lower().startswith(("http://", "https://")):
            return False, "结果地址不是 http(s)"
        try:
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            dest.parent.mkdir(parents=True, exist_ok=True)
            written = 0
            with urllib.request.urlopen(request, timeout=timeout) as resp, open(dest, "wb") as fh:
                while True:
                    chunk = resp.read(64 * 1024)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > MAX_DOWNLOAD_BYTES:
                        fh.close()
                        dest.unlink(missing_ok=True)
                        return False, "结果文件超过大小上限"
                    fh.write(chunk)
            if written == 0:
                dest.unlink(missing_ok=True)
                return False, "下载到的文件为空"
            return True, ""
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            dest.unlink(missing_ok=True)
            return False, f"下载失败：{type(exc).__name__}"


def _safe_json(raw: str) -> dict[str, Any]:
    try:
        data = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return {"raw": raw[:800]}
    return data if isinstance(data, dict) else {"data": data}


async def run_blocking(func, *args, **kwargs):
    """在事件循环里安全地跑同步 HTTP 调用。"""
    return await asyncio.to_thread(func, *args, **kwargs)
