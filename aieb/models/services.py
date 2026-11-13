"""图片 / 视频 / 语音生成服务。

每个服务都做四件同样的事：

1. **校验**：模型是否支持这个能力、套餐里有没有、凭据是否齐 → 不满足就返回明确错误；
2. **调用**：按官方协议发请求（每个模型可能有自己的适配细节，用分支封装）；
3. **落盘**：把生成结果下载到本地并登记进现有 ``ArtifactStore``；
4. **如实返回**：成功就是成功，失败就是失败，异步任务保留 task_id。

所有产出都只返回 artifact id（前端用现有签名下载接口取文件），
**不把带签名的临时地址**回传给浏览器。
"""

from __future__ import annotations

import asyncio
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from aieb.artifacts import ArtifactStore
from aieb.models.catalog import (
    DEFAULT_MODELS,
    PROTOCOL_ASYNC_TASK,
    PROTOCOL_MULTIMODAL,
    PROTOCOL_TTS,
    Capability,
    get_model,
    resolve_model_id,
    set_provider_configured,
    sync_available_from_plan,
)
from aieb.models.dashscope import DashScopeClient, ProviderResponse, normalize_error
from aieb.models.result import CapabilityResult, ErrorCode, ResultStatus

# ---------------------------------------------------------------------------
# 附件格式约束（来自官方文档 / 实测）
# ---------------------------------------------------------------------------

#: 图片：OpenAI/DashScope 常用的四种
ALLOWED_IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp")
MAX_IMAGE_BYTES = 10 * 1024 * 1024

#: 音频：官方支持 aac/wav/mp3 等主流格式；Flash 模型 ≤10MB / ≤5 分钟
ALLOWED_AUDIO_SUFFIXES = (".wav", ".mp3", ".m4a", ".aac", ".ogg", ".flac", ".amr", ".webm")
MAX_AUDIO_BYTES = 10 * 1024 * 1024
MAX_AUDIO_SECONDS = 300

#: TTS 单次最多 600 字符（官方限制）
MAX_TTS_CHARS = 600

#: 视频默认参数（实测 1280*720 可出片）
DEFAULT_VIDEO_SIZE = "1280*720"
ALLOWED_VIDEO_SIZES = ("1280*720", "960*540", "720*480")

#: 轮询与限额（防止无控制地烧 Credits）
DEFAULT_POLL_INTERVAL = 5.0
DEFAULT_MAX_WAIT = 600.0
DEFAULT_MAX_POLLS = 120


def _default_bailian_base() -> str:
    return "https://dashscope.aliyuncs.com/compatible-mode/v1"


def provider_supports_media(provider: Any | None) -> bool:
    """当前对话 provider 是否属于"能调百炼图像/视频/语音"的那一类。

    为什么要判这个：DeepSeek / GLM 这类对话模型**没有**图片、视频、语音合成能力，
    它们背后也没有百炼的凭据。用户在模型下拉里选了 DeepSeek，就不该能用
    "图片生成"——否则等于背着用户偷偷换成千问去生成，是自相矛盾的。
    """
    if provider is None:
        return False
    base = str(getattr(provider, "base_url", "") or "")
    name = str(getattr(provider, "name", "") or "").lower()
    model = str(getattr(provider, "model", "") or "").lower()
    if "aliyuncs.com" in base or "dashscope" in base:
        return True
    return "qwen" in name or "qwen" in model


