"""ModelRegistry：AI 外脑 Demo 的统一模型目录。

三个状态必须严格区分（Phase 1 要求）：

* ``registered``  —— 在目录里，有 id / provider / 能力标签（只是"知道有这个模型"）
* ``implemented`` —— 有对应的适配器代码路径（能真的发请求）
* ``verified``    —— 真实调用成功过（有 request_id / 产出物为证）

再接上两个运行时状态：

* ``configured``  —— 服务端有 base_url + API Key
* ``available``   —— 百炼 Token Plan 的 ``/models`` 里真实存在（套餐覆盖）

``auto`` 是**路由别名**，不是模型：它按能力解析到该能力的默认模型，
所以 ``resolve_model_id("auto", Capability.IMAGE_GENERATE)`` 得到的是一张图模型，
而 ``resolve_model_id("auto", Capability.CHAT)`` 得到的是默认对话模型。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from enum import Enum

# ---------------------------------------------------------------------------
# 能力
# ---------------------------------------------------------------------------


class Capability(str, Enum):
    CHAT = "chat"
    WRITING = "writing"
    VISION_UNDERSTANDING = "vision_understanding"
    IMAGE_GENERATE = "image_generate"
    VIDEO_GENERATE = "video_generate"
    AUDIO_TRANSCRIBE = "audio_transcribe"
    AUDIO_SPEECH = "audio_speech"
    REALTIME_VOICE = "realtime_voice"
    WEB_SEARCH = "web_search"
    WEB_FETCH = "web_fetch"
    IMAGE_SEARCH = "image_search"
    TEXT_IMAGE_SEARCH = "text_image_search"
    CODE_INTERPRETER = "code_interpreter"


#: 生成类能力：一次调用产出一个（或多个）文件，必须登记到 Artifact
GENERATIVE_CAPABILITIES = frozenset({
    Capability.IMAGE_GENERATE,
    Capability.VIDEO_GENERATE,
    Capability.AUDIO_SPEECH,
})

# ---------------------------------------------------------------------------
# Provider / 协议
# ---------------------------------------------------------------------------

PROVIDER_BAILIAN = "bailian-token-plan"
PROVIDER_DEEPSEEK_DIRECT = "deepseek"
PROVIDER_AIEB = "aieb"

#: OpenAI 兼容 chat（现有对话链路，不动）
PROTOCOL_OPENAI_CHAT = "openai-chat-completions"
#: 百炼多模态生成（图片已验证：input.messages + content[].image）
PROTOCOL_MULTIMODAL = "dashscope-multimodal-generation"
#: 百炼异步任务（视频：提交 → 轮询 /api/v1/tasks/{id}）
PROTOCOL_ASYNC_TASK = "dashscope-async-task"
#: 百炼语音合成（已验证：/api/v1/services/audio/tts/SpeechSynthesizer）
PROTOCOL_TTS = "dashscope-tts"
#: 百炼语音识别（异步转录；本套餐未打通，见 verified 字段）
PROTOCOL_ASR = "dashscope-asr"
#: 实时语音 WebSocket（已验证握手）
PROTOCOL_REALTIME = "dashscope-realtime-ws"
#: 本项目自带的能力（WebSearch / WebFetch）
PROTOCOL_LOCAL_TOOL = "aieb-tool"

AUTO_ALIAS = "auto"


@dataclass
class ModelSpec:
    id: str
    provider: str
    capabilities: frozenset[Capability]
    input_modalities: frozenset[str]
    output_modalities: frozenset[str]
    protocol: str
    display_name: str = ""
    streaming: bool = False
    async_task: bool = False
    tool_calling: bool = False
    needs_upload: bool = False
    #: 目录里存在（注册态）
    registered: bool = True
    #: 有适配器实现
    implemented: bool = True
    #: 真实调用成功过
    verified: bool = False
    #: 服务端有凭据
    configured: bool = False
    #: 百炼 /models 里真实存在（套餐覆盖）
    available: bool = True
    #: 是否允许普通用户在功能栏里选
    user_selectable: bool = True
    #: 验证备注/限制（给开发者看的、可公开的信息）
    notes: str = ""
    #: 已验证通过的具体证据（endpoint + 观察到的返回要点）
    verified_via: str = ""
    extra: dict[str, str] = field(default_factory=dict)

    def supports(self, capability: Capability) -> bool:
        return capability in self.capabilities

    def public_dict(self) -> dict[str, object]:
        """给前端/API 的只读描述（不含任何凭据）。"""
        return {
            "id": self.id,
            "display_name": self.display_name or self.id,
            "provider": self.provider,
            "capabilities": sorted(c.value for c in self.capabilities),
            "input_modalities": sorted(self.input_modalities),
            "output_modalities": sorted(self.output_modalities),
            "protocol": self.protocol,
            "streaming": self.streaming,
            "async_task": self.async_task,
            "tool_calling": self.tool_calling,
            "needs_upload": self.needs_upload,
            "registered": self.registered,
            "implemented": self.implemented,
            "verified": self.verified,
            "configured": self.configured,
            "available": self.available,
            "user_selectable": self.user_selectable,
            "usable": self.usable,
            "notes": self.notes,
        }

    @property
    def usable(self) -> bool:
        """当前是否真的可调用：有实现 + 有凭据 + 套餐里有。"""
        return self.implemented and self.configured and self.available

    @property
    def trustworthy(self) -> bool:
        """是否可以向普通用户宣称"已验证可用"（默认不宣称）。"""
        return self.usable and self.verified


# ---------------------------------------------------------------------------
# 目录内容
# ---------------------------------------------------------------------------

_TEXT = frozenset({Capability.CHAT, Capability.WRITING})
_TEXT_VISION = _TEXT | {Capability.VISION_UNDERSTANDING}


def _m(**kwargs) -> ModelSpec:
    return ModelSpec(**kwargs)


MODEL_REGISTRY: dict[str, ModelSpec] = {}


def _register(spec: ModelSpec) -> None:
    MODEL_REGISTRY[spec.id] = spec


# ── 文本 / 推理 / 视觉理解 ────────────────────────────────────────────
for _id, _display, _caps, _tools, _stream in (
    ("qwen3.8-max", "千问 3.8-Max", _TEXT_VISION, True, True),
    ("qwen3.8-flash", "千问 3.8-Flash", _TEXT_VISION, True, True),
    ("qwen3.7-max", "千问 3.7-Max", _TEXT_VISION, True, True),
    ("qwen3.7-plus", "千问 3.7-Plus", _TEXT_VISION, True, True),
    ("qwen3.6-flash", "千问 3.6-Flash", _TEXT_VISION, True, True),
    ("decision-model-preview", "决策模型（预览）", _TEXT, False, True),
):
    _register(_m(
        id=_id,
        provider=PROVIDER_BAILIAN,
        capabilities=_caps,
        input_modalities=frozenset({"text", "image"}),
        output_modalities=frozenset({"text"}),
        protocol=PROTOCOL_OPENAI_CHAT,
        display_name=_display,
        streaming=_stream,
        tool_calling=_tools,
        verified=(_id == "qwen3.8-max"),
        verified_via=(
            "compatible-mode/v1/chat/completions 真实调用（Web 主模型、工具调用均跑通）"
            if _id == "qwen3.8-max" else ""
        ),
        notes=(
            "视觉理解能力随模型声明，未单独做图片实测"
            if _id != "qwen3.8-max"
            else "对话+工具调用已验证；视觉理解未单独实测"
        ),
    ))

for _id, _display in (
    ("deepseek-v4.1-flash-1017", "DeepSeek V4.1 Flash（1017）"),
    ("deepseek-v4.1-flash", "DeepSeek V4.1 Flash"),
    ("deepseek-v4-pro-0813", "DeepSeek V4-Pro（0813）"),
    ("deepseek-v4-pro", "DeepSeek V4-Pro"),
    ("deepseek-v4-flash-0731", "DeepSeek V4-Flash（0731）"),
    ("glm-5.3", "GLM-5.3"),
    ("glm-5.2", "GLM-5.2"),
):
    _register(_m(
        id=_id,
        provider=PROVIDER_BAILIAN,
        capabilities=_TEXT,
        input_modalities=frozenset({"text"}),
        output_modalities=frozenset({"text"}),
        protocol=PROTOCOL_OPENAI_CHAT,
        display_name=_display,
        streaming=True,
        tool_calling=True,
        notes="Token Plan 兼容模式下未逐个实测；仓库内 deepseek provider（api.deepseek.com）的对话链路已验证",
    ))

# ── 图片生成 ─────────────────────────────────────────────────────────
for _id, _display, _ref, _verified, _via in (
    (
        "qwen-image-3.0-pro", "千问图像 3.0-Pro", True, True,
        "POST /api/v1/services/aigc/multimodal-generation/generation（input.messages）→ 200 + 真实图片 URL",
    ),
    (
        "wan2.7-image", "万相 2.7-Image", True, True,
        "同上端点，wan2.7-image → 200 + 真实图片 URL",
    ),
    (
        "wan2.7-image-pro", "万相 2.7-Image-Pro", True, False,
        "同家族端点，未单独跑生成（省 Credits）",
    ),
):
    _register(_m(
        id=_id,
        provider=PROVIDER_BAILIAN,
        capabilities=frozenset({Capability.IMAGE_GENERATE}),
        input_modalities=frozenset({"text", "image"}) if _ref else frozenset({"text"}),
        output_modalities=frozenset({"image"}),
        protocol=PROTOCOL_MULTIMODAL,
        display_name=_display,
        verified=_verified,
        verified_via=_via,
        notes="支持参考图（图生图编辑）的模型会带上 input.messages 中的图片块",
    ))

# ── 视频生成（异步任务）─────────────────────────────────────────────
_register(_m(
    id="wan3.0-video",
    provider=PROVIDER_BAILIAN,
    capabilities=frozenset({Capability.VIDEO_GENERATE}),
    input_modalities=frozenset({"text", "image"}),
    output_modalities=frozenset({"video"}),
    protocol=PROTOCOL_ASYNC_TASK,
    display_name="万相 3.0-Video",
    async_task=True,
    verified=True,
    verified_via=(
        "POST /api/v1/services/aigc/video-generation/video-synthesis"
        "（X-DashScope-Async: enable）→ task_id；GET /api/v1/tasks/{id} → SUCCEEDED + mp4"
    ),
    notes="实测约 3 分钟出片；提交后必须轮询，不能同步等待",
))

for _id, _mods, _display in (
    ("happyhorse-1.1-t2v", frozenset({"text"}), "HappyHorse 1.1 文生视频"),
    ("happyhorse-1.1-i2v", frozenset({"text", "image"}), "HappyHorse 1.1 图生视频"),
    ("happyhorse-1.1-r2v", frozenset({"text", "image", "video"}), "HappyHorse 1.1 参考视频"),
):
    _register(_m(
        id=_id,
        provider=PROVIDER_BAILIAN,
        capabilities=frozenset({Capability.VIDEO_GENERATE}),
        input_modalities=_mods,
        output_modalities=frozenset({"video"}),
        protocol=PROTOCOL_ASYNC_TASK,
        display_name=_display,
        async_task=True,
        # 关键事实：Token Plan 的 /models 里**没有**这三个模型
        available=False,
        implemented=False,
        verified=False,
        user_selectable=False,
        notes="当前 Token Plan 的 /models 列表未包含该模型，调用前会直接返回 model_not_supported",
    ))

# ── 语音 ─────────────────────────────────────────────────────────────
_register(_m(
    id="qwen-audio-3.0-tts-plus",
    provider=PROVIDER_BAILIAN,
    capabilities=frozenset({Capability.AUDIO_SPEECH}),
    input_modalities=frozenset({"text"}),
    output_modalities=frozenset({"audio"}),
    protocol=PROTOCOL_TTS,
    display_name="千问语音合成 3.0-Plus",
    verified=True,
    verified_via=(
        "POST /api/v1/services/audio/tts/SpeechSynthesizer（voice=longanqian）"
        "→ 200 + 可下载 wav"
    ),
    notes="音色必须用该模型的系统音色（longanqian 实测可用；Cherry/longanhuan 会返回 Engine error 411）",
    extra={"default_voice": "longanqian", "max_chars": "600"},
))

_register(_m(
    id="qwen-audio-3.0-asr-flash",
    provider=PROVIDER_BAILIAN,
    capabilities=frozenset({Capability.AUDIO_TRANSCRIBE}),
    input_modalities=frozenset({"audio"}),
    output_modalities=frozenset({"text"}),
    protocol=PROTOCOL_ASR,
    display_name="千问语音识别 3.0-Flash",
    needs_upload=True,
    verified=False,
    verified_via="",
    notes=(
        "官方文档：Qwen-Audio-3.x-ASR-Flash 为同步模型；本 Token Plan 对该模型"
        "的同步调用返回 403 current user api does not support synchronous calls，"
        "异步路径返回 400 url error，尚无可用的调用方式，标为未验证"
    ),
))

_register(_m(
    id="qwen-audio-3.0-realtime-plus",
    provider=PROVIDER_BAILIAN,
    capabilities=frozenset({Capability.REALTIME_VOICE}),
    input_modalities=frozenset({"text", "audio"}),
    output_modalities=frozenset({"text", "audio"}),
    protocol=PROTOCOL_REALTIME,
    display_name="千问实时语音 3.0-Plus",
    streaming=True,
    verified=True,
    verified_via=(
        "wss://token-plan.cn-beijing.maas.aliyuncs.com/api-ws/v1/realtime"
        "?model=qwen-audio-3.0-realtime-plus → session.created（voice=longanqian）"
    ),
    notes="只验证了连接与握手（session.created / session.updated），未实现完整通话界面",
))

# ── 本项目自建能力（已有实现，直接复用）────────────────────────────
_register(_m(
    id="aieb-web-search",
    provider=PROVIDER_AIEB,
    capabilities=frozenset({Capability.WEB_SEARCH}),
    input_modalities=frozenset({"text"}),
    output_modalities=frozenset({"text"}),
    protocol=PROTOCOL_LOCAL_TOOL,
    display_name="AI 外脑联网搜索",
    configured=True,  # 不依赖外部凭据（需要时再用 TAVILY_API_KEY，否则走 Bing）
    verified=True,
    verified_via="现有 WebSearch 工具（Bing/Tavily/DDG/原生）已在真实会话中跑通",
    user_selectable=False,
))

_register(_m(
    id="aieb-web-fetch",
    provider=PROVIDER_AIEB,
    capabilities=frozenset({Capability.WEB_FETCH}),
    input_modalities=frozenset({"text"}),
    output_modalities=frozenset({"text"}),
    protocol=PROTOCOL_LOCAL_TOOL,
    display_name="AI 外脑网页读取",
    configured=True,
    verified=True,
    verified_via="现有 WebFetch 工具（SSRF 防护 + 正文提取）已在真实会话中跑通",
    user_selectable=False,
))

# ── 百炼独有、尚未适配的能力（登记但明确标注不可用）──────────────────
for _id, _cap, _display, _note in (
    (
        "bailian-image-search", Capability.IMAGE_SEARCH, "百炼以图搜图",
        "官方需要多模态检索/图像检索接口，Token Plan 未在 /models 暴露，未实现",
    ),
    (
        "bailian-text-image-search", Capability.TEXT_IMAGE_SEARCH, "百炼文搜图",
        "同上：需要检索库/知识库侧接口，未实现",
    ),
    (
        "bailian-code-interpreter", Capability.CODE_INTERPRETER, "百炼代码解释器",
        "沙箱执行属于 Agent 侧能力，本项目已有本地 Bash 执行路径，未接入云端解释器",
    ),
):
    _register(_m(
        id=_id,
        provider=PROVIDER_BAILIAN,
        capabilities=frozenset({_cap}),
        input_modalities=frozenset({"text"}),
        output_modalities=frozenset({"text"}),
        protocol=PROTOCOL_MULTIMODAL,
        display_name=_display,
        available=False,
        implemented=False,
        verified=False,
        user_selectable=False,
        notes=_note,
    ))


# ---------------------------------------------------------------------------
# 默认模型（每个能力的路由目标；auto 解析到这里）
# ---------------------------------------------------------------------------

DEFAULT_MODELS: dict[Capability, str] = {
    Capability.CHAT: "qwen3.8-max",
    Capability.WRITING: "qwen3.8-max",
    Capability.VISION_UNDERSTANDING: "qwen3.8-max",
    Capability.IMAGE_GENERATE: "qwen-image-3.0-pro",
    Capability.VIDEO_GENERATE: "wan3.0-video",
    Capability.AUDIO_TRANSCRIBE: "qwen-audio-3.0-asr-flash",
    Capability.AUDIO_SPEECH: "qwen-audio-3.0-tts-plus",
    Capability.REALTIME_VOICE: "qwen-audio-3.0-realtime-plus",
    Capability.WEB_SEARCH: "aieb-web-search",
    Capability.WEB_FETCH: "aieb-web-fetch",
    Capability.IMAGE_SEARCH: "bailian-image-search",
    Capability.TEXT_IMAGE_SEARCH: "bailian-text-image-search",
    Capability.CODE_INTERPRETER: "bailian-code-interpreter",
}


# ---------------------------------------------------------------------------
# 查询 / 状态更新
# ---------------------------------------------------------------------------


def get_model(model_id: str) -> ModelSpec | None:
    return MODEL_REGISTRY.get(model_id)


def list_models(
    *,
    capability: Capability | None = None,
    selectable_only: bool = False,
    usable_only: bool = False,
) -> list[ModelSpec]:
    out: list[ModelSpec] = []
    for spec in MODEL_REGISTRY.values():
        if capability is not None and not spec.supports(capability):
            continue
        if selectable_only and not spec.user_selectable:
            continue
        if usable_only and not spec.usable:
            continue
        out.append(spec)
    return out


def models_for(capability: Capability) -> list[ModelSpec]:
    """某能力下用户可选的模型（稳定排序：默认模型排第一）。"""
    default_id = DEFAULT_MODELS.get(capability)
    items = list_models(capability=capability, selectable_only=True)
    items.sort(key=lambda s: (s.id != default_id, s.id))
    return items


def resolve_model_id(model_id: str | None, capability: Capability) -> str | None:
    """把 ``auto`` / 空值解析成该能力的具体模型；未知模型返回 None。"""
    default_id = DEFAULT_MODELS.get(capability)
    candidate = (model_id or "").strip()
    if not candidate or candidate == AUTO_ALIAS:
        return default_id
    spec = MODEL_REGISTRY.get(candidate)
    if spec is None:
        return None
    return candidate


def mark_verified(model_id: str, *, via: str = "", verified: bool = True) -> bool:
    spec = MODEL_REGISTRY.get(model_id)
    if spec is None:
        return False
    spec.verified = verified
    if via:
        spec.verified_via = via
    return True


def set_provider_configured(configured: bool, *, provider: str = PROVIDER_BAILIAN) -> None:
    for spec in MODEL_REGISTRY.values():
        if spec.provider == provider:
            spec.configured = configured


def set_model_available(model_id: str, available: bool) -> bool:
    spec = MODEL_REGISTRY.get(model_id)
    if spec is None:
        return False
    spec.available = available
    return True


def sync_available_from_plan(plan_model_ids: list[str]) -> dict[str, bool]:
    """用百炼 ``/models`` 的真实返回校准 ``available``。

    目录里存在、但套餐列表里没有的模型（例如 happyhorse-*）会被标成不可用，
    这样调用前就能给出明确错误，而不是让用户等到 404。
    """
    known = set(plan_model_ids)
    changed: dict[str, bool] = {}
    for spec in MODEL_REGISTRY.values():
        if spec.provider != PROVIDER_BAILIAN or spec.protocol == PROTOCOL_LOCAL_TOOL:
            continue
        if not spec.implemented:
            continue
        should = spec.id in known
        if spec.available != should:
            spec.available = should
            changed[spec.id] = should
    return changed


def registry_snapshot() -> dict[str, object]:
    """完整目录快照（含 aliases），供后端 API / 开发者模式使用。"""
    return {
        "alias": {AUTO_ALIAS: {c.value: DEFAULT_MODELS.get(c) for c in Capability}},
        "models": [spec.public_dict() for spec in MODEL_REGISTRY.values()],
    }


def env_has_bailian_key() -> bool:
    """服务端是否至少有一种方式拿到百炼 Key（不返回 Key 本身）。"""
    for name in ("ALIYUN_BAILIAN_API_KEY", "DASHSCOPE_API_KEY"):
        if os.environ.get(name):
            return True
    return False
