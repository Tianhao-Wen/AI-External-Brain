
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .validator import (
    ConfigError,
    DEFAULT_CONTEXT_WINDOW,
    VALID_PERMISSION_MODES,
    VALID_PROTOCOLS,
    VALID_TEAMMATE_MODES,
    lookup_model_context_window,
    validate_config_structure,
)


_ENV_KEY_MAP = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "openai-compat": "OPENAI_API_KEY",
}

_ENV_VAR_RE = re.compile(r"\$\{([^}]+)\}")


@dataclass
class ProviderConfig:
    name: str
    protocol: str
    base_url: str
    model: str
    api_key: str = ""
    thinking: bool = False
    # 0 表示"未设置" — get_context_window() 通过四层 fallback 解析真实窗口大小。
    # 正数表示配置文件里显式指定的覆盖值。
    context_window: int = 0
    max_output_tokens: int = 0
    # 仅用于 UI 展示（例如 Web 模型选择器），不参与任何请求
    display_name: str = ""
    # 运行时 cache，存放从 provider 的 /v1/models 端点自动拉取的 context window
    # （get_context_window 的第 2 层）。通过 set_fetched_context_window() 写入一次；
    # 0 表示"尚未拉取"。不会持久化。
    _fetched_context_window: int = field(default=0, repr=False)

    def resolve_api_key(self) -> str:
        """解析 API key。

        依次尝试：配置文件里的值（支持 ``${ENV_VAR}`` 展开）→ 协议对应的
        环境变量（ANTHROPIC_API_KEY / OPENAI_API_KEY）。未展开的占位符
        会被当作“未配置”，避免把字面量 ``${FOO}`` 当成 key 发给 provider。
        """
        if self.api_key:
            expanded = resolve_env_vars(self.api_key)
            if expanded and "${" not in expanded:
                return expanded
        env_var = _ENV_KEY_MAP.get(self.protocol, "")
        return os.environ.get(env_var, "")

    def set_fetched_context_window(self, window: int) -> None:
        """记录从 provider 自动拉取到的 context window（第 2 层）。

        非正数会被忽略，这样一次失败的拉取就不会污染 cache。在解析
        context window 时，每个 provider 只会调用一次。
        """
        if window > 0:
            self._fetched_context_window = window

    def get_context_window(self) -> int:
        """通过四层 fallback 解析模型的 context window，按优先级从高到低：

          1. 配置文件提供的 context_window（> 0）——显式覆盖，永远优先。
          2. 从 provider 的 /v1/models 端点自动拉取并通过 set_fetched_context_window
             缓存的值（只有 anthropic 协议的 provider 才会设置它；拉取失败或缺失时
             保持为 0 并跳过）。
          3. 内置的「模型名 -> window」映射表（按子串匹配）。
          4. 保守的默认值（claude -> 200000，其他 -> 128000）。
        """
        if self.context_window > 0:
            return self.context_window
        if self._fetched_context_window > 0:
            return self._fetched_context_window
        window = lookup_model_context_window(self.model)
        if window > 0:
            return window
        if "claude" in self.model.lower():
            return DEFAULT_CONTEXT_WINDOW
        return 128_000

    def get_max_output_tokens(self) -> int:
        if self.max_output_tokens > 0:
            return self.max_output_tokens
        if self.thinking:
            return 64000
        return 8192


def resolve_env_vars(value: str) -> str:
    return _ENV_VAR_RE.sub(lambda m: os.environ.get(m.group(1), m.group(0)), value)


def build_child_env(declared_env: dict[str, str] | None) -> dict[str, str]:
    env: dict[str, str] = {}
    path = os.environ.get("PATH", "")
    if path:
        env["PATH"] = path
    for key, value in (declared_env or {}).items():
        env[key] = resolve_env_vars(value)
    return env


@dataclass
class MCPServerConfig:
    name: str
    command: str | None = None
    args: list[str] = field(default_factory=list)
    url: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    env: dict[str, str] = field(default_factory=dict)


    @property
    def is_stdio(self) -> bool:
        return self.command is not None


@dataclass
class WorktreeConfig:
    symlink_directories: list[str] = field(default_factory=lambda: ["node_modules", ".venv", "vendor"])
    stale_cleanup_interval: int = 3600
    stale_cutoff_hours: int = 24


@dataclass
class SandboxAppConfig:
    """沙箱相关的配置项。

    字段类型为 ``bool | None``：None 表示“本层未配置”（由 validator 产生），
    直接构造时默认 False，避免调用方拿到 None。
    """
    enabled: bool | None = False       # 是否启用 OS 级沙箱（仅 macOS/Linux 真实可用）
    auto_allow: bool | None = False    # 是否在沙箱可用时自动放行命令
    network_enabled: bool | None = False  # 沙箱内是否允许网络访问


