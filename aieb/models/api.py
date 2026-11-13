"""给下一阶段「Web 功能栏」用的统一后端接口。

下一阶段的 UI 只需要两件事：
1. ``describe()`` 拿能力清单（哪些能力现在真的能用、默认模型是什么）；
2. ``handle(request)`` 发一次请求，拿统一的 ``CapabilityResult`` 字典。

界面不需要知道百炼的端点、参数、异步轮询细节，也不需要碰 API Key。
所有阻塞 HTTP 都跑在线程里（``asyncio.to_thread``），不会卡住 WebSocket 循环。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from aieb.artifacts import ArtifactStore
from aieb.models.catalog import (
    Capability,
    registry_snapshot,
    resolve_model_id,
)
from aieb.models.result import CapabilityResult, ErrorCode, ResultStatus
from aieb.models.router import CapabilityRouter, RouteDecision
from aieb.models.services import MediaServices

#: 不需要模型、已有本地实现的能力：由 Agent 侧的工具处理
AGENT_SIDE_CAPABILITIES = frozenset({
    Capability.CHAT,
    Capability.WRITING,
    Capability.VISION_UNDERSTANDING,
    Capability.WEB_SEARCH,
    Capability.WEB_FETCH,
})


@dataclass
class MediaRequest:
    """一次多模态请求（字段都是可选的，按能力取用）。"""

    prompt: str = ""
    capability: Capability | str | None = None
    model: str | None = None
    #: 语音合成用的文本
    text: str = ""
    #: 语音识别用的公网音频地址
    audio_url: str = ""
    #: 图生图 / 图生视频的参考图地址
    reference_image: str = ""
    voice: str = ""
    size: str = ""
    count: int = 1
    #: 产物归属的轮次（Artifact 卡片按轮次挂载）
    turn: int = 0
    #: 附件描述（用于路由判定）：[{"kind": "image", "name": "a.png"}]
    attachments: list[dict[str, Any]] = field(default_factory=list)
    developer: bool = False


class MediaAPI:
    """统一入口。一个会话一个实例（复用同一个 ArtifactStore）。"""

    def __init__(
        self,
        providers: list[Any] | None,
        *,
        artifact_store: ArtifactStore | None = None,
        work_dir: Path | str | None = None,
    ) -> None:
        self.services = MediaServices.create(
            providers,
            artifact_store=artifact_store,
            work_dir=Path(work_dir) if work_dir else None,
        )
        self.router = CapabilityRouter()

    # -- 描述 -------------------------------------------------------------
    def describe(self) -> dict[str, Any]:
        """功能栏需要的全部信息（不含任何凭据/地址）。"""
        return {
            "configured": self.services.client.configured,
            "capabilities": self.services.capabilities(),
            "catalog": registry_snapshot(),
        }

    def route(self, request: MediaRequest) -> RouteDecision:
        return self.router.route(
            request.prompt or request.text,
            attachments=request.attachments,
            capability=request.capability,
            model=request.model,
        )

    # -- 执行 -------------------------------------------------------------
    async def handle(self, request: MediaRequest) -> dict[str, Any]:
        decision = self.route(request)
        result = await self._dispatch(request, decision)
        return result.to_dict(developer=request.developer)

    async def _dispatch(self, request: MediaRequest, decision: RouteDecision) -> CapabilityResult:
        capability = decision.capability
        model = decision.model if not decision.model_mismatch else request.model

        if decision.model_mismatch:
            return CapabilityResult(
                capability=capability.value, model=model or ""
            ).fail(
                ErrorCode.MODEL_NOT_SUPPORTED,
                message=f"{model} 不支持 {capability.value}",
                detail={"routed_capability": capability.value},
            )

        if capability is Capability.IMAGE_GENERATE:
            return await asyncio.to_thread(
                self.services.image.generate,
                request.prompt or request.text,
                model=model,
                reference_image=request.reference_image,
                turn=request.turn,
                count=request.count,
            )

        if capability is Capability.VIDEO_GENERATE:
            submitted = await asyncio.to_thread(
                self.services.video.submit,
                request.prompt or request.text,
                model=model,
                image=request.reference_image,
                size=request.size or "1280*720",
            )
            if submitted.status is not ResultStatus.PENDING:
                return submitted
            # 提交成功后异步等待；超时由服务内部的上限控制，不会无限阻塞
            return await self.services.video.wait(
                submitted.task_id, model=submitted.model, turn=request.turn
            )

        if capability is Capability.AUDIO_SPEECH:
            return await asyncio.to_thread(
                self.services.audio.synthesize,
                request.text or request.prompt,
                model=model,
                voice=request.voice,
                turn=request.turn,
            )

        if capability is Capability.AUDIO_TRANSCRIBE:
            return await asyncio.to_thread(
                self.services.audio.transcribe,
                request.audio_url,
                model=model,
                turn=request.turn,
            )

        if capability is Capability.REALTIME_VOICE:
            return await self.services.realtime.probe()

        # Agent 侧能力：交给现有 chat / 工具链路，这里只说明"应该走哪条路"
        if capability in AGENT_SIDE_CAPABILITIES:
            return CapabilityResult(
                capability=capability.value,
                model=model or "",
                status=ResultStatus.SUCCESS,
                result={
                    "handled_by": "agent",
                    "note": "这类请求继续走现有 Agent 链路（对话 / 工具调用）",
                    "suggested_capability": capability.value,
                },
            )

        # 其余能力：注册了但没有适配器
        spec_model = resolve_model_id(model, capability) or ""
        return CapabilityResult(capability=capability.value, model=spec_model).fail(
            ErrorCode.NOT_IMPLEMENTED,
            message=f"{capability.value} 还没有可用的实现",
        )


def create_media_api(
    providers: list[Any] | None,
    *,
    artifact_store: ArtifactStore | None = None,
    work_dir: Path | str | None = None,
) -> MediaAPI:
    return MediaAPI(providers, artifact_store=artifact_store, work_dir=work_dir)