def resolve_bailian_client(providers: list[Any] | None, *, timeout: float = 60.0) -> DashScopeClient:
    """从现有 provider 配置里复用百炼的 base_url 与 Key（不要新加一套配置）。

    优先找 ``base_url`` 含 ``aliyuncs.com`` 的 provider，其次是名字里带 qwen 的；
    都找不到时退回环境变量 + 官方默认域名。
    """
    candidates = list(providers or [])
    chosen = None
    for provider in candidates:
        base = str(getattr(provider, "base_url", "") or "")
        if "aliyuncs.com" in base or "dashscope" in base:
            chosen = provider
            break
    if chosen is None:
        for provider in candidates:
            if "qwen" in str(getattr(provider, "name", "")).lower():
                chosen = provider
                break
    if chosen is not None:
        api_key = ""
        try:
            api_key = chosen.resolve_api_key()
        except Exception:  # noqa: BLE001 - 未配置时静默降级
            api_key = ""
        base_url = str(getattr(chosen, "base_url", "") or _default_bailian_base())
        client = DashScopeClient(base_url, api_key, timeout=timeout)
    else:
        client = DashScopeClient(
            os.environ.get("DASHSCOPE_BASE_URL", _default_bailian_base()),
            os.environ.get("ALIYUN_BAILIAN_API_KEY") or os.environ.get("DASHSCOPE_API_KEY", ""),
            timeout=timeout,
        )
    set_provider_configured(client.configured)
    return client


# ---------------------------------------------------------------------------
# 基类
# ---------------------------------------------------------------------------


class _BaseService:
    capability: Capability

    def __init__(
        self,
        client: DashScopeClient,
        *,
        artifact_store: ArtifactStore | None = None,
        work_dir: Path | None = None,
    ) -> None:
        self.client = client
        self.artifact_store = artifact_store
        self.work_dir = Path(work_dir) if work_dir else Path.cwd() / ".aieb" / "media"

    # -- 公共校验 ---------------------------------------------------------
    def _prepare(self, model_id: str | None) -> tuple[CapabilityResult, Any]:
        """统一的"能不能调"检查，返回 (结果外壳, ModelSpec 或 None)。"""
        resolved = resolve_model_id(model_id, self.capability) or ""
        result = CapabilityResult(capability=self.capability.value, model=resolved)
        spec = get_model(resolved)
        if spec is None:
            result.fail(ErrorCode.MODEL_NOT_FOUND, message=f"目录里没有模型 {resolved or '(空)'}")
            return result, None
        if not spec.supports(self.capability):
            result.fail(
                ErrorCode.MODEL_NOT_SUPPORTED,
                message=f"{resolved} 不支持 {self.capability.value}",
                detail={"capabilities": sorted(c.value for c in spec.capabilities)},
            )
            return result, None
        if not spec.implemented:
            result.fail(
                ErrorCode.NOT_IMPLEMENTED,
                message=f"{resolved} 还没有适配器",
                detail={"notes": spec.notes},
            )
            return result, None
        if not spec.available:
            result.fail(
                ErrorCode.MODEL_NOT_SUPPORTED,
                message=f"当前套餐不包含 {resolved}",
                detail={"notes": spec.notes},
            )
            return result, None
        if not self.client.configured:
            result.fail(
                ErrorCode.NOT_CONFIGURED,
                message="服务端缺少百炼 API Key 或 base_url",
                detail={"has_key": self.client.has_key(), "root": self.client.root},
            )
            return result, None
        return result, spec

    def _http_error(self, result: CapabilityResult, response: ProviderResponse) -> CapabilityResult:
        provider_message = response.provider_message()
        code = normalize_error(provider_message) if provider_message else response.error_code()
        if response.status == 403 and "does not support" in provider_message.lower():
            code = ErrorCode.MODEL_NOT_SUPPORTED
        return result.fail(
            code,
            message="",  # 交给 USER_FACING_MESSAGE 生成人话
            detail={
                "http_status": response.status,
                "request_id": response.request_id,
                "provider_message": provider_message,
            },
        )

    # -- 落盘 -------------------------------------------------------------
    def _save(
        self,
        result: CapabilityResult,
        url: str,
        *,
        filename: str,
        turn: int,
        kind: str,
        progress: Callable[[str], None] | None = None,
    ) -> dict[str, Any] | None:
        """下载 + 登记 Artifact；返回给前端的产物描述（不含签名地址）。

        ``progress`` 用于把"正在下载"这个真实阶段报给上层（进度 UI 靠它区分
        "模型还在生成"与"文件正在拉回本地"）。
        """
        if progress is not None:
            try:
                progress("downloading")
            except Exception:  # noqa: BLE001 - 上报失败不能影响主流程
                pass
        dest = self.work_dir / filename
        ok, error = self.client.download(url, dest)
        if not ok:
            result.detail.setdefault("download_errors", []).append(error)
            return None
        artifact = None
        if self.artifact_store is not None:
            try:
                artifact = self.artifact_store.add_file(dest, turn=turn, name=filename)
            except OSError:
                artifact = None
        entry: dict[str, Any] = {
            "kind": kind,
            "name": filename,
            "bytes": dest.stat().st_size if dest.exists() else 0,
        }
        if artifact is not None:
            entry["artifact_id"] = artifact.id
            result.artifacts.append({
                "id": artifact.id,
                "name": artifact.name,
                "kind": artifact.kind,
                "size": artifact.size,
            })
        else:
            entry["artifact_id"] = ""
            result.detail.setdefault("warnings", []).append("结果已生成但未能登记为 Artifact")
        return entry