@dataclass
class RemoteConfig:
    """Remote（浏览器 UI）监听配置。

    默认只监听 loopback；host/port 为 None 表示未配置，由调用方给默认值。
    auth_token 建议用环境变量 ``AIEB_REMOTE_TOKEN``（历史名 ``WENDREAM_REMOTE_TOKEN`` 仍可用）提供，不要写进仓库。
    """
    host: str | None = None
    port: int | None = None
    auth_token: str | None = None
    allowed_origins: list[str] = field(default_factory=list)


@dataclass
class WebSearchConfig:
    """联网搜索配置。

    API Key 不在这个结构里：只从环境变量 ``TAVILY_API_KEY`` 读取，
    避免密钥被写进配置文件或随配置传输。

    ``provider``：``auto``（有 Key 用 Tavily，否则 DuckDuckGo）/ ``tavily`` / ``duckduckgo``。
    """

    enabled: bool = True
    provider: str = "auto"
    max_results: int = 5
    timeout: float = 15.0
    # 每轮预算：6 次搜索/6 次读网页足够一次新闻调研，且不会失控
    max_searches_per_turn: int = 6
    max_fetches_per_turn: int = 6
    # 仅对“可重试”错误（网络/5xx/限流）生效；默认 0 次，避免放大计费与限流
    max_retries: int = 0
    # 是否允许使用 provider 原生联网（目前支持百炼 qwen 系列）
    native_enabled: bool = False
    # 指向 providers: 里某个 provider 的 name（复用它的 base_url / model / key）
    native_provider: str = ""


@dataclass
class AppConfig:
    providers: list[ProviderConfig]
    permission_mode: str | None = "acceptEdits"
    mcp_servers: list[MCPServerConfig] = field(default_factory=list)
    raw_hooks: list[dict] = field(default_factory=list)
    enable_fork: bool | None = False
    enable_verification_agent: bool | None = False
    worktree: WorktreeConfig = field(default_factory=WorktreeConfig)
    teammate_mode: str = ""
    enable_coordinator_mode: bool | None = False
    sandbox: SandboxAppConfig = field(default_factory=SandboxAppConfig)
    remote: RemoteConfig = field(default_factory=RemoteConfig)
    web_search: WebSearchConfig = field(default_factory=WebSearchConfig)


def _load_single_file(path: Path) -> AppConfig:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise ConfigError(f"Failed to parse config {path}: {e}") from e

    validated = validate_config_structure(raw)

    providers = [
        ProviderConfig(
            name=p["name"],
            protocol=p["protocol"],
            base_url=p["base_url"],
            model=p["model"],
            api_key=p["api_key"],
            thinking=p["thinking"],
            context_window=p["context_window"],
            max_output_tokens=p["max_output_tokens"],
            display_name=p.get("display_name", ""),
        )
        for p in validated["providers"]
    ]

    mcp_servers = [
        MCPServerConfig(
            name=s["name"],
            command=s["command"],
            args=s["args"],
            url=s["url"],
            headers=s["headers"],
            env=s["env"],
        )
        for s in validated["mcp_servers"]
    ]

    wt = validated["worktree"]
    worktree_cfg = WorktreeConfig(
        symlink_directories=wt["symlink_directories"],
        stale_cleanup_interval=wt["stale_cleanup_interval"],
        stale_cutoff_hours=wt["stale_cutoff_hours"],
    )

    sb = validated["sandbox"]
    sandbox_cfg = SandboxAppConfig(
        enabled=sb["enabled"],
        auto_allow=sb["auto_allow"],
        network_enabled=sb["network_enabled"],
    )

    rm = validated["remote"]
    remote_cfg = RemoteConfig(
        host=rm["host"],
        port=rm["port"],
        auth_token=rm["auth_token"],
        allowed_origins=list(rm["allowed_origins"] or []),
    )

    return AppConfig(
        providers=providers,
        # None = 本层未配置，留给合并后统一归一化（否则项目层会覆盖用户层）
        permission_mode=validated["permission_mode"],
        mcp_servers=mcp_servers,
        raw_hooks=validated["hooks"],
        enable_fork=validated["enable_fork"],
        enable_verification_agent=validated["enable_verification_agent"],
        worktree=worktree_cfg,
        teammate_mode=validated["teammate_mode"],
        enable_coordinator_mode=validated["enable_coordinator_mode"],
        sandbox=sandbox_cfg,
        remote=remote_cfg,
        web_search=WebSearchConfig(
            enabled=validated["web_search"]["enabled"],
            provider=validated["web_search"]["provider"],
            max_results=validated["web_search"]["max_results"],
            timeout=validated["web_search"]["timeout"],
            max_searches_per_turn=validated["web_search"]["max_searches_per_turn"],
            max_fetches_per_turn=validated["web_search"]["max_fetches_per_turn"],
            max_retries=validated["web_search"]["max_retries"],
            native_enabled=validated["web_search"]["native_enabled"],
            native_provider=validated["web_search"]["native_provider"],
        ),
    )


