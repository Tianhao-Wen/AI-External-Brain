"""把 Phase 1 的 MediaAPI 接到 Web 会话上（WebSocket 任务宿主）。

职责边界：

* 校验：能力、模型、参数都必须在服务端判定（前端传什么都不算数）；
* 执行：生成类任务在后台跑，不阻塞 WebSocket 读循环；
* 播报：按真实状态发 ``capability_started / progress / result`` 事件；
* 持久化：任务记录落盘，刷新页面/断开重连后能接着看，而且**不会重新提交**
  （视频任务只认云端 task_id，重复提交就是重复计费）；
* 恢复：重连时把未完成任务重新挂上轮询，把最近完成的任务再播一遍。

集中在这里而不是散在 ``remote.py`` 里，是为了让 Web 层只做"收消息→转交"。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from aieb.artifacts import ArtifactStore
from aieb.models.catalog import Capability, get_model
from aieb.models.result import CapabilityResult, ErrorCode, ResultStatus, redact
from aieb.models.services import MediaServices

#: 生成类能力（走 MediaAPI，产出文件）
GENERATIVE = frozenset({
    Capability.IMAGE_GENERATE,
    Capability.VIDEO_GENERATE,
    Capability.AUDIO_SPEECH,
    Capability.AUDIO_TRANSCRIBE,
    Capability.REALTIME_VOICE,
})

#: 交给现有对话/工具链处理的能力
AGENT_SIDE = frozenset({
    Capability.CHAT,
    Capability.WRITING,
    Capability.VISION_UNDERSTANDING,
    Capability.WEB_SEARCH,
    Capability.WEB_FETCH,
})

#: 已知"注册了但真实不可用"的能力：即便 usable=True 也要在 UI 里置灰，
#: 避免按钮可点但必然报错（Phase 1 实测结论）。
KNOWN_UNAVAILABLE: dict[str, str] = {
    Capability.AUDIO_TRANSCRIBE.value: (
        "语音转写尚未打通：当前套餐对 qwen-audio-3.0-asr-flash 的调用被拒绝（403/400）"
    ),
    Capability.REALTIME_VOICE.value: "实时语音仅完成连接验证，通话界面尚未开发",
    Capability.IMAGE_SEARCH.value: "以图搜图还没有接入适配器",
    Capability.TEXT_IMAGE_SEARCH.value: "文搜图还没有接入适配器",
    Capability.CODE_INTERPRETER.value: "云端代码解释器还没有接入",
}

#: 能力的中文名（UI 用；普通用户看不到内部 id）
CAPABILITY_LABELS: dict[str, str] = {
    Capability.CHAT.value: "对话",
    Capability.WRITING.value: "帮我写作",
    Capability.IMAGE_GENERATE.value: "图片生成",
    Capability.VIDEO_GENERATE.value: "视频生成",
    Capability.AUDIO_SPEECH.value: "语音生成",
    Capability.AUDIO_TRANSCRIBE.value: "录音转写",
    Capability.REALTIME_VOICE.value: "实时语音",
    Capability.VISION_UNDERSTANDING.value: "看图理解",
    Capability.WEB_SEARCH.value: "联网搜索",
    Capability.WEB_FETCH.value: "网页读取",
    Capability.IMAGE_SEARCH.value: "以图搜图",
    Capability.TEXT_IMAGE_SEARCH.value: "文搜图",
    Capability.CODE_INTERPRETER.value: "代码解释器",
}

#: 输入框提示语（按能力切换）
CAPABILITY_PLACEHOLDERS: dict[str, str] = {
    Capability.CHAT.value: "输入消息，AI 外脑会帮你完成任务…",
    Capability.WRITING.value: "告诉我你想写什么内容（邮件 / 文章 / 简历 / 改写）…",
    Capability.IMAGE_GENERATE.value: "描述你想生成的图片…",
    Capability.VIDEO_GENERATE.value: "描述你想生成的视频…",
    Capability.AUDIO_SPEECH.value: "输入要朗读的文字…",
    Capability.WEB_SEARCH.value: "想查什么？例如“今天有什么 AI 新闻”",
    Capability.WEB_FETCH.value: "贴一个网址，我来读正文…",
    Capability.VISION_UNDERSTANDING.value: "上传图片并告诉我你想了解什么…",
}

#: 能力 → 默认模型（下发给前端做展示，前端不能改）
MAX_RESTORE_TASKS = 20

log = logging.getLogger(__name__)


@dataclass
class CapabilityTask:
    """一次能力调用的持久化记录（不含任何凭据或签名地址）。"""

    id: str
    capability: str
    model: str
    prompt: str
    turn: int
    status: str = "submitting"   # submitting|queued|running|downloading|success|failed|timeout|cancelled
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    provider_task_id: str = ""
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    error_code: str = ""
    error_message: str = ""
    detail: dict[str, Any] = field(default_factory=dict)
    restore_note: str = ""
    #: 前端生成的一次性请求标识（防重复提交 & 把 started 事件绑回本地卡片）
    request_id: str = ""
    #: 停止的结果：before_send（未发出）/ remote_cancelled（云端已撤销）/ local_only（已提交无法撤销）
    cancel_mode: str = ""

    @property
    def in_progress(self) -> bool:
        return self.status in ("submitting", "queued", "running", "downloading")

    @property
    def resumable(self) -> bool:
        """能否接着查云端任务：超时也算（任务可能还在云端跑）。"""
        return bool(self.provider_task_id) and self.status in (
            "submitting", "queued", "running", "downloading", "timeout"
        )

    def public(self) -> dict[str, Any]:
        return {
            "task_id": self.id,
            "capability": self.capability,
            "model": self.model,
            "prompt": self.prompt,
            "turn": self.turn,
            "status": self.status,
            "created_at": self.created_at,
            "provider_task_id": self.provider_task_id,
            "artifacts": self.artifacts,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "detail": self.detail,
            "in_progress": self.in_progress,
            "resumable": self.resumable,
            "cancel_mode": self.cancel_mode,
        }


class CapabilityHost:
    """一个会话一个实例；由 RemoteServer 持有。"""

    def __init__(
        self,
        *,
        providers: list[Any] | None,
        artifact_store: ArtifactStore,
        broadcast: Callable[[dict[str, Any]], Awaitable[None]],
        state_path: Path,
        work_dir: Path,
        artifact_payload: Callable[[list[dict[str, Any]]], list[dict[str, Any]]],
        provider_probe: Callable[[], tuple[bool, str]] | None = None,
    ) -> None:
        self._services = MediaServices.create(
            providers, artifact_store=artifact_store, work_dir=work_dir
        )
        self._broadcast = broadcast
        self._state_path = state_path
        self._artifact_payload = artifact_payload
        #: 返回 (当前模型是否具备生成能力, 当前模型显示名)；用于把能力栏
        #: 与用户选中的对话模型绑在一起（DeepSeek 不应该能出图）。
        self._provider_probe = provider_probe or (lambda: (True, ""))
        self._tasks: dict[str, CapabilityTask] = {}
        self._running: dict[str, asyncio.Task[None]] = {}
        self._seen_requests: dict[str, float] = {}
        self._turn_base = 100_000
        self._load_state()

    # -- 当前模型 -----------------------------------------------------------
    def media_provider_state(self) -> tuple[bool, str]:
        try:
            capable, label = self._provider_probe()
        except Exception:  # noqa: BLE001 - 探测失败就当作不可用，宁严不宽
            return False, ""
        return bool(capable), str(label or "")

    def _media_blocked_reason(self) -> str:
        capable, label = self.media_provider_state()
        if capable:
            return ""
        who = f"「{label}」" if label else "当前对话模型"
        return (
            f"{who}是纯对话模型，不具备生成图片/视频/语音的能力；"
            "在右上角切换到「千问 Qwen3.8-Max」后即可使用。"
        )

    # -- 状态 -------------------------------------------------------------
    @property
    def services(self) -> MediaServices:
        return self._services

    def describe(self) -> dict[str, Any]:
        """功能栏需要的能力清单（服务端是唯一事实来源）。"""
        info = self._services.capabilities()
        items: list[dict[str, Any]] = []
        for item in info:
            capability = str(item["capability"])
            model_id = str(item["default_model"] or "")
            spec = get_model(model_id)
            reason = ""
            enabled = bool(item["usable"])
            media_capable, provider_label = self.media_provider_state()
            if capability in AGENT_SIDE:
                # 对话类能力永远可用：走的是现有 Agent 链路
                enabled = True
            elif capability in KNOWN_UNAVAILABLE:
                enabled = False
                reason = KNOWN_UNAVAILABLE[capability]
            elif not item["implemented"]:
                enabled = False
                reason = "该能力还没有接入"
            elif not media_capable:
                # 关键：DeepSeek 这类纯对话模型不能借百炼偷偷出图
                enabled = False
                reason = self._media_blocked_reason()
            elif not item["configured"]:
                enabled = False
                reason = "服务端尚未配置对应的模型凭据"
            elif not item["usable"]:
                enabled = False
                reason = "当前套餐不包含这个能力所需的模型"
            items.append({
                "capability": capability,
                "label": CAPABILITY_LABELS.get(capability, capability),
                "placeholder": CAPABILITY_PLACEHOLDERS.get(capability, ""),
                "default_model": model_id,
                "models": [
                    {"id": m.id, "label": m.display_name or m.id}
                    for m in (
                        sorted(
                            [s for s in _models_for(capability) if s.user_selectable and s.available],
                            key=lambda s: (s.id != model_id, s.id),
                        )
                    )
                ],
                "implemented": item["implemented"],
                "verified": item["verified"],
                "enabled": enabled,
                "reason": reason,
                "requires_prompt": capability != Capability.AUDIO_TRANSCRIBE,
                "agent_side": capability in AGENT_SIDE,
                "protocol": spec.protocol if spec else "",
            })
        return {
            "capabilities": items,
            "verified_models": [
                {"id": m.id, "label": m.display_name or m.id, "verified": m.verified}
                for m in _all_models()
                if m.verified
            ],
            "configured": self._services.client.configured,
            "max_video_wait_seconds": int(self._services.video.max_wait),
            # 前端据此显示"当前模型（能/不能生成）"
            "active_provider": {
                "media_capable": self.media_provider_state()[0],
                "label": self.media_provider_state()[1],
                "hint": self._media_blocked_reason(),
            },
        }

    # -- 提交 -------------------------------------------------------------
    async def start(self, payload: dict[str, Any]) -> dict[str, Any]:
        """处理一次 capability_request。返回 ack（真正的结果在后面广播）。"""
        request_id = str(payload.get("request_id") or "").strip()
        capability_raw = str(payload.get("capability") or "").strip()
        prompt = str(payload.get("prompt") or "").strip()
        model = str(payload.get("model") or "").strip()
        voice = str(payload.get("voice") or "").strip()
        size = str(payload.get("size") or "").strip()
        audio_url = str(payload.get("audio_url") or "").strip()

        if not request_id:
            return self._ack_error("invalid_argument", "缺少请求标识")
        # 重复提交（连续点击 / 前端重发）：同一次请求只跑一次
        if request_id in self._seen_requests:
            return {"ok": False, "deduplicated": True, "request_id": request_id}
        self._prune_requests()

        try:
            capability = Capability(capability_raw)
        except ValueError:
            return self._ack_error("model_not_supported", "未知的功能类型")

        if capability in AGENT_SIDE:
            return self._ack_error(
                "invalid_argument", "这类请求走对话链路，不需要能力任务", extra={"agent_side": True}
            )

        # 服务端校验模型：只接受目录里存在、允许用户选、且属于该能力的模型
        default_id = _default_model(capability)
        resolved = model or default_id
        spec = get_model(resolved)
        if spec is None:
            return self._ack_error("model_not_found", "没有这个模型")
        if not spec.supports(capability):
            return self._ack_error("model_not_supported", "这个模型不支持所选功能")
        if not spec.user_selectable and resolved != default_id:
            return self._ack_error("model_not_supported", "这个模型未对用户开放")
        if capability.value in KNOWN_UNAVAILABLE:
            return self._ack_error("model_not_supported", KNOWN_UNAVAILABLE[capability.value])

        if capability in (Capability.IMAGE_GENERATE, Capability.VIDEO_GENERATE,
                          Capability.AUDIO_SPEECH) and not prompt:
            return self._ack_error("invalid_argument", "请先描述你想要的内容")
        if capability is Capability.AUDIO_TRANSCRIBE and not audio_url:
            return self._ack_error("invalid_argument", "需要音频链接")
        # 服务端兜底：当前对话模型不能生成，就绝不拿别的模型代替
        if capability in GENERATIVE and not self.media_provider_state()[0]:
            return self._ack_error("model_not_supported", self._media_blocked_reason())

        task = CapabilityTask(
            id="cap_" + uuid.uuid4().hex[:12],
            capability=capability.value,
            model=resolved,
            prompt=prompt,
            turn=self._next_turn(),
            request_id=request_id,
        )
        self._seen_requests[request_id] = time.time()
        self._tasks[task.id] = task
        self._save_state()
        await self._emit_started(task)

        runner = asyncio.create_task(
            self._run(task, capability, voice=voice, size=size, audio_url=audio_url)
        )
        self._running[task.id] = runner
        runner.add_done_callback(lambda _t, tid=task.id: self._running.pop(tid, None))
        return {"ok": True, "task_id": task.id, "request_id": request_id,
                "capability": task.capability, "model": task.model, "status": task.status}

    # -- 执行 -------------------------------------------------------------
    async def _run(
        self,
        task: CapabilityTask,
        capability: Capability,
        *,
        voice: str,
        size: str,
        audio_url: str,
    ) -> None:
        try:
            report = self._stage_reporter(task)
            # 用户在请求发出前就点了停止：这里直接放弃，一分钱不花
            if task.status == "cancelled":
                if not task.cancel_mode or task.cancel_mode == "local_only":
                    task.cancel_mode = "before_send"
                    task.error_message = "已停止，这次请求还没有发出，没有产生费用。"
                    self._save_state()
                    await self._emit_cancelled(task)
                return
            if capability is Capability.IMAGE_GENERATE:
                if not await self._begin_stage(task):
                    return
                result = await asyncio.to_thread(
                    self._services.image.generate,
                    task.prompt,
                    model=task.model,
                    turn=task.turn,
                    progress=report,
                )
                await self._finish(task, result)

            elif capability is Capability.AUDIO_SPEECH:
                if not await self._begin_stage(task):
                    return
                result = await asyncio.to_thread(
                    self._services.audio.synthesize,
                    task.prompt,
                    model=task.model,
                    voice=voice,
                    turn=task.turn,
                    progress=report,
                )
                await self._finish(task, result)

            elif capability is Capability.VIDEO_GENERATE:
                await self._run_video(task, size=size)

            elif capability is Capability.AUDIO_TRANSCRIBE:
                if not await self._begin_stage(task):
                    return
                result = await asyncio.to_thread(
                    self._services.audio.transcribe,
                    audio_url,
                    model=task.model,
                    turn=task.turn,
                )
                await self._finish(task, result)

            elif capability is Capability.REALTIME_VOICE:
                result = await self._services.realtime.probe()
                await self._finish(task, result)

            else:
                result = CapabilityResult(capability=task.capability, model=task.model)
                result.fail(ErrorCode.NOT_IMPLEMENTED, message="该功能还没有实现")
                await self._finish(task, result)
        except asyncio.CancelledError:  # pragma: no cover - 用户停止或服务关闭
            if task.status != "cancelled":
                task.status = "cancelled"
            self._save_state()
            raise
        except Exception as exc:  # noqa: BLE001 - 兜底：任何异常都要变成可见错误
            task.status = "failed"
            task.error_code = ErrorCode.PROVIDER_ERROR.value
            task.error_message = "生成过程出错，请稍后重试"
            task.detail["exception"] = f"{type(exc).__name__}: {exc}"
            task.updated_at = time.time()
            self._save_state()
            await self._emit_result(task)

    async def _begin_stage(self, task: CapabilityTask) -> bool:
        """进入生成阶段。返回 False 表示用户在这之前已停止（不发请求、不花钱）。"""
        await self._set_status(task, "running")
        if task.status == "cancelled":
            await self._abandon_before_send(task)
            return False
        return True

    async def _abandon_before_send(self, task: CapabilityTask) -> None:
        if task.cancel_mode != "remote_cancelled":
            task.cancel_mode = "before_send"
            task.error_message = "已停止，这次请求还没有发出，没有产生费用。"
            self._save_state()
            await self._emit_cancelled(task)

    def _stage_reporter(self, task: CapabilityTask) -> Any:
        """生成服务在**工作线程**里运行，阶段变化要抛回事件循环才能广播。"""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # pragma: no cover - 不在事件循环里（不应发生）
            return None
        allowed = ("queued", "running", "downloading")

        def report(stage: str) -> None:
            if stage not in allowed or task.status == stage:
                return
            try:
                asyncio.run_coroutine_threadsafe(self._set_status(task, stage), loop)
            except RuntimeError:  # pragma: no cover - 循环已关闭
                pass

        return report

    async def _run_video(self, task: CapabilityTask, *, size: str) -> None:
        """视频：提交 → 立即播报排队 → 轮询播报真实状态 → 完成。

        关键点：
        * 提交成功后马上广播（用户 1 秒内就能看到卡片，不用等三五分钟）；
        * 只播报云端真实状态，不编造百分比；
        * 云端 task_id 落盘，断线重连后接着轮询老任务，绝不重新提交。
        """
        if task.status == "cancelled":
            await self._abandon_before_send(task)
            return
        submitted = await asyncio.to_thread(
            self._services.video.submit, task.prompt, model=task.model, size=size or "1280*720"
        )
        if submitted.status is ResultStatus.PENDING:
            task.provider_task_id = submitted.task_id
            task.detail["video_size"] = size or "1280*720"
            await self._set_status(task, "queued")
            await self._poll_video(task, submitted.task_id)
            return
        await self._finish(task, submitted)

    async def _poll_video(self, task: CapabilityTask, provider_task_id: str) -> None:
        service = self._services.video
        interval = service.poll_interval
        deadline = time.monotonic() + service.max_wait
        last = ""
        while True:
            if task.cancel_mode == "remote_cancelled":
                return   # 云端任务已被撤销，不必再查
            snapshot = await asyncio.to_thread(service.poll, provider_task_id)
            status = snapshot.status
            if status != last:
                last = status
                task.detail["provider_status"] = status
                if status in ("PENDING", "QUEUED"):
                    await self._set_status(task, "queued")
                elif status in ("RUNNING", "PROCESSING"):
                    await self._set_status(task, "running")
            if status in ("SUCCEEDED", "FAILED", "CANCELED", "UNKNOWN", "ERROR"):
                if task.cancel_mode == "remote_cancelled":
                    return
                if status == "SUCCEEDED" and snapshot.video_url:
                    result = await asyncio.to_thread(
                        self._save_video, task, snapshot.video_url, self._stage_reporter(task)
                    )
                else:
                    result = CapabilityResult(
                        capability=task.capability, model=task.model, task_id=provider_task_id
                    )
                    result.fail(
                        ErrorCode.TASK_FAILED,
                        message="视频生成任务失败",
                        detail={"provider_status": status,
                                "provider_message": snapshot.provider_message},
                    )
                await self._finish(task, result, provider_task_id=provider_task_id)
                return
            if time.monotonic() >= deadline:
                result = CapabilityResult(
                    capability=task.capability, model=task.model, task_id=provider_task_id
                )
                result.fail(
                    ErrorCode.TASK_TIMEOUT,
                    message="视频还在云端生成，等待超时；可以在本卡片上点「继续查询」",
                    detail={"provider_status": status},
                )
                if task.status != "cancelled":
                    task.status = "timeout"
                await self._finish(task, result, provider_task_id=provider_task_id, keep_pending=True)
                return
            await asyncio.sleep(interval)

    def _save_video(
        self, task: CapabilityTask, url: str, progress: Any = None
    ) -> CapabilityResult:
        service = self._services.video
        result = CapabilityResult(capability=task.capability, model=task.model,
                                  task_id=task.provider_task_id)
        name = f"{task.model}_{uuid.uuid4().hex[:8]}.mp4"
        entry = service._save(  # noqa: SLF001 - 复用同一套下载 + Artifact 登记
            result, url, filename=name, turn=task.turn, kind="video", progress=progress
        )
        if entry:
            result.status = ResultStatus.SUCCESS
            result.result = {"video": entry, "task_id": task.provider_task_id}
        else:
            result.fail(ErrorCode.NETWORK_ERROR, message="视频已生成，但下载失败")
        return result

    # -- 恢复 -------------------------------------------------------------
    async def restore(self) -> None:
        """新连接（刷新页面）时把任务补发一遍；未完成的继续轮询/如实报中断。"""
        if not self._tasks:
            return
        tasks = sorted(self._tasks.values(), key=lambda t: t.created_at)
        for task in tasks[-MAX_RESTORE_TASKS:]:
            if task.id not in self._running and (task.in_progress or task.resumable):
                # 进程重启过：同步调用早就没了，视频还能拿云端 task_id 接着查；
                # 其它任务只能如实告诉用户"被打断了"，而不是永远转圈。
                if task.capability == Capability.VIDEO_GENERATE.value and task.resumable:
                    runner = asyncio.create_task(
                        self._resume_video(task, task.provider_task_id)
                    )
                    self._running[task.id] = runner
                    runner.add_done_callback(lambda _t, tid=task.id: self._running.pop(tid, None))
                elif task.in_progress:
                    self._mark_interrupted(task)
            await self._emit_restored(task)

    def _mark_interrupted(self, task: CapabilityTask) -> None:
        task.status = "failed"
        task.error_code = ErrorCode.TASK_FAILED.value
        task.error_message = "任务在生成过程中被中断（服务重启或连接断开），请重新提交"
        task.updated_at = time.time()
        self._save_state()

    async def _resume_video(self, task: CapabilityTask, provider_task_id: str) -> None:
        task.restore_note = "已从上次的任务继续查询（不会重新提交）"
        self._save_state()
        await self._poll_video(task, provider_task_id)

    async def cancel(self, task_id: str) -> bool:
        """用户点「停止」：能撤销就撤销，撤销不了就把已经付费的结果接住。

        三种真实结果（不粉饰）：
        * ``before_send``：请求还没发出去就取消了——完全没产生费用；
        * ``remote_cancelled``：视频还在云端排队，已调取消接口搮销；
        * ``salvaging``：请求已发出/云端已在生成，**没法撤销**。
          但既然这份钱已经花了，后台就**继续收结果**：回来了就挂在卡片上
          （``cancel_mode`` 变成 ``salvaged``），用户不会"付了钱却什么都拿不到"。
        """
        task = self._tasks.get(task_id)
        if task is None or not task.in_progress:
            return False

        mode = "salvaging"
        note = "已停止等待。这次请求已经提交给模型服务，无法撤销；如果云端完成了，结果会保留在这张卡片上。"
        runner = None

        if task.capability == Capability.VIDEO_GENERATE.value and task.provider_task_id:
            ok, detail = await asyncio.to_thread(
                self._services.video.cancel, task.provider_task_id
            )
            task.detail["cancel_result"] = detail
            if ok:
                mode = "remote_cancelled"
                note = "已停止，云端任务已撤销，不会再产生生成费用。"
                runner = self._running.pop(task_id, None)
        elif task.status == "submitting" and not task.provider_task_id:
            mode = "before_send"
            note = "已停止，这次请求还没有发出，没有产生费用。"
            runner = self._running.pop(task_id, None)

        task.status = "cancelled"
        task.cancel_mode = mode
        task.updated_at = time.time()
        task.error_code = ""
        task.error_message = note
        if runner is not None:
            runner.cancel()
        self._save_state()
        await self._emit_cancelled(task)
        return True

    async def _emit_cancelled(self, task: CapabilityTask, *, salvaged: bool = False) -> None:
        await self._safe_broadcast({
            "type": "capability_result",
            "data": {
                **task.public(),
                "artifacts": self._artifact_payload(task.artifacts),
                "restored": False,
                "cancelled_by_user": True,
                "result_salvaged": salvaged,
            },
        })

    async def resume(self, task_id: str) -> bool:
        task = self._tasks.get(task_id)
        if task is None or not task.resumable:
            return False
        if task.capability != Capability.VIDEO_GENERATE.value:
            return False
        if task.id in self._running:
            return True
        runner = asyncio.create_task(self._resume_video(task, task.provider_task_id))
        self._running[task.id] = runner
        runner.add_done_callback(lambda _t, tid=task.id: self._running.pop(tid, None))
        return True

    def pending_video_tasks(self) -> list[dict[str, Any]]:
        return [t.public() for t in self._tasks.values() if t.in_progress]

    def cleanup(self) -> None:
        """会话切换/删除时取消所有后台轮询（云端任务本身不受影响）。"""
        for runner in list(self._running.values()):
            runner.cancel()
        self._running.clear()

    # -- 事件 -------------------------------------------------------------
    async def _emit_started(self, task: CapabilityTask) -> None:
        await self._safe_broadcast({
            "type": "capability_started",
            "data": {
                "task_id": task.id,
                "request_id": task.request_id,
                "capability": task.capability,
                "model": task.model,
                "prompt": task.prompt,
                "turn": task.turn,
                "status": task.status,
            },
        })

    async def _emit_restored(self, task: CapabilityTask) -> None:
        # 产物要重新换一次签名下载地址：记录里存的只有 artifact id，
        # 否则刷新后卡片能恢复但图片/音频/视频全都渲染不出来。
        await self._safe_broadcast({
            "type": "capability_result",
            "data": {
                **task.public(),
                "artifacts": self._artifact_payload(task.artifacts),
                "restored": True,
            },
        })

    async def _set_status(self, task: CapabilityTask, status: str) -> None:
        if task.status == "cancelled":
            return   # 用户已停止：线程里迟到的阶段上报一律忽略
        if task.status == status:
            return
        task.status = status
        task.updated_at = time.time()
        self._save_state()
        await self._safe_broadcast({
            "type": "capability_progress",
            "data": {
                "task_id": task.id,
                "capability": task.capability,
                "status": status,
                "elapsed": round(time.time() - task.created_at, 1),
                "provider_task_id": task.provider_task_id,
            },
        })

    async def _finish(
        self,
        task: CapabilityTask,
        result: CapabilityResult,
        *,
        provider_task_id: str = "",
        keep_pending: bool = False,
    ) -> None:
        if provider_task_id:
            task.provider_task_id = provider_task_id
        # 用户已经点了停止，但这次请求是撤销不了的（钱已经花了）：
        # 结果回来了就接住——把产物挂在卡片上，别让用户白付一次钱。
        salvaging = task.status == "cancelled"
        if salvaging:
            if result.status is ResultStatus.SUCCESS:
                task.artifacts = [dict(a) for a in result.artifacts]
                task.cancel_mode = "salvaged"
                task.error_message = (
                    "已停止等待；云端已经生成完成，结果已保留在上面（这次已经计费）。"
                )
                task.error_code = ""
            else:
                task.cancel_mode = "salvaged_empty"
                task.error_message = "已停止等待；云端没有返回可用结果。"
            task.detail.update({
                k: v for k, v in (redact(result.detail) or {}).items() if k not in task.detail
            })
            task.updated_at = time.time()
            self._save_state()
            await self._emit_cancelled(
                task, salvaged=result.status is ResultStatus.SUCCESS
            )
            return
        if result.status is ResultStatus.SUCCESS:
            task.status = "success"
            task.artifacts = [dict(a) for a in result.artifacts]
            task.error_code = ""
            task.error_message = ""
        else:
            task.status = "timeout" if keep_pending else "failed"
            task.error_code = (result.error_code or ErrorCode.PROVIDER_ERROR).value
            task.error_message = result.user_message()
        # detail 只用于开发者模式，落盘/下发前统一脱敏（会抹掉签名地址与凭据）
        task.detail.update({
            k: v for k, v in (redact(result.detail) or {}).items() if k not in task.detail
        })
        task.updated_at = time.time()
        payload = {
            "task_id": task.id,
            "request_id": task.request_id,
            "capability": task.capability,
            "model": task.model,
            "prompt": task.prompt,
            "turn": task.turn,
            "status": task.status,
            "result": result.result,
            "artifacts": self._artifact_payload(task.artifacts),
            "error_code": task.error_code,
            "error_message": task.error_message,
            "provider_task_id": task.provider_task_id,
            "detail": task.detail,
            "elapsed": round(time.time() - task.created_at, 1),
            "restored": False,
        }
        self._save_state()
        await self._safe_broadcast({"type": "capability_result", "data": payload})

    async def _safe_broadcast(self, payload: dict[str, Any]) -> None:
        try:
            await self._broadcast(payload)
        except Exception:  # noqa: BLE001 - 广播失败不能影响任务本身
            pass

    # -- 持久化 -----------------------------------------------------------
    def _load_state(self) -> None:
        try:
            raw = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        for item in raw.get("tasks", []) if isinstance(raw, dict) else []:
            try:
                task = CapabilityTask(**item)
            except TypeError:
                continue
            self._tasks[task.id] = task
        self._turn_base = max(
            [self._turn_base] + [t.turn for t in self._tasks.values()] + [100_000]
        )

    def _save_state(self) -> None:
        payload = {
            "version": 1,
            "tasks": [asdict(task) for task in self._tasks.values()],
        }
        text = json.dumps(payload, ensure_ascii=False)
        tmp = self._state_path.with_suffix(".json.tmp")
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, self._state_path)
            return
        except OSError as exc:
            # Windows 上目标文件被杀毒/其它进程瞬时占用时 replace 会失败，
            # 不能就这么丢掉记录，否则界面会永远停在"进行中"。
            log.warning("capability state save (atomic) failed: %s", exc)
        try:
            self._state_path.write_text(text, encoding="utf-8")
        except OSError as exc:
            log.warning("capability state save (fallback) failed: %s", exc)
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass

    def _next_turn(self) -> int:
        """能力任务用独立的高位轮次号。

        为什么不用 ``_turn_no``：聊天回放是按「第几个用户消息」定位产物的，
        能力任务如果共享同一套递增序号，会让历史会话里的文件卡片错位。
        """
        self._turn_base += 1
        return self._turn_base

    def _prune_requests(self) -> None:
        cutoff = time.time() - 600
        for key, stamp in list(self._seen_requests.items()):
            if stamp < cutoff:
                self._seen_requests.pop(key, None)

    @staticmethod
    def _ack_error(code: str, message: str, *, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = {"ok": False, "error_code": code, "error_message": message}
        payload.update(extra or {})
        return payload


def _models_for(capability_value: str) -> list[Any]:
    from aieb.models.catalog import models_for

    try:
        return models_for(Capability(capability_value))
    except ValueError:
        return []


def _all_models() -> list[Any]:
    from aieb.models.catalog import list_models

    return list_models()


def _default_model(capability: Capability) -> str:
    from aieb.models.catalog import DEFAULT_MODELS

    return DEFAULT_MODELS.get(capability, "")