# ---------------------------------------------------------------------------
# 图片生成
# ---------------------------------------------------------------------------


class ImageGenerationService(_BaseService):
    """文生图 / 图生图（参考图编辑）。

    实测协议（qwen-image-3.0-pro / wan2.7-image 均通过）::

        POST /api/v1/services/aigc/multimodal-generation/generation
        {"model": "...", "input": {"messages": [
            {"role": "user", "content": [{"text": "一只橘猫"}, {"image": "<url>"}]}]}}

    返回 ``output.choices[0].message.content[]``，其中 ``type=image`` 的项带图片地址。
    """

    capability = Capability.IMAGE_GENERATE

    def generate(
        self,
        prompt: str,
        *,
        model: str | None = None,
        reference_image: str = "",
        turn: int = 0,
        count: int = 1,
        progress: Callable[[str], None] | None = None,
    ) -> CapabilityResult:
        result, spec = self._prepare(model)
        if spec is None:
            return result
        prompt = (prompt or "").strip()
        if not prompt:
            return result.fail(ErrorCode.INVALID_ARGUMENT, message="图片描述不能为空")
        if reference_image and "image" not in spec.input_modalities:
            return result.fail(
                ErrorCode.UNSUPPORTED_INPUT,
                message=f"{spec.id} 不支持参考图输入",
                detail={"input_modalities": sorted(spec.input_modalities)},
            )
        count = max(1, min(int(count or 1), 4))

        images: list[dict[str, Any]] = []
        last_error: ProviderResponse | None = None
        for index in range(count):
            content: list[dict[str, Any]] = [{"text": prompt}]
            if reference_image:
                content.append({"image": reference_image})
            response = self.client.post_service(
                "services/aigc/multimodal-generation/generation",
                {"model": spec.id, "input": {"messages": [{"role": "user", "content": content}]}},
                timeout=180.0,
            )
            result.detail.setdefault("request_ids", []).append(response.request_id)
            if not response.ok:
                last_error = response
                break
            urls = _extract_image_urls(response.payload)
            if not urls:
                last_error = response
                result.detail["unexpected_payload"] = response.payload
                break
            for url in urls:
                name = f"{spec.id}_{uuid.uuid4().hex[:8]}_{index + 1}.png"
                entry = self._save(
                    result, url, filename=name, turn=turn, kind="image", progress=progress
                )
                if entry:
                    images.append(entry)

        if not images:
            if last_error is not None and not last_error.ok:
                return self._http_error(result, last_error)
            return result.fail(
                ErrorCode.PROVIDER_ERROR,
                message="没有从返回里找到图片地址",
                detail={"unexpected_payload": getattr(last_error, "payload", {})},
            )
        result.status = ResultStatus.SUCCESS
        result.result = {"images": images, "prompt": prompt, "model": spec.id}
        return result

    def supports_reference_image(self, model: str | None = None) -> bool:
        resolved = resolve_model_id(model, self.capability) or ""
        spec = get_model(resolved)
        return bool(spec and "image" in spec.input_modalities)