def _merge_config(base: AppConfig, override: AppConfig) -> AppConfig:
    """把 override 层合并进 base。

    关键规则：只有 override **显式配置** 的字段才覆盖；
    ``None`` 表示未配置，必须保留上一层取值。绝不能用 truthiness 判断，
    否则“显式 false”永远无法覆盖上层的 true。
    """
    if override.providers:
        base.providers = override.providers
    if override.permission_mode:
        base.permission_mode = override.permission_mode

    if override.mcp_servers:
        by_name = {s.name: i for i, s in enumerate(base.mcp_servers)}
        for s in override.mcp_servers:
            if s.name in by_name:
                base.mcp_servers[by_name[s.name]] = s
            else:
                base.mcp_servers.append(s)
                by_name[s.name] = len(base.mcp_servers) - 1

    base.raw_hooks.extend(override.raw_hooks)
    for field_name in (
        "enable_fork",
        "enable_verification_agent",
        "enable_coordinator_mode",
    ):
        value = getattr(override, field_name)
        if value is not None:
            setattr(base, field_name, value)
    if override.teammate_mode:
        base.teammate_mode = override.teammate_mode

    # 沙箱配置：逐字段独立合并（同样区分未配置与显式 false）
    for sb_field in ("enabled", "auto_allow", "network_enabled"):
        value = getattr(override.sandbox, sb_field)
        if value is not None:
            setattr(base.sandbox, sb_field, value)

    # Remote：未配置的字段保留上层
    if override.remote.host is not None:
        base.remote.host = override.remote.host
    if override.remote.port is not None:
        base.remote.port = override.remote.port
    if override.remote.auth_token is not None:
        base.remote.auth_token = override.remote.auth_token
    if override.remote.allowed_origins:
        base.remote.allowed_origins = list(override.remote.allowed_origins)

    # 联网搜索：逐字段合并（None = 本层未配置）
    for ws_field in (
        "enabled",
        "provider",
        "max_results",
        "timeout",
        "max_searches_per_turn",
        "max_fetches_per_turn",
        "max_retries",
        "native_enabled",
        "native_provider",
    ):
        value = getattr(override.web_search, ws_field)
        if value is not None:
            setattr(base.web_search, ws_field, value)
    return base


def _normalize(config: AppConfig) -> AppConfig:
    """合并完成后把 None 归一成最终运行时取值（消费者不需要处理 None）。"""
    config.enable_fork = bool(config.enable_fork)
    config.enable_verification_agent = bool(config.enable_verification_agent)
    config.enable_coordinator_mode = bool(config.enable_coordinator_mode)
    config.sandbox.enabled = bool(config.sandbox.enabled)
    config.sandbox.auto_allow = bool(config.sandbox.auto_allow)
    config.sandbox.network_enabled = bool(config.sandbox.network_enabled)
    if not config.permission_mode:
        config.permission_mode = "acceptEdits"
    # 搜索配置：None → 默认值
    ws = config.web_search
    if ws.enabled is None:
        ws.enabled = True
    if not ws.provider:
        ws.provider = "auto"
    if ws.max_results is None:
        ws.max_results = 5
    if ws.timeout is None:
        ws.timeout = 15.0
    if ws.max_searches_per_turn is None:
        ws.max_searches_per_turn = 6
    if ws.max_fetches_per_turn is None:
        ws.max_fetches_per_turn = 6
    if ws.max_retries is None:
        ws.max_retries = 0
    if ws.native_enabled is None:
        ws.native_enabled = False
    if ws.native_provider is None:
        ws.native_provider = ""
    return config


def load_config(path: Path | None = None) -> AppConfig:
    if path is not None:
        if not path.exists():
            raise ConfigError(f"Config file not found: {path}")
        return _normalize(_load_single_file(path))

    cwd = Path.cwd()
    home = Path.home()
    candidates = [
        home / ".aieb" / "config.yaml",
        cwd / ".aieb" / "config.yaml",
        cwd / ".aieb" / "config.local.yaml",
    ]

    merged: AppConfig | None = None
    for p in candidates:
        if not p.exists():
            continue
        layer = _load_single_file(p)
        if merged is None:
            merged = layer
        else:
            merged = _merge_config(merged, layer)

    if merged is None:
        raise ConfigError(
            "No config file found. Expected .aieb/config.yaml "
            "in project or ~/.aieb/config.yaml"
        )
    return _normalize(merged)
