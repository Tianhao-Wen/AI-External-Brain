"""AI 外脑 Demo 多模态能力层（Phase 1：后端接入与路由）。

模块划分：

* ``result``    —— 统一的业务结果结构与错误码（不把多模态输出强行转成纯文本）
* ``catalog``   —— ModelRegistry：所有模型的静态能力目录（注册 / 可实现 / 已验证 三态）
* ``dashscope`` —— 百炼 Token Plan 的 HTTP 客户端（统一鉴权、超时、错误翻译、脱敏）
* ``router``    —— CapabilityRouter：把一次用户请求路由到某个能力 + 模型
* ``services``  —— 图片 / 视频 / 语音 三个生成服务 + 实时语音连接探测

设计约束（与 Phase 1 要求一致）：
* 不改 Agent Loop，不碰 ``client.py`` / ``agent.py`` 的请求协议；
* 生成结果统一走现有 ``ArtifactStore``，不另建文件管理；
* Key 只在服务端读取，任何对外结构都不带 Key 或带签名的临时 URL；
* 没走过的调用一律标 ``verified=False``，绝不把"注册了"当成"能用"。
"""

from __future__ import annotations

from aieb.models.catalog import (
    AUTO_ALIAS,
    Capability,
    ModelSpec,
    get_model,
    list_models,
    mark_verified,
    models_for,
    resolve_model_id,
    set_provider_configured,
)
from aieb.models.result import CapabilityResult, ErrorCode, ResultStatus
from aieb.models.router import CapabilityRouter, RouteDecision
from aieb.models.services import (
    AudioService,
    ImageGenerationService,
    MediaServices,
    RealtimeVoiceClient,
    VideoGenerationService,
)

__all__ = [
    "AUTO_ALIAS",
    "AudioService",
    "Capability",
    "CapabilityResult",
    "CapabilityRouter",
    "ErrorCode",
    "ImageGenerationService",
    "MediaServices",
    "ModelSpec",
    "RealtimeVoiceClient",
    "ResultStatus",
    "RouteDecision",
    "VideoGenerationService",
    "get_model",
    "list_models",
    "mark_verified",
    "models_for",
    "resolve_model_id",
    "set_provider_configured",
]