def _extract_image_urls(payload: dict[str, Any]) -> list[str]:
    """从官方返回里取图片地址；兼容几种已知形态。"""
    urls: list[str] = []
    output = payload.get("output")
    if isinstance(output, dict):
        choices = output.get("choices")
        if isinstance(choices, list):
            for choice in choices:
                message = choice.get("message") if isinstance(choice, dict) else None
                if not isinstance(message, dict):
                    continue
                for block in message.get("content") or []:
                    if isinstance(block, dict):
                        url = block.get("image") or block.get("url")
                        if isinstance(url, str) and url.startswith(("http://", "https://")):
                            urls.append(url)
        results = output.get("results")
        if isinstance(results, list):
            for item in results:
                if isinstance(item, dict):
                    url = item.get("url")
                    if isinstance(url, str) and url.startswith(("http://", "https://")):
                        urls.append(url)
    return urls


# ---------------------------------------------------------------------------
# 视频生成（异步任务）
# ---------------------------------------------------------------------------


@dataclass
class VideoTask:
    task_id: str
    status: str
    video_url: str = ""
    provider_message: str = ""


class VideoGenerationService(_BaseService):
    """文生视频 / 图生视频。

    实测协议::

        POST /api/v1/services/aigc/video-generation/video-synthesis
        header: X-DashScope-Async: enable
        {"model": "wan3.0-video", "input": {"prompt": ...}, "parameters": {"size": "1280*720"}}
        → {"output": {"task_id": "...", "task_status": "PENDING"}}

        GET /api/v1/tasks/{task_id} → PENDING/RUNNING/SUCCEEDED/FAILED + video_url

    设计要点：**绝不**在一次请求里无限等待。提交与等待分离，轮询有间隔、
    次数、总时长三重上限，并且同一时刻只允许一个视频任务在跑。
    """

    capability = Capability.VIDEO_GENERATE

    def __init__(
        self,
        client: DashScopeClient,
        *,
        artifact_store: ArtifactStore | None = None,
        work_dir: Path | None = None,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        max_wait: float = DEFAULT_MAX_WAIT,
        max_polls: int = DEFAULT_MAX_POLLS,
    ) -> None:
        super().__init__(client, artifact_store=artifact_store, work_dir=work_dir)
        self.poll_interval = max(1.0, poll_interval)
        self.max_wait = max(self.poll_interval, max_wait)
        self.max_polls = max(2, max_polls)
        self._gate = asyncio.Semaphore(1)
        #: 本进程已提交的视频任务数（供配额观察，不做硬限制）
        self.submitted = 0

    def submit(
        self,
        prompt: str,
        *,
        model: str | None = None,
        image: str = "",
        size: str = DEFAULT_VIDEO_SIZE,
    ) -> CapabilityResult:
        result, spec = self._prepare(model)
        if spec is None:
            return result
        prompt = (prompt or "").strip()
        if not prompt:
            return result.fail(ErrorCode.INVALID_ARGUMENT, message="视频描述不能为空")
        if image and "image" not in spec.input_modalities:
            return result.fail(
                ErrorCode.UNSUPPORTED_INPUT,
                message=f"{spec.id} 不支持图生视频",
                detail={"input_modalities": sorted(spec.input_modalities)},
            )
        if size not in ALLOWED_VIDEO_SIZES:
            return result.fail(
                ErrorCode.INVALID_ARGUMENT,
                message=f"不支持的视频尺寸：{size}",
                detail={"allowed": list(ALLOWED_VIDEO_SIZES)},
            )

        payload_input: dict[str, Any] = {"prompt": prompt}
        if image:
            payload_input["img_url"] = image
        response = self.client.post_service(
            "services/aigc/video-generation/video-synthesis",
            {"model": spec.id, "input": payload_input, "parameters": {"size": size}},
            async_mode=True,
            timeout=120.0,
        )
        result.detail.update({"request_id": response.request_id, "http_status": response.status})
        if not response.ok:
            return self._http_error(result, response)
        output = response.payload.get("output") or {}
        task_id = str(output.get("task_id") or "")
        if not task_id:
            return result.fail(
                ErrorCode.PROVIDER_ERROR,
                message="提交成功但没有拿到 task_id",
                detail={"payload": response.payload},
            )
        self.submitted += 1
        result.task_id = task_id
        result.status = ResultStatus.PENDING
        result.result = {
            "task_id": task_id,
            "task_status": str(output.get("task_status") or "PENDING"),
            "model": spec.id,
            "prompt": prompt,
            "size": size,
        }
        return result

    def cancel(self, task_id: str) -> tuple[bool, str]:
        """真正撤销云端异步任务。返回 (是否已取消, 说明)。

        实测（Token Plan）：
        * 任务处于 **PENDING（排队）** 时可以取消，返回 ``task_status=CANCELED``；
        * 一旦进入 **RUNNING**，接口会拒绝：
          ``Failed to cancel the task, please confirm if the task is in PENDING status.``
        * 同步生成（图片/语音）根本没有撤销接口。

        所以能不能"真的停下来"取决于云端此刻的状态，调用方必须按返回值如实告知用户。
        """
        response = self.client.cancel_task(task_id, timeout=30.0)
        if not response.ok:
            return False, response.provider_message() or f"HTTP {response.status}"
        output = response.payload.get("output") or {}
        status = str(output.get("task_status") or "")
        if status in ("CANCELED", "CANCELLED"):
            return True, status
        return False, status or "unknown"

    def poll(self, task_id: str) -> VideoTask:
        response = self.client.get_task(task_id, timeout=30.0)
        if not response.ok:
            return VideoTask(
                task_id=task_id,
                status="ERROR",
                provider_message=response.provider_message() or f"HTTP {response.status}",
            )
        output = response.payload.get("output") or {}
        return VideoTask(
            task_id=task_id,
            status=str(output.get("task_status") or "UNKNOWN"),
            video_url=str(output.get("video_url") or ""),
            provider_message=str(output.get("message") or output.get("code") or ""),
        )

    async def wait(
        self,
        task_id: str,
        *,
        model: str | None = None,
        turn: int = 0,
        max_wait: float | None = None,
        max_polls: int | None = None,
        progress: Callable[[str], None] | None = None,
    ) -> CapabilityResult:
        """轮询到终态；超时后返回 TASK_TIMEOUT 并**保留 task_id**。"""
        result = CapabilityResult(
            capability=self.capability.value,
            model=resolve_model_id(model, self.capability) or "",
            task_id=task_id,
        )
        limit = min(max_wait or self.max_wait, self.max_wait)
        polls = min(max_polls or self.max_polls, self.max_polls)
        interval = self.poll_interval
        started = time.monotonic()

        async with self._gate:  # 同一时刻只跑一个视频任务
            for attempt in range(polls):
                task = await asyncio.to_thread(self.poll, task_id)
                result.detail.setdefault("polls", []).append(
                    {"n": attempt + 1, "status": task.status}
                )
                if task.status in ("SUCCEEDED", "FAILED", "CANCELED", "UNKNOWN"):
                    if task.status == "SUCCEEDED" and task.video_url:
                        name = f"{result.model or 'video'}_{uuid.uuid4().hex[:8]}.mp4"
                        entry = await asyncio.to_thread(
                            self._save, result, task.video_url,
                            filename=name, turn=turn, kind="video", progress=progress,
                        )
                        if entry:
                            result.status = ResultStatus.SUCCESS
                            result.result = {"video": entry, "task_id": task_id}
                            return result
                        return result.fail(
                            ErrorCode.NETWORK_ERROR,
                            message="视频已生成，但下载失败，可以稍后用任务号重试",
                            detail={"task_id": task_id},
                        )
                    return result.fail(
                        ErrorCode.TASK_FAILED,
                        message="视频生成任务失败",
                        detail={"task_status": task.status, "provider_message": task.provider_message},
                    )
                if time.monotonic() - started >= limit:
                    break
                await asyncio.sleep(interval)
        return result.fail(
            ErrorCode.TASK_TIMEOUT,
            message="视频还在生成中，等待超时；可以用任务号稍后查询",
            detail={
                "task_id": task_id,
                "waited_seconds": round(time.monotonic() - started, 1),
                "polls": len(result.detail.get("polls", [])),
            },
        )


