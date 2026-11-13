
"""
Remote Control 服务器：通过 WebSocket 桥接 Agent 事件和 Web UI。

使用 websockets 库提供 HTTP（静态 HTML）+ WebSocket 服务，
让用户在浏览器中与 AI 外脑 Agent 交互。
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import socket
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlsplit

import websockets
from websockets.asyncio.server import Server as WSServer, ServerConnection
from websockets.http11 import Request, Response

from aieb.agent import (
    Agent,
    CompactNotification,
    ErrorEvent,
    HookEvent,
    LoopComplete,
    PermissionRequest,
    PermissionResponse,
    RetryEvent,
    StreamText,
    ThinkingText,
    ToolResultEvent,
    ToolUseEvent,
    TurnComplete,
    UsageEvent,
)
from aieb import __version__
from aieb import artifacts
from aieb import env_compat
from aieb import session_purge
from aieb.client import create_client, resolve_context_window
from aieb.commands import CommandContext, CommandRegistry, CommandType
from aieb.commands.handlers import register_all_commands
from aieb.commands.parser import parse_command
from aieb.config import MCPServerConfig, ProviderConfig
from aieb.conversation import ConversationManager, DocumentBlock, ImageBlock, Message
from aieb.hooks import HookEngine
from aieb.mcp import MCPManager
from aieb.memory import MemoryManager, find_relevant_memories, load_instructions, render_reminder
from aieb.memory import provenance
from aieb.memory.session import (
    TITLE_MAX_LENGTH,
    Session,
    SessionManager,
    is_human_message,
)
from aieb.permissions import (
    DangerousCommandDetector,
    PathSandbox,
    PermissionChecker,
    PermissionMode,
    RuleEngine,
)
from aieb.brain import prompts as brain_prompts
from aieb.brain.ops import BrainOps, BrainOpsError
from aieb.brain.service import BrainService
from aieb.brain.tools import register_brain_tools
from aieb.brain.ui import render_index
from aieb.skills.loader import SkillLoader
from aieb.tools import ToolRegistry, create_default_registry, reset_per_turn_budgets
from aieb.tools.load_skill import LoadSkill

log = logging.getLogger(__name__)

# 浏览器 WebSocket API 不支持自定义请求头，因此 token 通过 subprotocol 传递：
#   new WebSocket(url, ['aieb.v1', 'aieb-token.<token>'])
# 之所以不用 URL query，是因为 query 会进入浏览器历史与服务端访问日志。
TOKEN_SUBPROTOCOL_PREFIX = "aieb-token."
WS_SUBPROTOCOL = "aieb.v1"

# 历史品牌子协议：旧页面/旧演示脚本仍可握手成功（只做接收兼容，不再对外宣告）
LEGACY_TOKEN_SUBPROTOCOL_PREFIXES = ("wendream-token.",)
LEGACY_WS_SUBPROTOCOLS = ("wendream.v1",)
ACCEPTED_WS_SUBPROTOCOLS = (WS_SUBPROTOCOL,) + LEGACY_WS_SUBPROTOCOLS

# 浏览器端无人确认权限请求时，等待多久后按拒绝处理（ASK ≠ ALLOW）
PERMISSION_TIMEOUT_SECONDS = 300.0


def is_loopback_host(host: str) -> bool:
    """host 是否只能被本机访问。"""
    h = (host or "").strip().lower().strip("[]")
    if h in {"localhost", "127.0.0.1", "::1"}:
        return True
    return h.startswith("127.")


@dataclass
class RemoteSettings:
    host: str = "127.0.0.1"
    port: int = 18888
    auth_token: str | None = None
    allowed_origins: list[str] = field(default_factory=list)
    allow_unauthenticated: bool = False


class RemoteAuthError(RuntimeError):
    """Remote 启动配置不安全（非 loopback 且没有认证）。"""


def resolve_remote_settings(
    config: Any = None,
    host: str | None = None,
    port: int | None = None,
    allow_unauthenticated: bool = False,
) -> RemoteSettings:
    """合并 CLI / 配置文件 / 环境变量，得到最终的 Remote 监听设置。

    token 来源优先级：``remote.auth_token`` → 环境变量 ``AIEB_REMOTE_TOKEN``
    （历史别名 ``WENDREAM_REMOTE_TOKEN`` 仍可用）。
    token 本身永远不会被打印或写入日志。
    """
    remote_cfg = getattr(config, "remote", None)
    final_host = host or getattr(remote_cfg, "host", None) or "127.0.0.1"
    final_port = port or getattr(remote_cfg, "port", None) or 18888
    token = getattr(remote_cfg, "auth_token", None) or None
    if not token:
        token = env_compat.get("REMOTE_TOKEN") or None
    origins = list(getattr(remote_cfg, "allowed_origins", None) or [])
    return RemoteSettings(
        host=final_host,
        port=int(final_port),
        auth_token=token,
        allowed_origins=origins,
        allow_unauthenticated=allow_unauthenticated,
    )


def provider_supports_vision(provider: ProviderConfig) -> bool:
    """返回 provider 是否应被前端视为支持图像输入。

    目前 Anthropic 与 OpenAI 系列（包括兼容协议）都可接收多模态内容，
    但旧实现只把 Anthropic 视为支持识图，导致 Qwen/DashScope 这类
    openai-compat 模型在网页端被提前拦截。对部分更老的文本模型来说它
    可能不是严格的视觉模型，但在本项目里，用户明确选中了图像输入时，
    这种“默认允许兼容协议”的行为比直接吞掉图片更符合用户预期。
    """
    if provider.protocol in {"anthropic", "openai", "openai-compat"}:
        return True
    model = provider.model.lower()
    return any(keyword in model for keyword in ("vision", "vl", "vqa", "multimodal", "qwen-vl", "llava", "gpt-4o", "gpt-4.1"))


class RemoteServer:
    """Remote Control 核心：桥接 Agent 事件和 WebSocket 客户端。"""

    def __init__(
        self,
        providers: list[ProviderConfig],
        mcp_servers: list[MCPServerConfig] | None = None,
        hook_engine: HookEngine | None = None,
        addr: str = "127.0.0.1",
        port: int = 18888,
        auth_token: str | None = None,
        allowed_origins: list[str] | None = None,
        allow_unauthenticated_remote: bool = False,
        web_search_config: Any = None,
    ) -> None:
        self.providers = providers
        self._mcp_server_configs = mcp_servers or []
        self.hook_engine = hook_engine
        self.addr = addr
        self.port = port
        # token 只保存在内存，不打印、不写日志
        self.auth_token = auth_token or None
        self.allowed_origins = list(allowed_origins or [])
        self.allow_unauthenticated_remote = bool(allow_unauthenticated_remote)
        self.web_search_config = web_search_config

        # 供 /healthz 计算 uptime（容器 healthcheck 与 smoke test 使用）
        self._started_at = time.monotonic()

        # WebSocket 连接池（支持多客户端广播）
        self._connections: set[ServerConnection] = set()

        # Agent 相关状态
        self.agent: Agent | None = None
        self.conversation: ConversationManager | None = None
        self.registry: ToolRegistry | None = None
        self.session_id: str = ""
        self._streaming = False
        self._cancel_event: asyncio.Event | None = None
        # 当前生效的 provider（网页端可切换，默认取列表第一个）
        self._active_provider: ProviderConfig = providers[0]

        # 对话分支（版本）现在由 Session 的对话树持久化维护：每条消息有稳定
        # message_id，编辑任一历史消息会生成兄弟分支，刷新/重启后仍可切回。
        # _branch_busy 是「编辑 / 重新生成 / 切换分支」的互斥标志：它们都会重写
        # conversation.history，并发进来会互相踩踏、把会话数据写坏。
        self._branch_busy: bool = False

        # 权限请求的 pending 队列：id -> Future
        self._pending_perms: dict[str, asyncio.Future[PermissionResponse]] = {}

        # 命令注册表
        self.command_registry = CommandRegistry()
        register_all_commands(self.command_registry)

        # MCP 相关
        self.mcp_manager: MCPManager | None = None
        self._mcp_instructions: str = ""

        # Skill 加载器
        self.skill_loader: SkillLoader | None = None

        # Memory / Session
        self.memory_manager: MemoryManager | None = None
        self.session_manager: SessionManager | None = None
        self.session: Session | None = None
        # 已落盘到当前 session 文件的消息对象 id() 集合，用于增量持久化。
        # 必须跟随 self.session 的生命周期重置（新建/切换会话时清空或
        # 预置为已加载的消息），否则每一轮新消息都会把此前的历史重新写入
        # 磁盘一遍，导致同一段对话在 session 文件里成倍重复。
        self._persisted_ids: set[int] = set()

        # ── 用户产物（Artifact）──
        # 每个会话一个独立存储；进程内随机密钥用于签名下载 URL，
        # 前端只拿得到 art_id + 签名，拿不到本地路径。
        self._artifact_secret: bytes = secrets.token_bytes(32)
        self._stores: dict[str, artifacts.ArtifactStore] = {}
        # 产物存储根目录（默认 .aieb/artifacts；测试可重定向）
        self.artifact_root: Path | None = None
        # 当前是第几轮人类对话（1 起），用于把产物挂到对应回答下。
        self._turn_no: int = 0

        # ── 多模态能力（图片/视频/语音…）──
        # 每个会话一个 CapabilityHost：任务记录落盘、后台执行、断线可恢复。
        # 懒创建：只有真的用到能力栏才去解析百炼客户端，纯对话不产生额外开销。
        self._cap_hosts: dict[str, Any] = {}

        # ── AI 外脑业务（知识库 / 会议 / 待办 / 邮件 / PPT）──
        self.brain_service: BrainService | None = None
        self.brain_ops: BrainOps | None = None

    # ------------------------------------------------------------------
    # 启动入口
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """启动 HTTP + WebSocket 服务器。"""
        # 启动前先校验绑定安全性：非 loopback 且无 token 直接拒绝启动。
        self._validate_binding()

        # 初始化 Agent
        self._init_agent()

        # 初始化 MCP（如果有配置）
        await self._init_mcp()

        print(f"\n  AI 外脑 Demo: http://{self._display_host()}:{self.port}\n")

        # websockets 的 serve 支持 process_request 回调来处理普通 HTTP
        async with websockets.serve(
            self._ws_handler,
            self.addr,
            self.port,
            process_request=self._process_http_request,
            # 必须回显子协议：浏览器在请求里带了 Sec-WebSocket-Protocol，
            # 若服务端不选，浏览器按 RFC 6455 判定握手失败并关闭连接
            # （表现为前端每 3 秒重连、界面一直显示"重连中…"）。
            subprotocols=list(ACCEPTED_WS_SUBPROTOCOLS),
            max_size=32 * 1024 * 1024,  # 32MB 消息上限（对齐 Anthropic 单次请求量级，容纳 base64 图片）
        ):
            # 服务器启动后永久阻塞
            await asyncio.Future()

    # ------------------------------------------------------------------
    # 绑定 / 认证 / Origin 校验
    # ------------------------------------------------------------------

    def _validate_binding(self) -> None:
        """非 loopback 绑定必须带 token，否则拒绝启动。"""
        if is_loopback_host(self.addr):
            return
        if self.auth_token:
            print(
                "WARNING: Remote server is listening on a non-loopback interface "
                f"({self.addr}). Anyone who can reach this port may attempt to connect; "
                "access is gated by the configured token.",
                file=__import__("sys").stderr,
            )
            return
        if self.allow_unauthenticated_remote:
            print(
                "WARNING: Remote server is listening on a non-loopback interface "
                f"({self.addr}) WITHOUT authentication. This is dangerous; "
                "only use it on a trusted network.",
                file=__import__("sys").stderr,
            )
            return
        raise RemoteAuthError(
            f"Refusing to bind remote server to non-loopback address {self.addr!r} "
            "without authentication. Set AIEB_REMOTE_TOKEN (or remote.auth_token), "
            "or pass --allow-unauthenticated-remote if you really want an open endpoint."
        )

    def _display_host(self) -> str:
        return "127.0.0.1" if is_loopback_host(self.addr) else self.addr

    def _origin_allowed(self, origin: str) -> bool:
        allowed = {
            f"http://127.0.0.1:{self.port}",
            f"http://localhost:{self.port}",
            f"http://[::1]:{self.port}",
        }
        allowed.update(self.allowed_origins)
        normalized = {item.rstrip("/").lower() for item in allowed}
        return origin.rstrip("/").lower() in normalized

    def _token_from_request(self, request: Request) -> str | None:
        header = request.headers.get("Authorization")
        if header and header.lower().startswith("bearer "):
            return header[7:].strip()
        proto = request.headers.get("Sec-WebSocket-Protocol") or ""
        prefixes = (TOKEN_SUBPROTOCOL_PREFIX,) + LEGACY_TOKEN_SUBPROTOCOL_PREFIXES
        for item in proto.split(","):
            item = item.strip()
            for prefix in prefixes:
                if item.startswith(prefix):
                    return item[len(prefix):]
        return None

    def authorize_request(self, request: Request) -> tuple[bool, str]:
        """返回 (是否放行, 拒绝原因)。供握手阶段调用，也便于测试。"""
        origin = request.headers.get("Origin")
        if origin and not self._origin_allowed(origin):
            return False, "origin"
        if self.auth_token:
            supplied = self._token_from_request(request) or ""
            if not hmac.compare_digest(supplied, self.auth_token):
                return False, "token"
        return True, ""

    # ------------------------------------------------------------------
    # HTTP 请求处理（为 / 路径提供前端 HTML）
    # ------------------------------------------------------------------

    def _qdrant_probe(self) -> dict[str, Any]:
        """轻量探测 Qdrant 是否可达（只建 TCP 连接，不查业务数据、不需要 qdrant-client）。"""
        url = os.environ.get("AIEB_QDRANT_URL", "http://127.0.0.1:6333")
        parts = urlsplit(url)
        host = parts.hostname or "127.0.0.1"
        port = parts.port or (443 if parts.scheme == "https" else 6333)
        started = time.perf_counter()
        try:
            with socket.create_connection((host, port), timeout=2.0):
                return {
                    "ok": True,
                    "url": url,
                    "latency_ms": round((time.perf_counter() - started) * 1000, 1),
                }
        except OSError as exc:
            return {"ok": False, "url": url, "error": type(exc).__name__}

    def _health_response(self, *, deep: bool) -> Response:
        payload: dict[str, Any] = {
            "ok": True,
            "app": "aieb",
            "version": __version__,
            "uptime_s": round(time.monotonic() - self._started_at, 1),
            "port": self.port,
        }
        status = 200
        if deep:
            probe = self._qdrant_probe()
            payload["qdrant"] = probe
            if not probe["ok"]:
                # 就绪探针：向量库不可用时返回 503，便于编排系统判断
                status = 503
                payload["ok"] = False
        return Response(
            status,
            "OK" if status == 200 else "Service Unavailable",
            websockets.Headers(
                {
                    "Content-Type": "application/json; charset=utf-8",
                    "Cache-Control": "no-store, must-revalidate",
                }
            ),
            json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        )

    def _process_http_request(
        self, connection: ServerConnection, request: Request
    ) -> Response | None:
        """拦截 HTTP 请求，对 / 路径返回 HTML 页面。
        返回 None 表示继续走 WebSocket 升级流程。
        """
        # 容器化部署用的健康检查：/healthz = 存活，/readyz = 就绪（含 Qdrant 可达性）。
        # 这两个路径不返回任何业务数据，也不泄露密钥/路径。
        plain_path = request.path.split("?")[0]
        if plain_path in {"/healthz", "/readyz"}:
            return self._health_response(deep=plain_path == "/readyz")

        if request.path == "/":
            # 首页是「AI 外脑 Demo」的产品化工作台（左侧导航 + 工作台/对话/
            # 知识库/会议中心/待办/记忆），不再是通用 Agent 聊天页。
            page = render_index(__version__)
            return Response(
                200,
                "OK",
                websockets.Headers(
                    {
                        "Content-Type": "text/html; charset=utf-8",
                        # 前端代码随版本更新，禁止缓存，否则浏览器会一直跑旧 JS，
                        # 表现为"改了没效果"，需要手动强刷才能看到。
                        "Cache-Control": "no-store, must-revalidate",
                        "Pragma": "no-cache",
                    }
                ),
                page.encode("utf-8"),
            )
        if request.path != "/ws":
            if request.path.split("?")[0] == "/file":
                return self._serve_file(request)
            if request.path.split("?")[0] == "/favicon.ico":
                # 页面已内联 SVG 图标；仍请求 .ico 的浏览器给个空 204，
                # 避免控制台出现无意义的 404
                return Response(204, "No Content", websockets.Headers(), b"")
            return Response(404, "Not Found", websockets.Headers(), b"404 Not Found")
        # /ws 路径 → 先做 Origin / token 校验，通过后才继续 WebSocket 升级
        ok, reason = self.authorize_request(request)
        if not ok:
            if reason == "token":
                return Response(
                    401,
                    "Unauthorized",
                    websockets.Headers({"Content-Type": "text/plain; charset=utf-8"}),
                    b"Unauthorized",
                )
            return Response(
                403,
                "Forbidden",
                websockets.Headers({"Content-Type": "text/plain; charset=utf-8"}),
                b"Forbidden origin",
            )
        return None

    # ------------------------------------------------------------------
    # 用户产物（Artifact）：独立存储 + 签名 URL 下载
    # ------------------------------------------------------------------

    _ARTIFACT_SIG_TTL = 3600  # 签名有效期（秒）

    def _workspace_root(self) -> Path:
        """Agent 的工作目录（产物采集的基准）。"""
        try:
            work_dir = getattr(self.agent, "work_dir", None) if self.agent else None
            return Path(work_dir).resolve() if work_dir else Path(os.getcwd()).resolve()
        except OSError:
            return Path(os.getcwd()).resolve()

    def _artifact_store(self) -> artifacts.ArtifactStore:
        """当前会话的产物存储（按 session_id 缓存）。"""
        sid = self.session_id or "default"
        store = self._stores.get(sid)
        if store is None:
            store = artifacts.ArtifactStore(
                sid, root=self.artifact_root, workspace=self._workspace_root()
            )
            self._stores[sid] = store
        return store

    def _artifact_signature(self, artifact_id: str, exp: int) -> str:
        message = f"{self.session_id}:{artifact_id}:{exp}".encode("utf-8")
        return hmac.new(self._artifact_secret, message, hashlib.sha256).hexdigest()[:32]

    def artifact_url(self, artifact_id: str, inline: bool = False) -> str:
        exp = int(time.time()) + self._ARTIFACT_SIG_TTL
        sig = self._artifact_signature(artifact_id, exp)
        url = f"/file?id={quote(artifact_id)}&exp={exp}&sig={sig}"
        return url + "&inline=1" if inline else url

    def _artifact_payload(self, items: list[artifacts.Artifact]) -> list[dict[str, Any]]:
        payload = self._artifact_store().payload(items)
        for entry in payload:
            entry["url"] = self.artifact_url(entry["id"], inline=False)
            if entry.get("preview"):
                entry["preview_url"] = self.artifact_url(entry["id"], inline=True)
        return payload

    def _publish_local_file(self, path: Path, name: str = "") -> dict[str, Any]:
        """把服务端生成的交付物（如 PPTX）直接收进产物区并返回前端卡片载荷。

        与「本轮新建文件扫描」不同，这里是显式交付：用户点了「生成 PPT」，
        文件就应该出现在产物区，不依赖文件名是否被回答正文提到。
        """
        try:
            item = self._artifact_store().add_file(
                Path(path), turn=max(self._turn_no, 1), name=name or Path(path).name
            )
        except Exception:  # noqa: BLE001 - 产物入库失败不应影响业务返回
            log.exception("publish local file failed")
            return {}
        if item is None:
            return {}
        payload = self._artifact_payload([item])
        return payload[0] if payload else {}

    async def _handle_brain_request(
        self, websocket: ServerConnection, data: dict[str, Any]
    ) -> None:
        """处理前端业务请求（知识库/会议/待办/邮件/PPT）。

        错误统一转成用户可读的中文提示，不向前端返回调用栈或本地路径。
        """
        req_id = str(data.get("req_id") or "")
        op = str(data.get("op") or "")
        if self.brain_ops is None:
            await self._send(websocket, {
                "type": "brain_response",
                "data": {"req_id": req_id, "ok": False, "error": "业务模块未初始化"},
            })
            return
        try:
            result = await self.brain_ops.dispatch(op, data)
            payload = {k: v for k, v in result.items() if not k.startswith("_")}
            await self._send(websocket, {
                "type": "brain_response",
                "data": {"req_id": req_id, "ok": True, "data": payload},
            })
        except BrainOpsError as exc:
            await self._send(websocket, {
                "type": "brain_response",
                "data": {"req_id": req_id, "ok": False, "error": str(exc)},
            })
        except Exception as exc:  # noqa: BLE001 - 任何异常都要变成人话
            log.exception("brain request failed: %s", op)
            from aieb.brain.tools_guard import describe_error

            await self._send(websocket, {
                "type": "brain_response",
                "data": {
                    "req_id": req_id,
                    "ok": False,
                    "error": describe_error(exc, "操作失败"),
                },
            })

    # ------------------------------------------------------------------
    # 多模态能力：把 MediaAPI 接到当前会话上
    # ------------------------------------------------------------------

    def _capability_state_path(self, session_id: str) -> Path:
        root = Path(self.work_dir or Path(os.getcwd())) / ".aieb" / "capability_tasks"
        return root / f"{session_id or 'default'}.json"

    def _capability_media_dir(self) -> Path:
        return Path(self.work_dir or Path(os.getcwd())) / ".aieb" / "media"

    def _capability_artifact_payload(
        self, items: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """按 artifact_id 取回带签名的下载地址（只给 ID，从不给本地路径）。"""
        store = self._artifact_store()
        resolved: list[artifacts.Artifact] = []
        for item in items or []:
            artifact_id = str((item or {}).get("id") or "")
            if not artifact_id:
                continue
            found = store.get(artifact_id)
            if found is not None:
                resolved.append(found)
        return self._artifact_payload(resolved)

    def _capability_host(self):
        """当前会话的能力宿主（懒创建，按 session_id 缓存）。"""
        from aieb.models.host import CapabilityHost
        from aieb.models.services import provider_supports_media

        sid = self.session_id or "default"
        host = self._cap_hosts.get(sid)
        if host is None:
            host = CapabilityHost(
                providers=self.providers,
                artifact_store=self._artifact_store(),
                broadcast=self._broadcast,
                state_path=self._capability_state_path(sid),
                work_dir=self._capability_media_dir(),
                artifact_payload=self._capability_artifact_payload,
                # 每次现取：用户在右上角换模型后，能力栏要跟着变
                provider_probe=lambda: (
                    provider_supports_media(self._active_provider),
                    getattr(self._active_provider, "display_name", "")
                    or self._active_provider.name,
                ),
            )
            self._cap_hosts[sid] = host
        return host

    async def _refresh_capabilities(self) -> None:
        """下发能力清单，并把当前会话的历史/未完成任务补发一遍。

        切会话时必须先停掉别的会话在跑的轮询：任务记录已经落盘，
        回到那个会话时 restore() 会接着查云端任务号，不会重新提交。
        """
        try:
            sid = self.session_id or "default"
            for key, other in list(self._cap_hosts.items()):
                if key != sid:
                    other.cleanup()
            host = self._capability_host()
            await self._broadcast({"type": "capabilities", "data": host.describe()})
            await host.restore()
        except Exception:  # noqa: BLE001 - 能力栏失败绝不影响主对话
            log.exception("capability refresh failed")
            await self._broadcast({
                "type": "capabilities",
                "data": {"capabilities": [], "error": "init_failed"},
            })

    def _serve_file(self, request: Request) -> Response:
        """``GET /file?id=<art_id>&exp=<ts>&sig=<hmac>[&inline=1]``。

        只接受服务端签发的产物 ID + 签名，前端拿不到任何本地路径；
        跨会话、过期、签名不符、路径被替换成符号链接都会失败。
        """
        parts = urlsplit(request.path)
        query = parse_qs(parts.query)
        headers = websockets.Headers({
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            # 即便被当作 HTML 打开也禁止脚本执行（纵深防御）
            "Content-Security-Policy": "default-src 'none'; sandbox",
        })

        artifact_id = (query.get("id") or [""])[0]
        sig = (query.get("sig") or [""])[0]
        try:
            exp = int((query.get("exp") or ["0"])[0])
        except ValueError:
            return Response(400, "Bad Request", headers, b"invalid exp")

        if not artifact_id or not sig:
            return Response(400, "Bad Request", headers, b"missing id")
        if exp < int(time.time()):
            return Response(403, "Forbidden", headers, b"expired")
        if not hmac.compare_digest(sig, self._artifact_signature(artifact_id, exp)):
            return Response(403, "Forbidden", headers, b"invalid signature")

        store = self._artifact_store()
        item = store.get(artifact_id)
        if item is None:
            return Response(404, "Not Found", headers, b"artifact not found")

        path = store.path_of(item)
        if path is None:
            return Response(404, "Not Found", headers, b"artifact unavailable")
        try:
            if path.stat().st_size > artifacts.MAX_ARTIFACT_BYTES:
                return Response(413, "Payload Too Large", headers, b"file too large")
            data = path.read_bytes()
        except OSError:
            return Response(500, "Internal Server Error", headers, b"read error")

        inline = (query.get("inline") or ["0"])[0] == "1"
        ctype, disposition = artifacts.content_type_for(path, inline)
        headers["Content-Type"] = ctype
        headers["Content-Disposition"] = f"{disposition}; filename*=UTF-8''{quote(item.name)}"
        headers["Content-Length"] = str(len(data))
        return Response(200, "OK", headers, data)

    # ------------------------------------------------------------------
    # 本轮产物识别：工具写入信息 + 最终回答文本 + 工作区快照（辅助）
    # ------------------------------------------------------------------

    _SCAN_SKIP_DIRS = {
        ".git", "node_modules", "__pycache__", ".aieb", ".venv", "venv",
        ".mypy_cache", ".pytest_cache", "dist", "build", ".idea", ".vscode",
    }

    def _snapshot_files(self, limit: int = 20000) -> dict[str, tuple[int, int]]:
        root = self._workspace_root()
        snap: dict[str, tuple[int, int]] = {}
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in self._SCAN_SKIP_DIRS]
            for filename in filenames:
                path = Path(dirpath) / filename
                try:
                    stat = path.stat()
                    rel = str(path.relative_to(root)).replace("\\", "/")
                except (OSError, ValueError):
                    continue
                snap[rel] = (stat.st_mtime_ns, stat.st_size)
                if len(snap) >= limit:
                    return snap
        return snap

    @staticmethod
    def _to_relative(raw: str, root: Path) -> str | None:
        """把工具参数里的路径规整成相对工作目录的 posix 路径。"""
        if not raw:
            return None
        try:
            candidate = Path(raw)
            if not candidate.is_absolute():
                candidate = root / candidate
            resolved = candidate.resolve()
            return str(resolved.relative_to(root)).replace("\\", "/")
        except (OSError, ValueError):
            return None

    def _publish_artifacts(
        self,
        *,
        snapshot_before: dict[str, tuple[int, int]],
        written_paths: set[str],
        final_text: str,
        turn: int,
    ) -> list[dict[str, Any]]:
        """挑出本轮真正要交付给用户的文件，复制进产物区并返回前端载荷。

        只收「本轮新建」的文件：修改已有工作文件（如改源码/配置）不会生成卡片。
        """
        root = self._workspace_root()
        snapshot_after = self._snapshot_files()

        candidates: list[str] = []
        for rel in snapshot_after:
            # 只收「本轮新建」的文件；已存在的属于修改工作文件，不算交付物
            if rel not in snapshot_before:
                candidates.append(rel)
        for raw in written_paths:
            rel = self._to_relative(raw, root)
            if rel and rel not in candidates and rel not in snapshot_before:
                candidates.append(rel)

        store = self._artifact_store()
        delivered: list[artifacts.Artifact] = []
        for rel in sorted(candidates)[:200]:
            name = Path(rel).name
            ext = artifacts.ext_of(rel)
            if not artifacts.should_deliver(rel, ext, artifacts.is_mentioned(rel, name, final_text)):
                continue
            item = store.add_file(root / rel, turn=turn, name=name)
            if item is None:
                continue
            delivered.append(item)
            if len(delivered) >= artifacts.MAX_ARTIFACTS_PER_TURN:
                break
        return self._artifact_payload(delivered)

    def _replay_artifacts(self) -> dict[int, list[dict[str, Any]]]:
        """按轮次分组的产物载荷，供历史会话重放时挂到对应回答下。"""
        store = self._artifact_store()
        grouped: dict[int, list[artifacts.Artifact]] = {}
        for item in store.payload_items():
            grouped.setdefault(item.turn, []).append(item)
        if not grouped:
            return {}
        return {turn: self._artifact_payload(items) for turn, items in grouped.items()}

    # ------------------------------------------------------------------
    # WebSocket 连接处理
    # ------------------------------------------------------------------

    async def _ws_handler(self, websocket: ServerConnection) -> None:
        """处理单个 WebSocket 连接的全生命周期。"""
        self._connections.add(websocket)
        try:
            # 连接建立时推送会话信息
            await self._broadcast({
                "type": "connected",
                "data": {
                    "session": self.session_id,
                    "cwd": os.getcwd(),
                    "visionEnabled": provider_supports_vision(self._active_provider),
                    "activeProvider": self._active_provider.name,
                },
            })

            # 推送命令列表
            await self._broadcast({
                "type": "commands",
                "data": self._build_command_list(),
            })

            # 推送可切换的 provider 列表
            await self._broadcast({
                "type": "providers",
                "data": {
                    "providers": [
                        {
                            "name": p.name,
                            "protocol": p.protocol,
                            "model": p.model,
                            "display_name": getattr(p, "display_name", "") or "",
                        }
                        for p in self.providers
                    ],
                    "active": self._active_provider.name,
                },
            })

            # 推送历史会话列表（侧边栏）
            await self._broadcast({
                "type": "session_list",
                "data": {"sessions": self._build_session_list()},
            })

            # 刷新页面（新连接）时把当前会话按「当前分支」重放一遍：
            # 否则用户会看到一个空对话，也无法在刷新后继续使用 ‹ 1/2 › 切回旧分支。
            # 只发给本连接，避免打断其它已打开的标签页。
            if self.conversation is not None and self.conversation.history:
                await self._send(websocket, {"type": "clear", "data": None})
                await self._replay_messages(
                    self.conversation.history,
                    send=lambda payload: self._send(websocket, payload),
                )

            # 能力栏：下发「哪些功能真的能用」+ 恢复本会话的历史/未完成任务。
            # 这一段不能阻断连接就绪：百炼客户端构建失败也要能正常聊天。
            await self._refresh_capabilities()

            # 消息循环
            async for raw in websocket:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                msg_type = msg.get("type", "")
                data = msg.get("data", {})

                if msg_type == "user_message":
                    content = data.get("content", "").strip()
                    image_blocks, document_blocks = self._parse_attachments(data)
                    capability_hint = str(data.get("capability") or "")
                    if content or image_blocks or document_blocks:
                        # 在后台任务中处理，不阻塞 WebSocket 读循环
                        asyncio.create_task(
                            self._handle_user_message(
                                content, image_blocks, document_blocks,
                                capability_hint=capability_hint,
                            )
                        )

                elif msg_type == "brain_request":
                    # 知识库 / 会议 / 待办 / 邮件 / PPT 的业务请求
                    asyncio.create_task(self._handle_brain_request(websocket, data or {}))

                elif msg_type == "capability_describe":
                    try:
                        await self._send(websocket, {
                            "type": "capabilities",
                            "data": self._capability_host().describe(),
                        })
                    except Exception:  # noqa: BLE001
                        log.exception("capability describe failed")

                elif msg_type == "capability_request":
                    await self._handle_capability_request(websocket, data or {})

                elif msg_type == "capability_resume":
                    await self._handle_capability_resume(websocket, data or {})

                elif msg_type == "capability_cancel":
                    await self._handle_capability_cancel(websocket, data or {})

                elif msg_type == "permission_response":
                    self._handle_permission_response(data)

                elif msg_type == "cancel":
                    if self._cancel_event is not None:
                        self._cancel_event.set()

                elif msg_type == "ping":
                    # 应用层保活
                    await self._broadcast({"type": "pong", "data": None})

                elif msg_type == "new_session":
                    asyncio.create_task(self._handle_new_session())

                elif msg_type == "replay_current":
                    # 页面内切走再切回「AI 对话」：DOM 已销毁但服务端会话还在，
                    # 只需把当前会话重放给这个连接（不新建会话、不调用模型）。
                    asyncio.create_task(self._handle_replay_current(websocket))

                elif msg_type == "load_session":
                    session_id = data.get("id", "")
                    if session_id:
                        asyncio.create_task(self._handle_load_session(session_id))

                elif msg_type == "delete_session":
                    session_id = data.get("id", "")
                    # 默认删除该会话产生的记忆（与 UI 里默认勾选一致）
                    purge_memories = data.get("purge_memories", True)
                    if session_id:
                        asyncio.create_task(
                            self._handle_delete_session(
                                session_id, purge_memories=bool(purge_memories)
                            )
                        )

                elif msg_type == "rename_session":
                    session_id = data.get("id", "")
                    title = data.get("title", "").strip()
                    if session_id and title:
                        asyncio.create_task(self._handle_rename_session(session_id, title))

                elif msg_type == "switch_provider":
                    provider_name = data.get("name", "")
                    if provider_name:
                        asyncio.create_task(self._handle_switch_provider(provider_name))

                elif msg_type == "regenerate":
                    # message_id 为空时退回"最后一轮"（兼容旧前端）
                    asyncio.create_task(
                        self._handle_regenerate(str(data.get("message_id") or ""))
                    )

                elif msg_type == "edit_message":
                    message_id = str(data.get("message_id") or "")
                    new_content = data.get("content", "").strip()
                    image_blocks, document_blocks = self._parse_attachments(data)
                    if message_id and (new_content or image_blocks or document_blocks):
                        asyncio.create_task(
                            self._handle_edit_message(
                                message_id, new_content, image_blocks, document_blocks
                            )
                        )

                elif msg_type == "switch_turn_version":
                    message_id = str(data.get("message_id") or "")
                    target_id = str(data.get("target_id") or "")
                    version_index = data.get("index")
                    if message_id:
                        asyncio.create_task(
                            self._handle_switch_turn_version(
                                message_id,
                                target_id=target_id,
                                index=version_index if isinstance(version_index, int) else None,
                            )
                        )

                elif msg_type == "memory_list":
                    asyncio.create_task(self._broadcast_memory_state())

                elif msg_type in ("memory_delete", "memory_clear", "memory_set_enabled"):
                    payload = dict(data or {})
                    if msg_type == "memory_delete":
                        payload["action"] = "delete"
                    elif msg_type == "memory_clear":
                        payload["action"] = "clear"
                    else:
                        payload["action"] = "set_enabled"
                    asyncio.create_task(self._handle_memory_action(payload))

        except websockets.ConnectionClosed:
            pass
        finally:
            self._connections.discard(websocket)

    # ------------------------------------------------------------------
    # Agent 初始化（复刻 TUI 的 _select_provider 流程）
    # ------------------------------------------------------------------

    def _init_agent(self) -> None:
        """初始化 Agent 及相关子系统。"""
        provider = self._active_provider
        work_dir = str(Path(os.getcwd()).resolve())
        home = Path.home()

        # 权限系统
        checker = PermissionChecker(
            detector=DangerousCommandDetector(),
            sandbox=PathSandbox(work_dir),
            rule_engine=RuleEngine(
                user_rules_path=home / ".aieb" / "permissions.yaml",
                project_rules_path=Path(work_dir) / ".aieb" / "permissions.yaml",
                local_rules_path=Path(work_dir) / ".aieb" / "permissions.local.yaml",
            ),
            mode=PermissionMode.DEFAULT,
        )

        # 加载自定义指令和记忆
        instructions = load_instructions(work_dir)
        self.memory_manager = MemoryManager(work_dir)
        self.session_manager = SessionManager(work_dir)
        self.work_dir = Path(work_dir).resolve()
        self.session = self.session_manager.create()
        self.session_id = self.session.session_id
        self._persisted_ids = set()

        # ── 外脑业务服务：知识库检索 / 会议分析 / 待办 / 邮件 / PPT ──
        self.brain_service = BrainService(
            work_dir=self.work_dir,
            provider=provider,
            memory_manager=self.memory_manager,
            publish=self._publish_local_file,
        )
        self.brain_ops = BrainOps(
            self.brain_service, provider_getter=lambda: self._active_provider
        )
        instructions = (instructions + "\n\n" + brain_prompts.BRAIN_INSTRUCTIONS).strip()

        # 创建 LLM 客户端
        client = create_client(provider)

        # 工具注册表（workspace 根必须与 Agent / PathSandbox 一致）
        self.registry = create_default_registry(
            work_dir=work_dir,
            web_search_config=self.web_search_config,
            llm_providers=self.providers,
        )

        # 外脑业务工具：让自然语言可以直接驱动知识库/会议/待办/PPT/邮件草稿
        if self.brain_service is not None:
            register_brain_tools(self.registry, self.brain_service, work_dir=work_dir)

        # Skill 加载
        self.skill_loader = SkillLoader(work_dir)
        self.skill_loader.load_all()
        load_skill_tool = LoadSkill()
        self.registry.register(load_skill_tool)

        # 创建 Agent
        self.agent = Agent(
            client=client,
            registry=self.registry,
            protocol=provider.protocol,
            work_dir=work_dir,
            permission_checker=checker,
            context_window=provider.get_context_window(),
            instructions_content=instructions,
            memory_manager=self.memory_manager,
            hook_engine=self.hook_engine,
        )
        self.agent.session_id = self.session_id
        # 把"知识库里现在有什么"注入环境上下文：
        # 工具存在 ≠ 模型知道该用；缺这个状态，模型会反问"要我查一下吗"。
        self.agent.set_env_context_extra(self.brain_service.knowledge_context())
        # 轻量级路由提示：命中"制度/数据类事实问题"时，提醒模型先检索再回答。
        from aieb.brain import routing as brain_routing

        self.agent.set_turn_hint_provider(
            lambda question: brain_routing.hint_for(
                question, has_documents=self.brain_service.has_documents()
            )
        )

        # 连接 Skill 到 Agent
        load_skill_tool.set_loader(self.skill_loader)
        load_skill_tool.set_agent(self.agent)

        catalog = self.skill_loader.get_catalog()
        if catalog:
            lines = ["You can use the following Skills:", ""]
            for name, desc in catalog:
                lines.append(f"- {name}: {desc}")
            lines.append("")
            lines.append("If the user's request matches a Skill, call LoadSkill to activate it.")
            self.agent.set_skill_catalog("\n".join(lines))

        # 初始化对话管理器
        self.conversation = ConversationManager()
        # 重启后接着上次的对话走：否则刷新页面只会看到一个空对话，
        # 分支虽然还在磁盘上，但用户看不到也切不回去。
        self._restore_last_session()

        log.info("Agent initialized: session=%s, model=%s", self.session_id, provider.model)

    def _restore_last_session(self) -> None:
        """启动时恢复最近活跃的会话（有内容的那种）。

        只影响「服务端进程重启」这一个场景：磁盘上的分支状态本来就还在，
        缺的只是「当前看哪一个会话 / 当前在哪个分支」。挑不出可用会话时
        保持构造函数里新建的空会话，行为与以前一致。
        """
        if self.session_manager is None or self.conversation is None:
            return
        try:
            metas = [
                m for m in self.session_manager.list()
                if (getattr(m, "message_count", 0) or 0) > 0
            ]
        except OSError:
            log.debug("读取会话列表失败，保持新会话")
            return
        metas.sort(key=lambda m: m.last_active, reverse=True)
        for meta in metas[:3]:
            try:
                result = self.session_manager.resume(meta.id)
            except Exception:  # noqa: BLE001
                log.exception("恢复上次会话失败: %s", meta.id)
                continue
            if result is None or not result.messages:
                continue
            try:
                if self.session is not None:
                    self.session.close()
            except Exception:  # noqa: BLE001
                pass
            self.session = result.session
            self.session_id = meta.id
            self.conversation.history = list(result.messages)
            self._persisted_ids = {id(m) for m in result.messages}
            self._turn_no = sum(1 for m in result.messages if is_human_message(m))
            log.info(
                "已恢复上次会话 %s（%d 条消息，当前分支）",
                self.session_id,
                len(result.messages),
            )
            return

    # ------------------------------------------------------------------
    # MCP 初始化
    # ------------------------------------------------------------------
    async def _init_mcp(self) -> None:
        """连接所有配置的 MCP 服务器，注册工具。"""
        if not self._mcp_server_configs or self.registry is None:
            return

        manager = MCPManager()
        manager.load_configs(self._mcp_server_configs)
        connect_result = await manager.register_all_tools(self.registry)
        self.mcp_manager = manager

        for err in connect_result.errors:
            log.warning("MCP error: %s", err)

        # 构建 MCP 指令（首次发送消息时注入 conversation）
        if connect_result.servers:
            parts = []
            for srv_info in connect_result.servers:
                section = f"## {srv_info.name}\n"
                if srv_info.instructions:
                    section += srv_info.instructions
                else:
                    tool_names = [
                        t.name for t in self.registry.list_tools()
                        if t.name.startswith(f"mcp__{srv_info.name}__")
                    ]
                    if tool_names:
                        section += "Available tools: " + ", ".join(tool_names)
                parts.append(section)
            self._mcp_instructions = (
                "# MCP Server Instructions\n\n"
                "The following MCP servers have provided instructions "
                "for how to use their tools and resources:\n\n"
                + "\n\n".join(parts)
            )

    # ------------------------------------------------------------------
    # 记忆精选召回（对齐 TUI 的 _prefetch_relevant_memories）
    # ------------------------------------------------------------------

    async def _prefetch_relevant_memories(self, query: str) -> str:
        """用一次独立的 side-query（8s 超时）挑出最多 5 条与当前消息最相关的
        记忆文件，返回渲染好的完整正文 system-reminder 文本；失败/超时返回 ""。

        用独立的 LLM client，使 selector 的 system prompt 不受主对话
        system prompt 影响。best-effort：任何失败都不应阻塞主对话。
        """
        if self.memory_manager is None or not self.providers:
            return ""

        provider = self._active_provider
        user_dir = self.memory_manager.user_mem_dir
        project_dir = self.memory_manager.project_mem_dir

        async def selector(system_prompt: str, user_message: str) -> str:
            from aieb.tools.base import StreamEnd, TextDelta

            side_client = create_client(provider)
            mini_conv = ConversationManager()
            mini_conv.history = [Message(role="user", content=user_message)]
            collected = ""
            async for event in side_client.stream(mini_conv, system=system_prompt):
                if isinstance(event, TextDelta):
                    collected += event.text
                elif isinstance(event, StreamEnd):
                    pass
            return collected

        try:
            results = await asyncio.wait_for(
                find_relevant_memories(
                    query=query,
                    user_mem_dir=user_dir,
                    project_mem_dir=project_dir,
                    recent_tools=None,
                    already_surfaced=None,
                    selector=selector,
                ),
                timeout=8.0,
            )
            return render_reminder(results)
        except (asyncio.TimeoutError, Exception):
            return ""

    # ------------------------------------------------------------------
    # 用户消息处理
    # ------------------------------------------------------------------

    #: 对话侧能力（不生成文件）的中文提示：用户选了功能，就要真的生效
    CAPABILITY_HINTS: dict[str, str] = {
        "writing": (
            "（用户当前选择了「帮我写作」功能：请直接产出可直接使用的成稿，"
            "不要反复反问，除非缺少关键事实。）"
        ),
        "web_search": "（用户当前选择了「联网搜索」功能：请优先联网检索，并在回答里标明信息来源。）",
        "web_fetch": "（用户当前选择了「网页读取」功能：如果消息里有链接，请先读取该网页正文再作答。）",
        "vision_understanding": "（用户当前选择了「看图理解」功能：请重点分析随消息提供的图片。）",
    }

    def _apply_capability_hint(self, capability_hint: str) -> str:
        """把功能栏选中的对话类能力变成一条 system-reminder（不改用户原文）。

        返回实际注入的提示文本（空串表示这个能力不需要提示），便于测试与排查。
        """
        hint = self.CAPABILITY_HINTS.get(capability_hint or "", "")
        if hint and self.conversation is not None:
            self.conversation.add_system_reminder(hint)
        return hint

    async def _handle_capability_request(
        self, websocket: ServerConnection, data: dict[str, Any]
    ) -> None:
        """服务端校验 + 后台执行；真正的结果走广播，ack 只告诉前端"收下了"。
        """
        try:
            ack = await self._capability_host().start(data)
        except Exception:  # noqa: BLE001 - 任何意外都要回一句人话
            log.exception("capability request failed")
            ack = {
                "ok": False,
                "error_code": "internal_error",
                "error_message": "生成任务没能启动，请稍后重试",
            }
        if not ack.get("ok"):
            ack["request_id"] = str(data.get("request_id") or "")
        await self._send(websocket, {"type": "capability_ack", "data": ack})

    async def _handle_capability_resume(
        self, websocket: ServerConnection, data: dict[str, Any]
    ) -> None:
        """「继续查询」：只对已有云端 task_id 的任务重新轮询，绝不重新提交。"""
        task_id = str(data.get("task_id") or "")
        host = self._capability_host()
        resumed = await host.resume(task_id)
        await self._send(websocket, {
            "type": "capability_ack",
            "data": {"ok": resumed, "task_id": task_id,
                     "error_message": "" if resumed else "这个任务已经结束或无法继续查询"},
        })

    async def _handle_capability_cancel(
        self, websocket: ServerConnection, data: dict[str, Any]
    ) -> None:
        """「停止」：停掉等待并如实告知后果（云端可能已生成/计费）。"""
        task_id = str(data.get("task_id") or "")
        try:
            stopped = await self._capability_host().cancel(task_id)
        except Exception:  # noqa: BLE001
            log.exception("capability cancel failed")
            stopped = False
        await self._send(websocket, {
            "type": "capability_ack",
            "data": {
                "ok": stopped,
                "task_id": task_id,
                "cancelled": True,
                "error_message": "" if stopped else "这个任务已经结束，无法停止",
            },
        })

    async def _handle_user_message(
        self,
        content: str,
        images: list[ImageBlock] | None = None,
        documents: list[DocumentBlock] | None = None,
        _same_turn: bool = False,
        capability_hint: str = "",
    ) -> None:
        """处理来自 Web UI 的用户消息或斜杠命令。

        _same_turn=True 表示这次调用来自「重新生成/编辑」——即在同一个提问位置
        上再来一版，而不是一条全新的消息。此时不清空该位置已经积累的版本历史。
        """
        if self._streaming:
            return

        # 斜杠命令
        if content.startswith("/"):
            await self._handle_slash_command(content)
            return

        # 普通消息 → 发给 Agent
        self._streaming = True
        assert self.conversation is not None
        assert self.agent is not None

        # 新一轮请求：重置联网工具的次数预算（搜索/读网页各有限额）
        reset_per_turn_budgets(self.registry)

        # 在主对话开始前先起一个后台任务，精选最相关的记忆（不阻塞主流程）
        prefetch_task = asyncio.create_task(self._prefetch_relevant_memories(content))

        self.conversation.add_user_message(content, images=images, documents=documents)
        user_msg = self.conversation.history[-1]
        if self.session is not None:
            self.session.append(user_msg)
            self._persisted_ids.add(id(user_msg))

        # 功能栏里选中的对话类能力（帮我写作 / 联网搜索 / 网页读取 / 看图理解）：
        # 不改变用户原文，只给模型一条附加提示，让"选中的功能"真正生效。
        self._apply_capability_hint(capability_hint)

        if not _same_turn:
            # 告诉前端这条消息的稳定 ID 与版本信息：它刚落盘，前端本地渲染时还
            # 不知道 ID，补一条事件过去，这条消息才能立刻被编辑、也能显示 ‹ 1/2 ›。
            await self._broadcast({
                "type": "user_message_saved",
                "data": {
                    "message_id": user_msg.message_id,
                    "version": self._version_info(user_msg),
                },
            })

        if _same_turn:
            # 编辑/重新生成时这条消息是服务端直接重发的，不是用户在输入框里敲的，
            # 前端没有机会提前本地渲染——这里补一条 replay_user 广播出去。
            # 带上 message_id 与版本信息，前端立刻就能显示 ‹ 1/2 › 导航。
            await self._broadcast({
                "type": "replay_user",
                "data": {
                    "content": user_msg.content,
                    "message_id": user_msg.message_id,
                    "version": self._version_info(user_msg),
                    "images": [
                        {"media_type": ib.media_type, "data": ib.data}
                        for ib in user_msg.images
                    ],
                    "documents": [
                        {"media_type": db.media_type, "data": db.data, "name": db.name}
                        for db in user_msg.documents
                    ],
                },
            })

        def flush_new_messages() -> None:
            """把 conversation.history 中尚未落盘的消息追加写入 session。

            用对象身份（而非下标游标）跟踪已持久化的消息，且该集合是
            RemoteServer 实例级别的状态（跟随 self.session 的生命周期），而
            不是每次调用重新创建：agent.run() 可能会在历史记录靠前的位置插入
            env/长期记忆注入的 system-reminder 消息，这会把原本在游标之前的
            用户消息「挤」到游标之后，导致基于下标切片的游标失效；而如果
            用一个每次调用都重新清空的局部集合来做身份追踪，则会在同一个
            会话的第二轮及之后每次把此前已经写过的历史重新写一遍，导致
            session 文件里的对话成倍重复。
            """
            if self.session is None:
                return
            for msg in self.conversation.history:
                if id(msg) not in self._persisted_ids:
                    self.session.append(msg)
                    self._persisted_ids.add(id(msg))

        # 首次注入 MCP 指令
        if self._mcp_instructions:
            self.conversation.add_system_reminder(self._mcp_instructions)
            self._mcp_instructions = ""

        # 把精选记忆的 prefetch 任务交给 agent：工具执行完毕后，agent 会在
        # 任务就绪时把结果作为 system-reminder 注入本轮对话（agent.py 中
        # memory_recall_task 消费逻辑），不阻塞主流程。
        self.agent.memory_recall_task = prefetch_task
        self.agent._memory_recall_consumed = False

        # 创建取消事件
        self._cancel_event = asyncio.Event()
        start_time = time.monotonic()
        stream_buf = ""
        answer_text = ""
        written_paths: set[str] = set()
        cancelled = False
        # 本轮编号（产物要能挂回对应的回答）
        self._turn_no += 1
        turn_no = self._turn_no
        # 记录本轮开始前的文件快照，结束后对比出新建的文件（作为辅助信号）
        try:
            snapshot_before = self._snapshot_files()
        except Exception:  # noqa: BLE001
            log.exception("artifact snapshot failed")
            snapshot_before = {}
        # 记忆目录快照：本轮新写的记忆要能归属到当前会话
        memory_dirs = self._memory_dirs()
        memory_before = {d: provenance.snapshot(d) for d in memory_dirs}

        try:
            async for event in self.agent.run(self.conversation):
                # 检查取消信号
                if self._cancel_event.is_set():
                    cancelled = True
                    break

                if isinstance(event, StreamText):
                    stream_buf += event.text
                    answer_text += event.text
                    await self._broadcast({
                        "type": "stream_text",
                        "data": {"text": event.text},
                    })

                elif isinstance(event, ThinkingText):
                    await self._broadcast({
                        "type": "thinking_text",
                        "data": {"text": event.text},
                    })

                elif isinstance(event, ToolUseEvent):
                    # 工具层信息：文件写入类工具直接告诉我们它写了哪个文件
                    if isinstance(event.arguments, dict):
                        for key in ("file_path", "path", "notebook_path"):
                            value = event.arguments.get(key)
                            if isinstance(value, str) and value:
                                written_paths.add(value)
                    await self._broadcast({
                        "type": "tool_use",
                        "data": {
                            "toolId": event.tool_id,
                            "toolName": event.tool_name,
                            "args": self._preview_args(event.arguments),
                        },
                    })

                elif isinstance(event, ToolResultEvent):
                    # 如果之前有累积的流式文本，先结束它
                    if stream_buf:
                        await self._broadcast({
                            "type": "stream_end",
                            "data": {"text": stream_buf},
                        })
                        stream_buf = ""
                    # 邮件草稿：工具输出里带了机器可读标记，这里解析成结构化事件，
                    # 前端据此在聊天里渲染真正的草稿卡片（而不是让用户去别的页面找）。
                    draft, clean_output = self._extract_email_draft(event)
                    await self._broadcast({
                        "type": "tool_result",
                        "data": {
                            "toolId": event.tool_id,
                            "toolName": event.tool_name,
                            "output": clean_output,
                            "isError": event.is_error,
                            "elapsed": event.elapsed,
                        },
                    })
                    if draft:
                        await self._broadcast({"type": "email_draft", "data": draft})

                elif isinstance(event, PermissionRequest):
                    # 生成唯一 ID，等待 Web 端回复；无人回复则超时后默认拒绝（ASK ≠ ALLOW）。
                    perm_id = f"perm_{time.time_ns()}"
                    self._pending_perms[perm_id] = event.future
                    asyncio.create_task(self._expire_permission(perm_id, event.future))
                    view = self._permission_view(event.tool_name, event.description)
                    await self._broadcast({
                        "type": "permission_request",
                        "data": {
                            "id": perm_id,
                            # 普通模式：人类可读的动作 + 对象 + 原因
                            "action": view["action"],
                            "target": view["target"],
                            "why": view["why"],
                            # 开发者模式：原始工具名与完整描述
                            "toolName": event.tool_name,
                            "description": event.description,
                        },
                    })

                elif isinstance(event, TurnComplete):
                    if stream_buf:
                        await self._broadcast({
                            "type": "stream_end",
                            "data": {"text": stream_buf},
                        })
                        stream_buf = ""
                    flush_new_messages()
                    await self._broadcast({
                        "type": "turn_complete",
                        "data": {"turn": event.turn},
                    })

                elif isinstance(event, LoopComplete):
                    if stream_buf:
                        await self._broadcast({
                            "type": "stream_end",
                            "data": {"text": stream_buf},
                        })
                        stream_buf = ""
                    flush_new_messages()
                    if self.session is not None:
                        self.session.meta.total_tokens = (
                            self.agent.total_input_tokens + self.agent.total_output_tokens
                        )
                    elapsed = time.monotonic() - start_time
                    await self._broadcast({
                        "type": "loop_complete",
                        "data": {
                            "totalTurns": event.total_turns,
                            "elapsed": elapsed,
                        },
                    })
                    await self._broadcast_session_list()

                elif isinstance(event, UsageEvent):
                    await self._broadcast({
                        "type": "usage",
                        "data": {
                            "inputTokens": event.input_tokens,
                            "outputTokens": event.output_tokens,
                        },
                    })

                elif isinstance(event, ErrorEvent):
                    await self._broadcast({
                        "type": "error",
                        "data": {"message": event.message},
                    })

                elif isinstance(event, CompactNotification):
                    await self._broadcast({
                        "type": "compact",
                        "data": {"message": event.message},
                    })

                elif isinstance(event, RetryEvent):
                    await self._broadcast({
                        "type": "retry",
                        "data": {
                            "reason": event.reason,
                            "waitMs": int(event.wait * 1000),
                        },
                    })

                elif isinstance(event, HookEvent):
                    status = "ok" if event.success else "error"
                    await self._broadcast({
                        "type": "system",
                        "data": {
                            "message": f"Hook [{event.hook_id}] {status}: {event.output}"
                        },
                    })

        except asyncio.CancelledError:
            await self._broadcast({
                "type": "error",
                "data": {"message": "Operation cancelled"},
            })
        except Exception as exc:
            log.exception("Agent run error")
            await self._broadcast({
                "type": "error",
                "data": {"message": str(exc)},
            })
        finally:
            self._streaming = False
            self._cancel_event = None

        # 用户点了停止：agent.run() 被 break 退出，不会产生 LoopComplete。
        # 必须补发一个终止事件，否则前端 streaming 状态永远为 true，
        # 输入框、上传、编辑重跑、切换会话全都会被锁死。
        if cancelled:
            if stream_buf:
                await self._broadcast({
                    "type": "stream_end",
                    "data": {"text": stream_buf},
                })
            await self._broadcast({
                "type": "loop_complete",
                "data": {
                    "totalTurns": 0,
                    "elapsed": time.monotonic() - start_time,
                    "cancelled": True,
                },
            })
            await self._broadcast_session_list()

        # 本轮产物：只有「新建 + 看起来是要交付的文件」才会变成卡片
        try:
            delivered = self._publish_artifacts(
                snapshot_before=snapshot_before,
                written_paths=written_paths,
                final_text=answer_text,
                turn=turn_no,
            )
        except Exception:  # noqa: BLE001
            log.exception("artifact publish failed")
            delivered = []
        if delivered:
            await self._broadcast({"type": "artifacts", "data": {"files": delivered}})

        # 记录本轮新增/修改的记忆来源（删对话时据此判断是否为该会话专属）
        for mem_dir in memory_dirs:
            try:
                provenance.record_changes(mem_dir, memory_before.get(mem_dir, {}), self.session_id)
            except OSError:
                log.debug("memory provenance record failed for %s", mem_dir)

    # ------------------------------------------------------------------
    # 斜杠命令处理
    # ------------------------------------------------------------------

    async def _handle_slash_command(self, input_text: str) -> None:
        """分发斜杠命令。"""
        name, args, is_command = parse_command(input_text)
        if not is_command or not name:
            return

        cmd = self.command_registry.find(name)
        if cmd is None:
            await self._broadcast({
                "type": "error",
                "data": {"message": f"Unknown command: /{name} — type /help to see available commands"},
            })
            await self._broadcast({"type": "command_done", "data": None})
            return

        # 需要参数但没给
        if not args and cmd.arg_prompt:
            await self._broadcast({
                "type": "system",
                "data": {"message": cmd.arg_prompt},
            })
            await self._broadcast({"type": "command_done", "data": None})
            return

        if cmd.type == CommandType.LOCAL:
            # 本地命令直接执行
            ctx = self._build_command_context(args)
            try:
                await cmd.handler(ctx)
            except Exception as exc:
                await self._broadcast({
                    "type": "error",
                    "data": {"message": f"Command error: {exc}"},
                })
            await self._broadcast({"type": "command_done", "data": None})

        elif cmd.type == CommandType.LOCAL_UI:
            # UI 命令需要特殊处理
            if name == "clear":
                self.conversation = ConversationManager()
                self._persisted_ids = set()
                if self.agent is not None:
                    self.agent.clear_active_skills()
                await self._broadcast({"type": "clear", "data": None})

            elif name == "compact":
                await self._handle_compact()
                return

            else:
                await self._broadcast({
                    "type": "system",
                    "data": {"message": f"/{name} is not fully supported in remote mode."},
                })

            await self._broadcast({"type": "command_done", "data": None})

        elif cmd.type == CommandType.PROMPT:
            # Prompt 类命令：handler 返回 prompt 文本，注入给 agent
            ctx = self._build_command_context(args)
            try:
                await cmd.handler(ctx)
            except Exception as exc:
                await self._broadcast({
                    "type": "error",
                    "data": {"message": f"Command error: {exc}"},
                })
                await self._broadcast({"type": "command_done", "data": None})

    def _build_command_context(self, args: str) -> CommandContext:
        """构建命令上下文。"""
        return CommandContext(
            args=args,
            agent=self.agent,
            conversation=self.conversation,
            session=self.session,
            session_manager=self.session_manager,
            memory_manager=self.memory_manager,
            ui=self,  # type: ignore[arg-type]
            config={
                "registry": self.command_registry,
            },
        )

    # ------------------------------------------------------------------
    # 会话管理（新建 / 加载 / 删除，供 Web 侧边栏使用）
    # ------------------------------------------------------------------

    def _build_session_list(self) -> list[dict[str, Any]]:
        """构建会话列表，推送给前端渲染历史侧边栏。

        “新建对话”会立刻创建一个空会话（0 条消息），如果全部列出来，侧边栏
        会被“未命名对话 · 0 条”这种条目刷屏。这里把空会话过滤掉，但保留
        当前正在用的那一个（否则用户会以为当前会话消失了）。
        """
        if self.session_manager is None:
            return []
        result: list[dict[str, Any]] = []
        for meta in self.session_manager.list():
            if (meta.message_count or 0) <= 0 and meta.id != self.session_id:
                continue
            result.append({
                "id": meta.id,
                "title": meta.title or meta.summary or "",
                "messageCount": meta.message_count,
                "createdAt": meta.created_at.isoformat(),
                "lastActive": meta.last_active.isoformat(),
            })
        return result

    async def _broadcast_session_list(self) -> None:
        await self._broadcast({
            "type": "session_list",
            "data": {"sessions": self._build_session_list()},
        })

    async def _handle_new_session(self) -> None:
        """开始一个全新的对话（侧边栏「新建对话」）。"""
        if self._streaming:
            await self._broadcast({
                "type": "error",
                "data": {"message": "Please wait for the current response to finish."},
            })
            return
        assert self.session_manager is not None
        assert self.agent is not None

        if self.session is not None:
            self.session.close()

        self.session = self.session_manager.create()
        self.session_id = self.session.session_id
        self.agent.session_id = self.session_id
        self.agent._loop_count = 0
        self.agent.clear_active_skills()
        self.conversation = ConversationManager()
        self._persisted_ids = set()
        self._turn_no = 0
        self._stores.clear()

        await self._broadcast({"type": "clear", "data": None})
        await self._broadcast({
            "type": "session_switched",
            "data": {"id": self.session_id, "cwd": os.getcwd(), "title": ""},
        })
        await self._broadcast_session_list()
        # 新会话是空的：能力栏重新下发（清掉上个会话的任务卡片）
        await self._refresh_capabilities()
        await self._broadcast({
            "type": "replay_done",
            "data": {"session": self.session_id, "messages": 0, "title": ""},
        })

    async def _handle_switch_provider(self, provider_name: str) -> None:
        """切换网页端使用的模型/provider。

        协议不同的 provider 之间图片/文档内容块无法互相翻译（比如 Anthropic
        专属的 image/document block 拿去发给 openai-compat 会被直接忽略），
        所以切换 provider 等同于开一个新对话——旧对话历史保留在磁盘上，随时
        可以从侧边栏切回去，只是不会带着切换前的上下文继续聊。
        """
        if self._streaming:
            await self._broadcast({
                "type": "error",
                "data": {"message": "Please wait for the current response to finish."},
            })
            return

        target = next((p for p in self.providers if p.name == provider_name), None)
        if target is None or target.name == self._active_provider.name:
            return

        assert self.session_manager is not None
        assert self.agent is not None

        self._active_provider = target
        self.agent.client = create_client(target)
        self.agent.protocol = target.protocol
        self.agent.context_window = target.get_context_window()
        # 外脑业务（会议分析 / 知识问答 / PPT 大纲）也必须跟随当前模型
        if self.brain_service is not None:
            self.brain_service.provider = target

        if self.session is not None:
            self.session.close()
        self.session = self.session_manager.create()
        self.session_id = self.session.session_id
        self.agent.session_id = self.session_id
        self.agent._loop_count = 0
        self.agent.clear_active_skills()
        self.conversation = ConversationManager()
        self._persisted_ids = set()

        await self._broadcast({"type": "clear", "data": None})
        await self._broadcast({
            "type": "session_switched",
            "data": {
                "id": self.session_id,
                "cwd": os.getcwd(),
                "title": "",
                "visionEnabled": provider_supports_vision(target),
                "activeProvider": target.name,
            },
        })
        await self._broadcast_session_list()
        # 换模型 = 能力栏也要换：DeepSeek 不能出图，千问可以
        await self._refresh_capabilities()

    async def _handle_replay_current(self, websocket: ServerConnection) -> None:
        """把当前会话（当前分支）重放给**发起请求的那个连接**。

        使用场景：用户只是在本页内切到别的页面又切回来。此时前端 DOM 被重建，
        但服务端会话、分支、产物都还在，不应该重新建会话，也不应该再次请求模型
        或重发邮件——只把已有历史重放一遍。

        与 ``_handle_load_session`` 的区别：
        * 不读磁盘、不切分支、不改 ``_persisted_ids``；
        * 只发给请求方，不打断其它已打开标签页；
        * 结束时补一条 ``replay_done``，前端据此决定是否显示欢迎页。
        """
        send = lambda payload: self._send(websocket, payload)  # noqa: E731 - 与既有回放用法保持一致
        if self.conversation is None:
            await send({
                "type": "replay_done",
                "data": {"session": self.session_id, "messages": 0},
            })
            return
        await send({"type": "clear", "data": None})
        if self.conversation.history:
            await self._replay_messages(self.conversation.history, send=send)
        await send({
            "type": "replay_done",
            "data": {
                "session": self.session_id,
                "messages": len(self.conversation.history),
                "title": getattr(getattr(self.session, "meta", None), "title", "") or "",
            },
        })

    async def _handle_load_session(self, session_id: str) -> None:
        """加载并重放一个历史会话。"""
        if self._streaming:
            await self._broadcast({
                "type": "error",
                "data": {"message": "Please wait for the current response to finish."},
            })
            return
        if session_id == self.session_id:
            # 点的是当前会话：不需要重新读盘，但要把历史重放一遍——
            # 用户可能刚从一个空 DOM 的页面切回来（以前这里直接 return，
            # 导致侧边栏点击“当前会话”没有任何反应）。
            if self.conversation is not None:
                await self._broadcast({"type": "clear", "data": None})
                await self._replay_messages(self.conversation.history)
            await self._broadcast({
                "type": "replay_done",
                "data": {
                    "session": self.session_id,
                    "messages": len(self.conversation.history) if self.conversation else 0,
                },
            })
            return
        assert self.session_manager is not None
        assert self.agent is not None

        result = self.session_manager.resume(session_id)
        if result is None:
            await self._broadcast({
                "type": "error",
                "data": {"message": f"Session not found: {session_id}"},
            })
            return

        if self.session is not None:
            self.session.close()

        self.session = result.session
        self.session_id = session_id
        self.agent.session_id = session_id
        self.agent._loop_count = 0
        self.agent.clear_active_skills()

        conv = ConversationManager()
        for msg in result.messages:
            conv.history.append(msg)
        self.conversation = conv
        # 刚从磁盘加载的消息本来就已经落盘了，标记为已持久化，
        # 避免下一轮 flush_new_messages() 把它们当作新消息重复写入。
        self._persisted_ids = {id(m) for m in conv.history}
        # 恢复轮次编号与产物（历史会话重开时卡片要能回到对应回答下）
        self._turn_no = sum(
            1 for m in conv.history if m.role == "user" and self._is_replayable_user(m)
        )
        self._stores.clear()

        await self._broadcast({"type": "clear", "data": None})
        await self._broadcast({
            "type": "session_switched",
            "data": {
                "id": self.session_id,
                "cwd": os.getcwd(),
                "title": result.session.meta.title,
            },
        })

        await self._replay_messages(result.messages)
        # 切回旧会话：把那个会话的能力任务（含未完成的）恢复出来
        await self._refresh_capabilities()
        await self._broadcast({
            "type": "replay_done",
            "data": {
                "session": self.session_id,
                "messages": len(result.messages),
                "title": result.session.meta.title or "",
            },
        })

    @staticmethod
    def _is_replayable_user(message: Message) -> bool:
        """可重放给前端的人类消息（排除系统提醒注入和空消息）。"""
        if not (message.content and not message.images and not message.documents):
            return False
        return not message.content.startswith("<system-reminder>") and not message.content.startswith(
            "Current working directory:"
        )

    # ------------------------------------------------------------------
    # 展示层：工具信息的脱敏 / 截断 / 历史回放载荷
    #
    # 这一层只影响「Web 上看到什么」，不改变 Agent 的实际执行，也不改动
    # 落盘的会话记录。普通模式与开发者模式共用同一份数据，由前端决定渲染。
    # ------------------------------------------------------------------

    # 参数里出现这些键名时一律打码（开发者模式也不例外）
    _SENSITIVE_KEY = re.compile(
        r"(api[_-]?key|token|secret|password|passwd|authorization|cookie|"
        r"credential|private[_-]?key|session[_-]?id)",
        re.IGNORECASE,
    )
    _MAX_ARG_CHARS = 600
    _MAX_OUTPUT_CHARS = 1200

    # 旧版本会把 "> 🔧 used WriteFile, ReadFile" 这类提示直接拼进回答正文。
    # 回放时只剥离这种由程序生成的整行，不会误删正文里正常讨论工具名的句子
    # （要求：兼容旧会话，但不误删用户/模型原本的文字）。
    _LEGACY_TOOL_NOTE = re.compile(
        r"^[ \t]*>[ \t]*🔧[ \t]*(?:used[ \t]+.+|已执行[ \t]*\d+[ \t]*个操作步骤)[ \t]*$",
        re.MULTILINE,
    )

    # 权限确认给普通用户看的动作名（不含工具名/路径）
    _PERMISSION_ACTIONS = {
        "ReadFile": "读取一个文件",
        "WriteFile": "保存一个文件",
        "EditFile": "修改一个文件",
        "Bash": "执行一条命令",
        "Glob": "查找文件",
        "Grep": "搜索文件内容",
        "SaveMemory": "保存一条偏好",
        "WebFetch": "访问一个网页",
        "WebSearch": "联网搜索",
    }

    @classmethod
    def _redact(cls, key: str, value: Any) -> Any:
        """按 key 判断敏感字段，并按长度截断字符串。"""
        if cls._SENSITIVE_KEY.search(str(key)):
            return "«已隐藏»"
        if isinstance(value, str) and len(value) > cls._MAX_ARG_CHARS:
            return value[: cls._MAX_ARG_CHARS] + "…"
        if isinstance(value, (dict, list)):
            # 嵌套结构只做浅层截断，避免把整个大对象原样送到浏览器
            text = json.dumps(value, ensure_ascii=False)
            if len(text) > cls._MAX_ARG_CHARS:
                return text[: cls._MAX_ARG_CHARS] + "…"
        return value

    @classmethod
    def _preview_args(cls, arguments: Any) -> dict[str, Any]:
        if not isinstance(arguments, dict):
            return {}
        return {str(k): cls._redact(str(k), v) for k, v in arguments.items()}

    @classmethod
    def _preview_output(cls, text: Any) -> str:
        if not isinstance(text, str):
            return ""
        text = text.strip()
        if len(text) > cls._MAX_OUTPUT_CHARS:
            return text[: cls._MAX_OUTPUT_CHARS] + "\n…（已截断）"
        return text

    #: 工具写进输出的机器可读标记（与 aieb/brain/tools.py 保持一致）
    _EMAIL_DRAFT_MARKER = "[[AIEB_EMAIL_DRAFT]]"

    @classmethod
    def _parse_email_marker(cls, text: Any) -> tuple[dict[str, Any] | None, str]:
        """从工具输出里取出邮件草稿载荷，并去掉标记。

        返回 ``(草稿 dict 或 None, 去掉标记后的输出)``。标记只由我们自己的工具写入，
        解析失败就当普通文本处理，绝不影响主流程。
        """
        output = text if isinstance(text, str) else ""
        if not output or cls._EMAIL_DRAFT_MARKER not in output:
            return None, output
        head, _, tail = output.partition(cls._EMAIL_DRAFT_MARKER)
        try:
            payload = json.loads(tail.strip().splitlines()[0])
        except (ValueError, IndexError):
            return None, output
        if not isinstance(payload, dict) or not payload.get("id"):
            return None, output
        return payload, head.strip()

    @classmethod
    def _extract_email_draft(cls, event: Any) -> tuple[dict[str, Any] | None, str]:
        """实时事件版本：解析草稿并把输出裁剪到展示长度。"""
        draft, cleaned = cls._parse_email_marker(getattr(event, "output", ""))
        return draft, cls._preview_output(cleaned)

    @classmethod
    def _strip_legacy_tool_notes(cls, text: str) -> str:
        """移除旧版本拼进回答里的工具提示行，其余内容原样保留。"""
        if not text or "🔧" not in text:
            return text
        cleaned = cls._LEGACY_TOOL_NOTE.sub("", text)
        if cleaned == text:
            return text
        return cleaned.rstrip()

    @classmethod
    def _permission_view(cls, tool_name: str, description: str) -> dict[str, str]:
        """把权限请求翻译成普通用户能看懂的一句话。

        ``description`` 可能是绝对路径或命令行：路径只留文件名（完整信息
        仍放在 ``detail`` 里，供开发者模式查看）。
        """
        action = cls._PERMISSION_ACTIONS.get(tool_name, "执行一个操作")
        raw = (description or "").strip()
        target = raw
        if raw and ("/" in raw or "\\" in raw) and len(raw) < 400:
            name = Path(raw.replace("\\", "/")).name
            if name:
                target = name
        if len(target) > 120:
            target = target[:120] + "…"
        return {
            "action": action,
            "target": target,
            "why": "只有你同意后，AI 外脑才会继续执行这一步。",
        }

    async def _replay_messages(self, messages: list[Message], *, send=None) -> None:
        """把历史消息重放到前端。

        结构约定（普通模式和开发者模式共用）：
          * 一条人类消息 → 一个 ``replay_user``；
          * 该轮所有助手文本合并成 **一条** ``replay_assistant``，
            工具调用放在结构化的 ``tools`` 字段里，**不再拼进回答正文**；
          * 产物按轮次挂在该轮回答上。

        ``send`` 默认为广播；刷新页面时传入「只发给本连接」的发送函数，
        这样新开的标签页能补齐当前分支，而不会打断其它标签页。

        历史记录里没有保存工具耗时，因此 ``elapsed`` 明确为 ``None``；
        没有对应 tool_result 的调用状态为 ``unknown``，绝不编造。
        """
        emit = send or self._broadcast
        artifacts_by_turn = self._replay_artifacts()

        # tool_result 是独立的 user 消息，先按 tool_use_id 建索引供配对
        results: dict[str, Any] = {}
        for message in messages:
            for tr in message.tool_results:
                results[tr.tool_use_id] = tr

        turn = 0
        texts: list[str] = []
        tools: list[dict[str, Any]] = []
        email_drafts: list[dict[str, Any]] = []
        # 本轮的「人类提问」（重新生成/版本切换都要靠它的稳定 ID 定位）
        turn_user_id = ""
        turn_version: dict[str, int] = {"index": 0, "total": 1}

        async def flush_turn() -> None:
            nonlocal texts, tools, email_drafts
            has_artifacts = turn in artifacts_by_turn
            if not texts and not tools and not has_artifacts and not email_drafts:
                return
            content = "\n\n".join(t.strip() for t in texts if t.strip())
            data: dict[str, Any] = {
                "content": content,
                # 这条回答属于哪个提问（历史回答也能「重新生成」）
                "user_message_id": turn_user_id,
                "version": turn_version,
            }
            if tools:
                data["tools"] = tools
            if has_artifacts:
                data["artifacts"] = artifacts_by_turn[turn]
            if email_drafts:
                # 刷新/切页后邮件草稿卡片也要回来（持久化在会话的工具输出里）
                data["email_drafts"] = email_drafts
            texts = []
            tools = []
            email_drafts = []
            await emit({"type": "replay_assistant", "data": data})

        for message in messages:
            if message.tool_results:
                continue
            if message.role == "user":
                if (
                    (not message.content and not message.images and not message.documents)
                    or message.content.startswith("<system-reminder>")
                    or message.content.startswith("Current working directory:")
                ):
                    continue
                await flush_turn()
                turn += 1
                turn_user_id = message.message_id or ""
                turn_version = self._version_info(message)
                await emit({
                    "type": "replay_user",
                    "data": {
                        "content": message.content,
                        "message_id": message.message_id,
                        "version": self._version_info(message),
                        "images": [
                            {"media_type": ib.media_type, "data": ib.data}
                            for ib in message.images
                        ],
                        "documents": [
                            {"media_type": db.media_type, "data": db.data, "name": db.name}
                            for db in message.documents
                        ],
                    },
                })
            elif message.role == "assistant":
                text = self._strip_legacy_tool_notes(message.content or "")
                if text.strip():
                    texts.append(text)
                for tu in message.tool_uses:
                    tr = results.get(tu.tool_use_id)
                    raw_output = "" if tr is None else tr.content
                    draft, cleaned = self._parse_email_marker(raw_output)
                    if draft:
                        email_drafts.append(draft)
                    tools.append({
                        "id": tu.tool_use_id,
                        "name": tu.tool_name,
                        "args": self._preview_args(tu.arguments),
                        "status": (
                            "unknown" if tr is None else ("failed" if tr.is_error else "ok")
                        ),
                        "output": self._preview_output(cleaned),
                        "isError": None if tr is None else bool(tr.is_error),
                        # 历史会话不保存耗时：明确留空，不编造
                        "elapsed": None,
                    })

        await flush_turn()

    def _version_info(self, message: Message) -> dict[str, int]:
        """这条消息在当前会话里是「第几版 / 共几版」。

        版本来自 Session 的对话树；没有持久化会话（例如只跑回放的测试）时
        退化成 1/1，前端就不会显示 ‹ › 导航。
        """
        session = getattr(self, "session", None)
        if session is None or not message.message_id:
            return {"index": 0, "total": 1}
        try:
            return session.version_info(message.message_id)
        except Exception:  # noqa: BLE001 - 版本信息可缺省，不能影响重放
            return {"index": 0, "total": 1}

    @staticmethod
    def _parse_attachments(
        data: dict[str, Any],
    ) -> tuple[list[ImageBlock], list[DocumentBlock]]:
        """从 WebSocket 载荷里解析图片/文档附件（发送与编辑共用）。"""
        images_raw = data.get("images", []) or []
        image_blocks = [
            ImageBlock(media_type=i.get("media_type", ""), data=i.get("data", ""))
            for i in images_raw
            if isinstance(i, dict) and i.get("media_type") and i.get("data")
        ]
        documents_raw = data.get("documents", []) or []
        document_blocks = [
            DocumentBlock(
                media_type=d.get("media_type", ""),
                data=d.get("data", ""),
                name=d.get("name", ""),
            )
            for d in documents_raw
            if isinstance(d, dict) and d.get("media_type") and d.get("data")
        ]
        return image_blocks, document_blocks

    def _find_last_human_message_index(self) -> int | None:
        """从后往前找最后一条「人类真正打字发送的」user 消息的下标。

        conversation.history 里 role == "user" 的消息还包括工具结果回填
        （tool_results 非空）和系统提醒注入，这些不是编辑/重新生成的对象。
        """
        assert self.conversation is not None
        for i in range(len(self.conversation.history) - 1, -1, -1):
            if is_human_message(self.conversation.history[i]):
                return i
        return None

    def _find_human_message_index(self, message_id: str) -> int | None:
        """按稳定消息 ID 定位一条可编辑的人类消息。

        这里**不再**靠「最后一条 user 消息」定位：编辑的可能是第 1 轮，也可能
        是中间某一轮。校验顺序：
          1. 消息必须存在于**当前会话**的历史（ID 来自其它会话时自然找不到）；
          2. 必须是人类真正发送的消息（工具结果回填、系统提醒注入一律拒绝）；
          3. 找不到就返回 None，调用方负责给出可读错误。
        """
        if not message_id or self.conversation is None:
            return None
        for i, message in enumerate(self.conversation.history):
            if message.message_id != message_id:
                continue
            if not is_human_message(message):
                return None
            return i
        return None

    async def _start_branch_and_resend(
        self,
        idx: int,
        content: str,
        images: list[ImageBlock] | None,
        documents: list[DocumentBlock] | None,
        *,
        notice: str,
    ) -> None:
        """把历史截到 idx 之前，新提问挂成一条新分支并重新请求模型。

        旧分支的消息**一条都不删除**：jsonl 里只是追加了新的兄弟节点，
        Session 的分支索引负责记住「现在显示哪一条路径」。
        """
        assert self.conversation is not None
        prefix = list(self.conversation.history[:idx])
        parent_id = Session.branch_parent_for(prefix)
        if self.session is not None:
            # 下一次 append 挂到前缀末尾的真实消息 → 形成兄弟分支
            self.session.branch_to(parent_id)
        self.conversation.history = prefix
        self._persisted_ids = {id(m) for m in prefix}

        await self._broadcast({"type": "clear", "data": None})
        await self._replay_messages(prefix)
        if notice:
            await self._broadcast({
                "type": "system",
                "data": {"message": notice},
            })
        await self._handle_user_message(
            content, images=images, documents=documents, _same_turn=True
        )

    def _capability_busy_reason(self) -> str:
        """有生成任务在跑时，分支编辑会跟任务归属撞车，直接拒绝并说明原因。

        视频任务可能跑几分钟，它的产物要挂到「哪一轮」已经确定；此时改写历史
        会让产物归属错乱，所以先拦住，而不是默默把结果挂到新分支上。
        """
        sid = self.session_id or "default"
        hosts = getattr(self, "_cap_hosts", None)
        if not hosts:
            return ""
        host = hosts.get(sid)
        if host is None:
            return ""
        try:
            running = [t for t in host.pending_video_tasks()]
        except Exception:  # noqa: BLE001
            return ""
        if not running:
            return ""
        return (
            "还有生成任务（视频/图片/语音）在进行中，先等它完成或点「停止」，"
            "否则产物会挂到错误的轮次上。"
        )

    async def _handle_regenerate(self, message_id: str = "") -> None:
        """重新生成某一轮用户的回复（默认最后一轮，也支持任意历史轮次）。

        实现为「同一位置的第二个版本」：原来的提问 + 回答作为兄弟分支保留，
        随时可以用 ‹ 1/2 › 切回去。不重放后续轮次，也不重放旧分支的工具调用。
        """
        if self._streaming or self._branch_busy:
            await self._broadcast({
                "type": "error",
                "data": {"message": "请先等当前操作结束，再重新生成。"},
            })
            return
        if self.conversation is None:
            return
        busy = self._capability_busy_reason()
        if busy:
            await self._broadcast({"type": "error", "data": {"message": busy}})
            return

        idx = self._find_human_message_index(message_id) if message_id else None
        if idx is None:
            if message_id:
                await self._broadcast({
                    "type": "error",
                    "data": {"message": "这条提问在当前对话里找不到，无法重新生成。"},
                })
                return
            idx = self._find_last_human_message_index()
        if idx is None:
            return
        target = self.conversation.history[idx]
        self._branch_busy = True
        try:
            await self._start_branch_and_resend(
                idx,
                target.content,
                target.images,
                target.documents,
                notice="",
            )
        finally:
            self._branch_busy = False

    async def _handle_edit_message(
        self,
        message_id: str,
        content: str,
        images: list[ImageBlock] | None = None,
        documents: list[DocumentBlock] | None = None,
    ) -> None:
        """编辑**任意一条**历史用户消息，并生成一条新分支。

        - 附件按前端传来为准：保留的旧附件会原样回传，移除的就不会出现在新
          分支里（不做「静默丢弃附件」）；
        - 旧分支的消息、工具执行记录、已产生的文件/记忆一概不撤销——分支只
          影响对话上下文，不做文件系统回滚。
        """
        if self._streaming or self._branch_busy:
            await self._broadcast({
                "type": "error",
                "data": {"message": "正在生成回答或处理上一条编辑，无法编辑消息。请稍后再试。"},
            })
            return
        if self.conversation is None:
            return

        busy = self._capability_busy_reason()
        if busy:
            await self._broadcast({"type": "error", "data": {"message": busy}})
            return

        idx = self._find_human_message_index(message_id)
        if idx is None and not message_id:
            # 兼容：消息刚发出、前端还没收到 user_message_saved 时没有 ID，
            # 退化成「最后一条用户消息」。有 ID 时一律按 ID 精确定位。
            idx = self._find_last_human_message_index()
        if idx is None:
            await self._broadcast({
                "type": "error",
                "data": {"message": "这条消息在当前对话里找不到，或不是可编辑的用户消息。"},
            })
            return

        self._branch_busy = True
        try:
            await self._start_branch_and_resend(
                idx,
                content,
                images,
                documents,
                notice=(
                    "已创建新的对话分支：之前这一支已经执行过的文件写入、联网操作"
                    "和记忆保存不会自动撤销。可以用消息旁的 ‹ 1/2 › 切回原来的分支。"
                ),
            )
        finally:
            self._branch_busy = False

    async def _handle_switch_turn_version(
        self,
        message_id: str,
        *,
        target_id: str = "",
        index: int | None = None,
    ) -> None:
        """在某条消息的多个版本之间切换。

        纯读取：不调用模型、不执行工具、不产生任何 API 费用，只是把
        conversation.history 换成另一条分支并重放给前端。
        """
        if self._streaming or self._branch_busy:
            await self._broadcast({
                "type": "error",
                "data": {"message": "正在生成回答，暂时不能切换分支。"},
            })
            return
        if self.conversation is None or self.session is None:
            return

        siblings = self.session.siblings(message_id)
        if len(siblings) <= 1:
            return
        target = ""
        if target_id in siblings:
            target = target_id
        elif index is not None and 0 <= index < len(siblings):
            target = siblings[index]
        if not target or target == message_id:
            return

        path = self.session.switch_to(target)
        if not path:
            return
        self.conversation.history = list(path)
        self._persisted_ids = {id(m) for m in path}
        self._turn_no = sum(1 for m in path if is_human_message(m))

        await self._broadcast({"type": "clear", "data": None})
        await self._replay_messages(self.conversation.history)

    async def _handle_delete_session(
        self, session_id: str, *, purge_memories: bool = True
    ) -> None:
        """删除一个历史会话。

        若删除的正是当前活跃会话，先自动切换到最近使用的另一个会话（不存在
        则新建一个），确保删除后界面始终有一个可用的活跃会话——切换会关闭
        旧 session 的文件句柄，之后再删除磁盘文件才不会在 Windows 上因文件
        被占用而失败。

        ``purge_memories`` 来自删除确认框：默认删掉该会话产生的长期记忆
        （用户级 + 项目级）；用户取消勾选时保留记忆，只把来源标记为“已删除
        的对话（保留）”。
        """
        assert self.session_manager is not None

        # 先拿到标题：会话记录删掉之后就查不到了，而“保留记忆”要把标题写进来源
        session_title = ""
        try:
            for meta in self.session_manager.list():
                if meta.id == session_id:
                    session_title = meta.title or ""
                    break
        except OSError:
            session_title = ""

        if session_id == self.session_id:
            if self._streaming:
                await self._broadcast({
                    "type": "error",
                    "data": {"message": "Please wait for the current response to finish."},
                })
                return
            others = [m for m in self.session_manager.list() if m.id != session_id]
            if others:
                await self._handle_load_session(others[0].id)
            else:
                await self._handle_new_session()

        # 若记忆提取还在跑，先等它收尾，否则清理完又会被写回来
        agent = self.agent
        if agent is not None:
            for _ in range(30):
                if not getattr(agent, "_extracting", False):
                    break
                await asyncio.sleep(0.1)

        report = session_purge.purge_session(
            session_id,
            work_dir=getattr(self, "work_dir", None) or self._workspace_root(),
            session_manager=self.session_manager,
            artifact_root=self.artifact_root,
            user_memory_dir=self._user_memory_dir(),
            purge_memories=purge_memories,
            session_title=session_title,
        )
        self._stores.pop(session_id, None)
        # 能力任务记录跟着会话一起删（不再占空间，也不会被 restore 到别的会话）
        hosts = getattr(self, "_cap_hosts", None)
        if isinstance(hosts, dict):
            host = hosts.pop(session_id, None)
            if host is not None:
                host.cleanup()
            try:
                self._capability_state_path(session_id).unlink(missing_ok=True)
            except (OSError, AttributeError):
                pass
        # 记忆面板可能开着：清理/保留后要立刻反映新状态
        await self._broadcast_memory_state()

        if report.ok:
            await self._broadcast({"type": "system", "data": {"message": report.summary()}})
        else:
            # 不静默成功：清理失败必须让用户看到
            log.error("session purge incomplete for %s: %s", session_id, report.errors)
            await self._broadcast({
                "type": "error",
                "data": {
                    "message": "删除会话时部分数据未能清理：\n- "
                    + "\n- ".join(report.errors)
                    + "\n\n会话记录已从列表移除，请稍后重试或检查文件占用。"
                },
            })
        await self._broadcast_session_list()

    @staticmethod
    def _user_memory_dir() -> Path | None:
        """用户级长期记忆目录（~/.aieb/memory）。"""
        try:
            return Path.home() / ".aieb" / "memory"
        except RuntimeError:
            return None

    # ------------------------------------------------------------------
    # Web 记忆管理
    # ------------------------------------------------------------------

    def _session_titles(self) -> dict[str, str]:
        if self.session_manager is None:
            return {}
        try:
            return {meta.id: (meta.title or "未命名对话") for meta in self.session_manager.list()}
        except OSError:
            return {}

    def _memory_payload(self) -> dict[str, Any]:
        mm = self.memory_manager
        if mm is None:
            return {"enabled": True, "items": [], "error": "记忆功能未初始化"}
        try:
            items = mm.list_for_web(self._session_titles())
        except Exception:  # noqa: BLE001
            log.exception("memory list failed")
            return {"enabled": mm.enabled, "items": [], "error": "读取记忆失败"}
        return {"enabled": mm.enabled, "items": items}

    async def _broadcast_memory_state(self) -> None:
        await self._broadcast({"type": "memory_state", "data": self._memory_payload()})

    async def _handle_memory_action(self, data: dict[str, Any]) -> None:
        """删除单条 / 清空范围 / 切换记忆开关。"""
        mm = self.memory_manager
        if mm is None:
            await self._broadcast({
                "type": "memory_action",
                "data": {"ok": False, "message": "记忆功能未初始化"},
            })
            return

        action = str(data.get("action") or "")
        ok = False
        message = "未知操作"

        if action == "delete":
            mem_id = str(data.get("id") or "").strip()
            if not mem_id:
                message = "缺少记忆标识"
            else:
                ok, message = mm.delete_by_id(mem_id)

        elif action == "clear":
            scope = str(data.get("scope") or "project")
            if scope not in {"project", "all"}:
                scope = "project"
            # 若有提取任务正在写记忆，先等它收尾，避免清空后又被写回
            agent = self.agent
            if agent is not None:
                for _ in range(30):
                    if not getattr(agent, "_extracting", False):
                        break
                    await asyncio.sleep(0.1)
            ok, message, removed = mm.clear_scope(scope)
            if ok:
                message = f"已清空 {removed} 条记忆"

        elif action == "set_enabled":
            enabled = bool(data.get("enabled", True))
            ok = mm.set_enabled(enabled)
            message = "已开启记忆功能" if enabled else "已关闭记忆功能（不再保存也不再使用已有记忆）"
            if not ok:
                message = "保存设置失败，请稍后重试"

        await self._broadcast({"type": "memory_action", "data": {"ok": ok, "message": message}})
        await self._broadcast_memory_state()

    def _memory_dirs(self) -> list[Path]:
        """需要跟踪来源的记忆目录（项目级 + 用户级），不存在则跳过。"""
        work = getattr(self, "work_dir", None) or self._workspace_root()
        candidates = [Path(work) / ".aieb" / "memory"]
        user = self._user_memory_dir()
        if user is not None:
            candidates.append(user)
        return [d for d in candidates if d.is_dir()]

    async def _handle_rename_session(self, session_id: str, title: str) -> None:
        """重命名一个历史会话（侧边栏手动改名，此后 append() 不会再自动覆盖标题）。"""
        assert self.session_manager is not None
        title = title[:TITLE_MAX_LENGTH]
        if self.session_manager.rename(session_id, title):
            if session_id == self.session_id and self.session is not None:
                self.session.meta.title = title
            await self._broadcast_session_list()

    async def _handle_compact(self) -> None:
        """处理 /compact 命令。"""
        if self.agent is None or self.conversation is None:
            await self._broadcast({
                "type": "error",
                "data": {"message": "Compact requires an active agent."},
            })
            await self._broadcast({"type": "command_done", "data": None})
            return

        await self._broadcast({
            "type": "system",
            "data": {"message": "Compacting conversation..."},
        })

        result = await self.agent.manual_compact(self.conversation)
        if isinstance(result, CompactNotification):
            await self._broadcast({
                "type": "system",
                "data": {"message": result.message},
            })
        elif isinstance(result, ErrorEvent):
            await self._broadcast({
                "type": "error",
                "data": {"message": result.message},
            })

        await self._broadcast({"type": "command_done", "data": None})

    # ------------------------------------------------------------------
    # UIController 协议实现（供命令系统回调）
    # ------------------------------------------------------------------

    def add_system_message(self, text: str) -> None:
        """同步接口 — 在事件循环中调度广播。"""
        asyncio.ensure_future(self._broadcast({
            "type": "system",
            "data": {"message": text},
        }))

    def send_user_message(self, text: str) -> None:
        """同步接口 — 注入用户消息并触发 agent。"""
        asyncio.create_task(self._handle_user_message(text))

    def set_plan_mode(self, enabled: bool) -> None:
        if self.agent is None:
            return
        if enabled:
            self.agent.set_permission_mode(PermissionMode.PLAN)
        else:
            self.agent.set_permission_mode(PermissionMode.DEFAULT)

    def get_token_count(self) -> tuple[int, int]:
        if self.agent:
            return self.agent.total_input_tokens, self.agent.total_output_tokens
        return 0, 0

    def refresh_status(self) -> None:
        pass  # Remote 模式不需要刷新 TUI 状态栏

    # ------------------------------------------------------------------
    # 权限响应处理
    # ------------------------------------------------------------------

    def _handle_permission_response(self, data: dict[str, Any]) -> None:
        """处理来自 Web UI 的权限回复。"""
        perm_id = data.get("id", "")
        response_str = data.get("response", "deny")

        future = self._pending_perms.pop(perm_id, None)
        if future is None or future.done():
            return

        # 映射字符串到枚举
        mapping = {
            "allow": PermissionResponse.ALLOW,
            "deny": PermissionResponse.DENY,
            "allowAlways": PermissionResponse.ALLOW_ALWAYS,
        }
        response = mapping.get(response_str, PermissionResponse.DENY)
        future.set_result(response)

    async def _expire_permission(
        self, perm_id: str, future: "asyncio.Future[PermissionResponse]"
    ) -> None:
        """浏览器端一直不回复时，超时按拒绝处理（ASK ≠ ALLOW）。"""
        try:
            await asyncio.sleep(PERMISSION_TIMEOUT_SECONDS)
        except asyncio.CancelledError:  # pragma: no cover
            return
        if future.done():
            return
        self._pending_perms.pop(perm_id, None)
        future.set_result(PermissionResponse.DENY)
        log.info("permission request timed out, denied: %s", perm_id)

    # ------------------------------------------------------------------
    # 辅助方法
    # ------------------------------------------------------------------

    def _build_command_list(self) -> list[dict[str, str]]:
        """构建命令列表，推送给前端用于斜杠命令菜单。"""
        result = []
        for cmd in self.command_registry.list_commands():
            result.append({
                "name": cmd.name,
                "description": cmd.description,
            })
        return result

    async def _broadcast(self, msg: dict[str, Any]) -> None:
        """向所有已连接的 WebSocket 客户端广播消息。"""
        if not self._connections:
            return
        data = json.dumps(msg, ensure_ascii=False)
        # 复制集合避免迭代中修改
        closed = []
        for ws in list(self._connections):
            try:
                await ws.send(data)
            except websockets.ConnectionClosed:
                closed.append(ws)
            except Exception:
                closed.append(ws)
        for ws in closed:
            self._connections.discard(ws)

    async def _send(self, websocket: ServerConnection, msg: dict[str, Any]) -> None:
        """只发给单个连接（用于「新连接补齐当前对话」这类定向重放）。"""
        try:
            await websocket.send(json.dumps(msg, ensure_ascii=False))
        except (websockets.ConnectionClosed, Exception):  # noqa: BLE001 - 连接断开即放弃
            self._connections.discard(websocket)