# ---------------------------------------------------------------------------
# 语音
# ---------------------------------------------------------------------------


class AudioService(_BaseService):
    """语音合成（已验证）与语音识别（本套餐未打通，如实报错）。"""

    capability = Capability.AUDIO_SPEECH

    # -- TTS --------------------------------------------------------------
    def synthesize(
        self,
        text: str,
        *,
        model: str | None = None,
        voice: str = "",
        turn: int = 0,
        audio_format: str = "wav",
        progress: Callable[[str], None] | None = None,
    ) -> CapabilityResult:
        resolved = resolve_model_id(model, Capability.AUDIO_SPEECH) or ""
        result = CapabilityResult(capability=Capability.AUDIO_SPEECH.value, model=resolved)
        spec = get_model(resolved)
        if spec is None or not spec.implemented:
            result.fail(ErrorCode.MODEL_NOT_FOUND, message=f"目录里没有可用的 TTS 模型 {resolved}")
            return result
        if not spec.available:
            return result.fail(ErrorCode.MODEL_NOT_SUPPORTED, message=f"当前套餐不包含 {resolved}")
        if not self.client.configured:
            return result.fail(ErrorCode.NOT_CONFIGURED, message="服务端缺少百炼 API Key")
        body = (text or "").strip()
        if not body:
            return result.fail(ErrorCode.INVALID_ARGUMENT, message="要合成的文本不能为空")
        if len(body) > MAX_TTS_CHARS:
            return result.fail(
                ErrorCode.UNSUPPORTED_INPUT,
                message=f"单次合成的文本不能超过 {MAX_TTS_CHARS} 字（当前 {len(body)} 字）",
                detail={"max_chars": MAX_TTS_CHARS, "chars": len(body)},
            )
        chosen_voice = voice or spec.extra.get("default_voice", "")
        if not chosen_voice:
            return result.fail(
                ErrorCode.INVALID_ARGUMENT,
                message="缺少音色（voice）",
                detail={"hint": spec.notes},
            )

        response = self.client.post_service(
            "services/audio/tts/SpeechSynthesizer",
            {
                "model": spec.id,
                "input": {
                    "text": body,
                    "voice": chosen_voice,
                    "format": audio_format,
                    "sample_rate": 24000,
                },
            },
            timeout=180.0,
        )
        result.detail.update({"request_id": response.request_id, "http_status": response.status})
        if not response.ok:
            if "engine error" in response.provider_message().lower():
                return result.fail(
                    ErrorCode.INVALID_ARGUMENT,
                    message=f"音色 {chosen_voice} 不可用于 {spec.id}",
                    detail={"provider_message": response.provider_message(), "notes": spec.notes},
                )
            return self._http_error(result, response)
        audio = ((response.payload.get("output") or {}).get("audio") or {})
        url = str(audio.get("url") or "")
        if not url:
            return result.fail(
                ErrorCode.PROVIDER_ERROR,
                message="合成成功但没有返回音频地址",
                detail={"payload_keys": sorted(response.payload.keys())},
            )
        name = f"{spec.id}_{uuid.uuid4().hex[:8]}.{audio_format}"
        entry = self._save(
            result, url, filename=name, turn=turn, kind="audio", progress=progress
        )
        if entry is None:
            return result.fail(
                ErrorCode.NETWORK_ERROR, message="音频已生成但下载失败，请稍后重试"
            )
        result.status = ResultStatus.SUCCESS
        result.result = {"audio": entry, "voice": chosen_voice, "chars": len(body)}
        return result

    # -- ASR --------------------------------------------------------------
    def validate_audio_input(self, path: str | Path | None, *, size: int | None = None) -> str:
        """校验音频格式/大小；返回空串表示通过，否则返回人话错误。"""
        if not path:
            return "没有提供音频"
        name = str(path).lower()
        if not name.endswith(ALLOWED_AUDIO_SUFFIXES):
            return f"不支持的音频格式，请使用 {'/'.join(ALLOWED_AUDIO_SUFFIXES)}"
        actual = size
        if actual is None:
            try:
                actual = Path(path).stat().st_size
            except OSError:
                actual = None
        if actual is not None and actual > MAX_AUDIO_BYTES:
            return f"音频超过 {MAX_AUDIO_BYTES // (1024 * 1024)}MB 上限"
        if actual is not None and actual <= 0:
            return "音频文件是空的"
        return ""

    def transcribe(
        self,
        audio_url: str,
        *,
        model: str | None = None,
        turn: int = 0,
        language_hints: list[str] | None = None,
    ) -> CapabilityResult:
        """语音转写。

        需要**公网可访问**的音频 URL（百炼要求 file_urls 可被抓取）。
        本地文件必须先上传到 OSS —— 本阶段没有上传通道，会如实返回错误，
        而不是假装转写成功。
        """
        resolved = resolve_model_id(model, Capability.AUDIO_TRANSCRIBE) or ""
        result = CapabilityResult(capability=Capability.AUDIO_TRANSCRIBE.value, model=resolved)
        spec = get_model(resolved)
        if spec is None or not spec.implemented:
            return result.fail(ErrorCode.MODEL_NOT_FOUND, message=f"目录里没有可用的 ASR 模型 {resolved}")
        if not self.client.configured:
            return result.fail(ErrorCode.NOT_CONFIGURED, message="服务端缺少百炼 API Key")

        url = (audio_url or "").strip()
        if not url.startswith(("http://", "https://")):
            return result.fail(
                ErrorCode.UNSUPPORTED_INPUT,
                message="语音识别需要公网可访问的音频链接；本地文件需要先上传",
                detail={"notes": spec.notes},
            )

        response = self.client.post_service(
            "services/audio/asr/transcription",
            {
                "model": spec.id,
                "input": {"file_urls": [url]},
                **({"parameters": {"language_hints": language_hints}} if language_hints else {}),
            },
            async_mode=True,
            timeout=60.0,
        )
        result.detail.update({"request_id": response.request_id, "http_status": response.status})
        if not response.ok:
            return self._http_error(result, response)
        task_id = str(((response.payload.get("output") or {}).get("task_id")) or "")
        if not task_id:
            return result.fail(
                ErrorCode.PROVIDER_ERROR,
                message="提交成功但没有拿到 task_id",
                detail={"payload": response.payload},
            )
        result.task_id = task_id
        result.status = ResultStatus.PENDING
        result.result = {"task_id": task_id, "note": "转写任务已提交，需要轮询结果"}
        return result


# ---------------------------------------------------------------------------
# 实时语音
# ---------------------------------------------------------------------------


class RealtimeVoiceClient:
    """实时语音的连接适配与独立连接测试。

    已验证：``wss://<host>/api-ws/v1/realtime?model=qwen-audio-3.0-realtime-plus``
    带 Bearer 头可以建立连接并收到 ``session.created``。

    本阶段只做到"能连上、能握手、能读到会话信息"，完整通话界面留到下一阶段。
    """

    def __init__(self, client: DashScopeClient, *, model: str | None = None) -> None:
        self.client = client
        self.model = resolve_model_id(model, Capability.REALTIME_VOICE) or ""

    async def probe(self, *, timeout: float = 15.0) -> CapabilityResult:
        result = CapabilityResult(
            capability=Capability.REALTIME_VOICE.value, model=self.model
        )
        spec = get_model(self.model)
        if spec is None or not spec.implemented:
            return result.fail(ErrorCode.MODEL_NOT_FOUND, message=f"没有实时语音模型 {self.model}")
        if not self.client.configured:
            return result.fail(ErrorCode.NOT_CONFIGURED, message="服务端缺少百炼 API Key")
        try:
            import websockets
        except ImportError:  # pragma: no cover - websockets 是运行依赖
            return result.fail(ErrorCode.NOT_IMPLEMENTED, message="缺少 websockets 依赖")

        url = f"{self.client.realtime_url}?model={self.model}"
        try:
            async with websockets.connect(
                url,
                additional_headers=self.client.auth_headers(),
                open_timeout=timeout,
                close_timeout=5,
            ) as ws:
                raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
                first = _safe_loads(raw)
                session = first.get("session") or {}
                if first.get("type") != "session.created":
                    return result.fail(
                        ErrorCode.PROVIDER_ERROR,
                        message="实时语音握手没有返回 session.created",
                        detail={"first_event": first.get("type", "")},
                    )
                result.status = ResultStatus.SUCCESS
                result.result = {
                    "connected": True,
                    "session_id": session.get("id", ""),
                    "model": session.get("model", self.model),
                    "voice": session.get("voice", ""),
                    "modalities": session.get("modalities", []),
                    "endpoint": url.split("?")[0],
                }
                return result
        except Exception as exc:  # noqa: BLE001 - 连接期任何异常都归为不可用
            code = ErrorCode.NETWORK_ERROR
            if "401" in str(exc) or "403" in str(exc):
                code = ErrorCode.AUTH_FAILED
            return result.fail(
                code,
                message="实时语音连接失败",
                detail={"error": f"{type(exc).__name__}: {exc}", "endpoint": url.split("?")[0]},
            )


def _safe_loads(raw: Any) -> dict[str, Any]:
    import json

    try:
        data = json.loads(raw if isinstance(raw, str) else raw.decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001
        return {}
    return data if isinstance(data, dict) else {}


# ---------------------------------------------------------------------------
# 聚合入口（下一阶段功能栏直接调这里）
# ---------------------------------------------------------------------------


@dataclass
class MediaServices:
    """把所有生成类服务打包，供 Web/TUI 复用同一份客户端与 Artifact 存储。"""

    client: DashScopeClient
    image: ImageGenerationService
    video: VideoGenerationService
    audio: AudioService
    realtime: RealtimeVoiceClient

    @classmethod
    def create(
        cls,
        providers: list[Any] | None,
        *,
        artifact_store: ArtifactStore | None = None,
        work_dir: Path | None = None,
    ) -> MediaServices:
        client = resolve_bailian_client(providers)
        return cls(
            client=client,
            image=ImageGenerationService(client, artifact_store=artifact_store, work_dir=work_dir),
            video=VideoGenerationService(client, artifact_store=artifact_store, work_dir=work_dir),
            audio=AudioService(client, artifact_store=artifact_store, work_dir=work_dir),
            realtime=RealtimeVoiceClient(client),
        )

    def sync_plan_models(self) -> dict[str, bool]:
        """用 ``/models`` 校准"套餐里到底有哪些模型"。返回被改动过的条目。"""
        if not self.client.configured:
            return {}
        response = self.client.list_models()
        if not response.ok:
            return {}
        ids = [
            str(item.get("id"))
            for item in (response.payload.get("data") or [])
            if isinstance(item, dict) and item.get("id")
        ]
        return sync_available_from_plan(ids)

    def capabilities(self) -> list[dict[str, Any]]:
        from aieb.models.router import CapabilityRouter

        return CapabilityRouter().available_capabilities()

    def default_model(self, capability: Capability) -> str:
        return DEFAULT_MODELS.get(capability, "")
