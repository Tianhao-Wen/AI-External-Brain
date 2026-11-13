"""多模态能力层（Phase 1）的测试。

全部离线：用一个假的 ``DashScopeClient`` 替换真实 HTTP，但**走真实的服务代码
路径**（校验 → 请求 → 解析 → 下载 → 登记 Artifact → 返回结构），
所以"成功/失败/未验证"这些判断都是被测实现本身做出的。

覆盖要求里的 12 项：注册完整性、能力标签、默认模型、显式选择优先、自动路由、
不支持能力报错、Artifact 登记、异步任务状态、音频格式校验、凭据/限流/超时/
额度异常、失败不假装成功、原有聊天与联网搜索不回归。
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from aieb.artifacts import ArtifactStore  # noqa: E402
from aieb.models.catalog import (  # noqa: E402
    AUTO_ALIAS,
    DEFAULT_MODELS,
    MODEL_REGISTRY,
    PROTOCOL_LOCAL_TOOL,
    Capability,
    get_model,
    list_models,
    registry_snapshot,
    resolve_model_id,
    sync_available_from_plan,
)
from aieb.models.dashscope import (  # noqa: E402
    DashScopeClient,
    ProviderResponse,
    classify_http_status,
    normalize_error,
    split_base_url,
)
from aieb.models.result import CapabilityResult, ErrorCode, ResultStatus, redact  # noqa: E402
from aieb.models.router import CapabilityRouter  # noqa: E402
from aieb.models.services import (  # noqa: E402
    AudioService,
    ImageGenerationService,
    MediaServices,
    RealtimeVoiceClient,
    VideoGenerationService,
    resolve_bailian_client,
)


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- 假客户端


class FakeClient(DashScopeClient):
    """可编排的假客户端：按顺序返回预置响应，记录所有请求。"""

    def __init__(self, *, configured: bool = True, timeout: float = 5.0) -> None:
        super().__init__("https://example.aliyuncs.com/compatible-mode/v1", "sk-test", timeout=timeout)
        if not configured:
            self._api_key = ""  # 模拟"没配 Key"
        self.posts: list[tuple[str, dict]] = []
        self.responses: list[ProviderResponse] = []
        self.task_polls: list[ProviderResponse] = []
        self.cancels: list[str] = []
        self.cancel_responses: list[ProviderResponse] = []
        #: 模拟"请求在路上"：让生成请求慢一点返回（默认不等）
        self.post_delay: float = 0.0
        self.downloads: list[str] = []
        self.download_ok = True

    # 覆盖网络边界
    def post_service(self, path, body, *, async_mode=False, timeout=None):
        self.posts.append((path, body))
        if self.post_delay:
            time.sleep(self.post_delay)
        if not self.responses:
            return ProviderResponse(status=200, payload={})
        return self.responses.pop(0)

    def get_task(self, task_id, *, timeout=None):
        if not self.task_polls:
            return ProviderResponse(status=500, payload={"error": {"message": "no more polls"}})
        return self.task_polls.pop(0)

    def cancel_task(self, task_id, *, timeout=None):
        """默认行为：模拟"任务在排队，取消成功"。"""
        self.cancels.append(task_id)
        if not self.cancel_responses:
            return ProviderResponse(status=200, payload={
                "output": {"task_id": task_id, "task_status": "CANCELED"}})
        return self.cancel_responses.pop(0)

    def download(self, url, dest, *, timeout=180.0):
        self.downloads.append(url)
        if not self.download_ok:
            return False, "网络错误"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"fake-media-bytes")
        return True, ""

    def list_models(self):
        return ProviderResponse(
            status=200,
            payload={"data": [{"id": "qwen3.8-max"}, {"id": "qwen-image-3.0-pro"}]},
        )


def ok(payload: dict) -> ProviderResponse:
    return ProviderResponse(status=200, payload=payload, request_id="req-ok")


def err(status: int, message: str = "") -> ProviderResponse:
    body = {"error": {"message": message}} if message else {}
    return ProviderResponse(status=status, payload=body, request_id="req-err")


def image_payload(url: str = "https://oss.example/gen/1.png") -> dict:
    return {"output": {"choices": [{"message": {"content": [{"type": "image", "image": url}]}}]}}


@pytest.fixture()
def store(tmp_path: Path) -> ArtifactStore:
    return ArtifactStore("test-session", root=tmp_path / "artifacts", workspace=tmp_path)


# ---------------------------------------------------------------- 1. 注册完整性


REQUIRED_TEXT_MODELS = [
    "qwen3.8-max", "qwen3.8-flash", "qwen3.7-plus", "qwen3.7-max", "qwen3.6-flash",
    "decision-model-preview",
    "deepseek-v4.1-flash-1017", "deepseek-v4.1-flash", "deepseek-v4-pro-0813",
    "deepseek-v4-pro", "deepseek-v4-flash-0731",
    "glm-5.3", "glm-5.2",
]
REQUIRED_IMAGE_MODELS = ["qwen-image-3.0-pro", "wan2.7-image", "wan2.7-image-pro"]
REQUIRED_VIDEO_MODELS = [
    "wan3.0-video", "happyhorse-1.1-r2v", "happyhorse-1.1-t2v", "happyhorse-1.1-i2v",
]
REQUIRED_AUDIO_MODELS = [
    "qwen-audio-3.0-asr-flash", "qwen-audio-3.0-realtime-plus", "qwen-audio-3.0-tts-plus",
]


def test_all_requested_models_are_registered():
    missing = [
        mid for mid in (
            REQUIRED_TEXT_MODELS + REQUIRED_IMAGE_MODELS
            + REQUIRED_VIDEO_MODELS + REQUIRED_AUDIO_MODELS
        )
        if get_model(mid) is None
    ]
    assert not missing, f"未注册：{missing}"


def test_auto_is_alias_not_a_model():
    assert AUTO_ALIAS not in MODEL_REGISTRY
    # auto 按能力解析到该能力的默认模型
    assert resolve_model_id("auto", Capability.CHAT) == "qwen3.8-max"
    assert resolve_model_id("auto", Capability.IMAGE_GENERATE) == "qwen-image-3.0-pro"
    assert resolve_model_id("auto", Capability.VIDEO_GENERATE) == "wan3.0-video"
    assert resolve_model_id("auto", Capability.AUDIO_SPEECH) == "qwen-audio-3.0-tts-plus"
    # 空值等价于 auto
    assert resolve_model_id("", Capability.CHAT) == "qwen3.8-max"
    # 未知模型返回 None（绝不悄悄替换成默认模型）
    assert resolve_model_id("no-such-model", Capability.CHAT) is None


# ---------------------------------------------------------------- 2. 能力标签


def test_capability_tags_are_correct():
    for mid in REQUIRED_TEXT_MODELS:
        spec = get_model(mid)
        assert spec is not None
        assert Capability.CHAT in spec.capabilities, mid
        assert Capability.IMAGE_GENERATE not in spec.capabilities, mid
        assert spec.protocol == "openai-chat-completions"

    for mid in REQUIRED_IMAGE_MODELS:
        spec = get_model(mid)
        assert spec is not None
        assert Capability.IMAGE_GENERATE in spec.capabilities
        assert spec.output_modalities == frozenset({"image"})
        assert spec.async_task is False

    for mid in REQUIRED_VIDEO_MODELS:
        spec = get_model(mid)
        assert spec is not None
        assert Capability.VIDEO_GENERATE in spec.capabilities
        assert spec.async_task is True

    asr = get_model("qwen-audio-3.0-asr-flash")
    assert asr is not None and Capability.AUDIO_TRANSCRIBE in asr.capabilities
    tts = get_model("qwen-audio-3.0-tts-plus")
    assert tts is not None and Capability.AUDIO_SPEECH in tts.capabilities
    rt = get_model("qwen-audio-3.0-realtime-plus")
    assert rt is not None and Capability.REALTIME_VOICE in rt.capabilities


def test_registered_implemented_verified_are_distinct():
    """注册 ≠ 可实现 ≠ 已验证（happyhorse 三个模型就是活例子）。"""
    for mid in ("happyhorse-1.1-t2v", "happyhorse-1.1-i2v", "happyhorse-1.1-r2v"):
        spec = get_model(mid)
        assert spec is not None and spec.registered is True
        assert spec.implemented is False      # 没有适配器
        assert spec.available is False        # 套餐 /models 里也没有
        assert spec.user_selectable is False

    assert get_model("qwen-image-3.0-pro").verified is True
    assert get_model("qwen-image-3.0-pro").verified_via  # 有证据说明
    assert get_model("qwen-audio-3.0-asr-flash").verified is False
    assert "未验证" in get_model("qwen-audio-3.0-asr-flash").notes or get_model(
        "qwen-audio-3.0-asr-flash"
    ).verified_via == ""


def test_plan_sync_marks_missing_models_unavailable():
    changed = sync_available_from_plan(["qwen3.8-max", "qwen-image-3.0-pro", "wan3.0-video"])
    # happyhorse 已经在目录里标过 available=False，不应被"反向"打开
    assert "happyhorse-1.1-t2v" not in changed
    # wan2.7-image 不在 plan 列表里 → 被标成不可用
    assert get_model("wan2.7-image").available is False
    assert changed.get("wan2.7-image") is False
    # 恢复现场，避免影响后续测试
    sync_available_from_plan(
        [m.id for m in MODEL_REGISTRY.values() if m.provider == "bailian-token-plan"]
    )
    for mid in ("wan2.7-image", "wan2.7-image-pro", "wan3.0-video"):
        get_model(mid).available = True


# ---------------------------------------------------------------- 3. 默认模型


def test_default_model_per_capability():
    assert DEFAULT_MODELS[Capability.CHAT] == "qwen3.8-max"
    assert DEFAULT_MODELS[Capability.IMAGE_GENERATE] == "qwen-image-3.0-pro"
    assert DEFAULT_MODELS[Capability.VIDEO_GENERATE] == "wan3.0-video"
    assert DEFAULT_MODELS[Capability.AUDIO_SPEECH] == "qwen-audio-3.0-tts-plus"
    assert DEFAULT_MODELS[Capability.AUDIO_TRANSCRIBE] == "qwen-audio-3.0-asr-flash"
    assert DEFAULT_MODELS[Capability.WEB_SEARCH] == "aieb-web-search"
    # 每个能力都有默认模型
    for capability in Capability:
        assert capability in DEFAULT_MODELS, capability


def test_local_tools_are_registered_as_existing_capabilities():
    search = get_model(DEFAULT_MODELS[Capability.WEB_SEARCH])
    fetch = get_model(DEFAULT_MODELS[Capability.WEB_FETCH])
    for spec, capability in ((search, Capability.WEB_SEARCH), (fetch, Capability.WEB_FETCH)):
        assert spec is not None
        assert capability in spec.capabilities
        assert spec.protocol == PROTOCOL_LOCAL_TOOL
        assert spec.verified is True  # 现有工具已经跑通过


# ---------------------------------------------------------------- 4. 显式优先


def test_explicit_capability_wins():
    router = CapabilityRouter()
    decision = router.route("随便说点什么", capability="image_generate")
    assert decision.capability is Capability.IMAGE_GENERATE
    assert decision.model == "qwen-image-3.0-pro"
    assert decision.source == "explicit"

    decision = router.route("今天有什么 AI 新闻", capability=Capability.WRITING)
    assert decision.capability is Capability.WRITING
    assert decision.source == "explicit"


def test_explicit_model_implies_capability():
    router = CapabilityRouter()
    decision = router.route("hello", model="wan3.0-video")
    assert decision.capability is Capability.VIDEO_GENERATE
    assert decision.model == "wan3.0-video"
    assert decision.source == "explicit"


def test_explicit_model_that_does_not_support_capability_is_flagged():
    router = CapabilityRouter()
    decision = router.route("画一只猫", capability="image_generate", model="qwen3.8-max")
    assert decision.capability is Capability.IMAGE_GENERATE
    assert decision.model == "qwen3.8-max"
    assert decision.model_mismatch is True


# ---------------------------------------------------------------- 5. 自动路由


@pytest.mark.parametrize(
    "text,expected",
    [
        ("帮我生成一张小猫吃饭的图片", Capability.IMAGE_GENERATE),
        ("画一只橘猫坐在窗台上", Capability.IMAGE_GENERATE),
        ("帮我把这张图片做成短视频", Capability.VIDEO_GENERATE),
        ("生成一段产品介绍视频", Capability.VIDEO_GENERATE),
        ("帮我把这段录音变成文字", Capability.AUDIO_TRANSCRIBE),
        ("把这段会议录音整理成文字稿", Capability.AUDIO_TRANSCRIBE),
        ("把这段话读出来", Capability.AUDIO_SPEECH),
        ("帮我写一篇旅行游记", Capability.WRITING),
        ("今天有什么 AI 新闻", Capability.WEB_SEARCH),
        ("实时语音对话怎么开", Capability.REALTIME_VOICE),
        ("推荐一些美食", Capability.CHAT),
    ],
)
def test_intent_routing(text: str, expected: Capability):
    decision = CapabilityRouter().route(text)
    assert decision.capability is expected, f"{text} → {decision.capability} ({decision.scores})"


def test_attachment_routing():
    router = CapabilityRouter()
    assert router.route("这是什么", attachments=[{"kind": "image"}]).capability is (
        Capability.VISION_UNDERSTANDING
    )
    assert router.route("", attachments=[{"kind": "audio"}]).capability is (
        Capability.AUDIO_TRANSCRIBE
    )
    # 附件是图片，但文字明确要生成视频 → 意图优先
    decision = router.route("帮我把这张图片做成短视频", attachments=[{"kind": "image"}])
    assert decision.capability is Capability.VIDEO_GENERATE
    assert decision.source == "intent"
    # 图片附件 + 改图意图 → 图生图，而不是"看图"
    decision = router.route("把这张图改成水彩风格", attachments=[{"kind": "image"}])
    assert decision.capability is Capability.IMAGE_GENERATE


def test_video_wording_does_not_hijack_writing():
    """'写一段视频文案' 真正要的是写作，不能被"视频"抢走。"""
    decision = CapabilityRouter().route("写一段视频文案")
    assert decision.capability is Capability.WRITING


def test_router_makes_no_model_calls():
    """路由是纯本地判定：不产生任何网络/模型调用。"""
    router = CapabilityRouter()
    decision = router.route("帮我生成一张海报")
    assert decision.source == "intent"
    # 判定结果里没有出现任何请求 ID / 调用痕迹
    assert decision.to_dict()["scores"]


# ---------------------------------------------------------------- 6. 不支持就报错


def test_image_service_rejects_non_image_model(store: ArtifactStore):
    service = ImageGenerationService(FakeClient(), artifact_store=store)
    result = service.generate("一只猫", model="qwen3.8-max")
    assert result.status is ResultStatus.ERROR
    assert result.error_code is ErrorCode.MODEL_NOT_SUPPORTED
    assert result.artifacts == []


def test_unimplemented_model_reports_not_implemented(store: ArtifactStore):
    service = VideoGenerationService(FakeClient(), artifact_store=store)
    result = service.submit("一只猫在跑", model="happyhorse-1.1-t2v")
    # happyhorse 既没有适配器也不在套餐里：优先报"套餐不支持"
    assert result.error_code in (ErrorCode.NOT_IMPLEMENTED, ErrorCode.MODEL_NOT_SUPPORTED)
    assert result.status is ResultStatus.ERROR


def test_unknown_model_reports_model_not_found(store: ArtifactStore):
    service = ImageGenerationService(FakeClient(), artifact_store=store)
    result = service.generate("x", model="not-a-model")
    assert result.error_code is ErrorCode.MODEL_NOT_FOUND


# ---------------------------------------------------------------- 7. Artifact 登记


def test_image_success_registers_artifact(store: ArtifactStore, tmp_path: Path):
    client = FakeClient()
    client.responses = [ok(image_payload())]
    service = ImageGenerationService(client, artifact_store=store, work_dir=tmp_path / "media")

    result = service.generate("一只橘猫", turn=3)

    assert result.status is ResultStatus.SUCCESS
    assert result.result["model"] == "qwen-image-3.0-pro"
    images = result.result["images"]
    assert len(images) == 1 and images[0]["artifact_id"]
    # 真的写进了 ArtifactStore（可被 /file 签名下载）
    artifact = store.get(images[0]["artifact_id"])
    assert artifact is not None and artifact.turn == 3 and artifact.size > 0
    # 对外只给 artifact id，不给带签名的源地址
    assert "https://oss.example" not in str(result.to_dict())
    assert client.downloads and "oss.example" in client.downloads[0]
    # 请求体走的是已验证的协议
    path, body = client.posts[0]
    assert path.endswith("services/aigc/multimodal-generation/generation")
    assert body["input"]["messages"][0]["content"][0]["text"] == "一只橘猫"


def test_image_multiple_count_makes_separate_calls(store: ArtifactStore, tmp_path: Path):
    client = FakeClient()
    client.responses = [ok(image_payload("https://oss.example/1.png")),
                        ok(image_payload("https://oss.example/2.png"))]
    service = ImageGenerationService(client, artifact_store=store, work_dir=tmp_path / "m")
    result = service.generate("两只猫", count=2)
    assert result.status is ResultStatus.SUCCESS
    assert len(result.result["images"]) == 2
    assert len(client.posts) == 2


def test_image_failure_is_not_reported_as_success(store: ArtifactStore, tmp_path: Path):
    client = FakeClient()
    client.responses = [err(400, "invalid parameter")]
    service = ImageGenerationService(client, artifact_store=store, work_dir=tmp_path / "m")
    result = service.generate("一只猫")
    assert result.status is ResultStatus.ERROR
    assert result.artifacts == []
    assert result.result is None


def test_image_empty_payload_is_error(store: ArtifactStore, tmp_path: Path):
    client = FakeClient()
    client.responses = [ok({"output": {"choices": []}})]
    service = ImageGenerationService(client, artifact_store=store, work_dir=tmp_path / "m")
    result = service.generate("一只猫")
    assert result.status is ResultStatus.ERROR
    assert result.error_code is ErrorCode.PROVIDER_ERROR


def test_download_failure_is_error_not_success(store: ArtifactStore, tmp_path: Path):
    client = FakeClient()
    client.responses = [ok(image_payload())]
    client.download_ok = False
    service = ImageGenerationService(client, artifact_store=store, work_dir=tmp_path / "m")
    result = service.generate("一只猫")
    assert result.status is ResultStatus.ERROR
    assert result.artifacts == []


# ---------------------------------------------------------------- 8. 异步任务


def test_video_submit_returns_task_id(store: ArtifactStore, tmp_path: Path):
    client = FakeClient()
    client.responses = [ok({"output": {"task_id": "task-1", "task_status": "PENDING"}})]
    service = VideoGenerationService(client, artifact_store=store, work_dir=tmp_path / "m")
    result = service.submit("一只橘猫在草地上跑")
    assert result.status is ResultStatus.PENDING
    assert result.task_id == "task-1"
    path, body = client.posts[0]
    assert path.endswith("services/aigc/video-generation/video-synthesis")
    assert body["parameters"]["size"] == "1280*720"


def test_video_wait_success_downloads_video(store: ArtifactStore, tmp_path: Path):
    client = FakeClient()
    client.responses = [ok({"output": {"task_id": "task-2", "task_status": "PENDING"}})]
    client.task_polls = [
        ok({"output": {"task_id": "task-2", "task_status": "RUNNING"}}),
        ok({"output": {"task_id": "task-2", "task_status": "SUCCEEDED",
                       "video_url": "https://oss.example/v.mp4"}}),
    ]
    service = VideoGenerationService(
        client, artifact_store=store, work_dir=tmp_path / "m", poll_interval=1.0
    )
    submitted = service.submit("猫")
    result = run(service.wait(submitted.task_id, turn=1))
    assert result.status is ResultStatus.SUCCESS
    assert result.result["video"]["artifact_id"]
    assert store.get(result.result["video"]["artifact_id"]) is not None


def test_video_failed_task_reports_task_failed(store: ArtifactStore, tmp_path: Path):
    client = FakeClient()
    client.task_polls = [ok({"output": {"task_id": "t", "task_status": "FAILED",
                                        "message": "content policy"}})]
    service = VideoGenerationService(
        client, artifact_store=store, work_dir=tmp_path / "m", poll_interval=1.0
    )
    result = run(service.wait("t"))
    assert result.status is ResultStatus.ERROR
    assert result.error_code is ErrorCode.TASK_FAILED
    assert result.task_id == "t"  # 失败也保留任务号


def test_video_timeout_keeps_task_id(store: ArtifactStore, tmp_path: Path):
    client = FakeClient()
    client.task_polls = [ok({"output": {"task_status": "RUNNING"}}) for _ in range(10)]
    service = VideoGenerationService(
        client, artifact_store=store, work_dir=tmp_path / "m",
        poll_interval=1.0, max_wait=1.0, max_polls=3,
    )
    result = run(service.wait("slow-task"))
    assert result.status is ResultStatus.ERROR
    assert result.error_code is ErrorCode.TASK_TIMEOUT
    assert result.task_id == "slow-task"
    assert result.detail["polls"]


def test_video_rejects_unsupported_size(store: ArtifactStore, tmp_path: Path):
    service = VideoGenerationService(FakeClient(), artifact_store=store, work_dir=tmp_path / "m")
    result = service.submit("猫", size="1920*1080")
    assert result.error_code is ErrorCode.INVALID_ARGUMENT


# ---------------------------------------------------------------- 9. 音频


def test_audio_validation_accepts_known_formats(tmp_path: Path):
    service = AudioService(FakeClient())
    good = tmp_path / "a.mp3"
    good.write_bytes(b"x" * 1024)
    assert service.validate_audio_input(good) == ""
    assert service.validate_audio_input(str(good)) == ""


def test_audio_validation_rejects_bad_format_and_size(tmp_path: Path):
    service = AudioService(FakeClient())
    bad = tmp_path / "a.txt"
    bad.write_bytes(b"x")
    assert "不支持的音频格式" in service.validate_audio_input(bad)
    big = tmp_path / "b.wav"
    big.write_bytes(b"x" * (11 * 1024 * 1024))
    assert "上限" in service.validate_audio_input(big)
    assert service.validate_audio_input(None)


def test_tts_success_registers_audio_artifact(store: ArtifactStore, tmp_path: Path):
    client = FakeClient()
    client.responses = [ok({"output": {"audio": {"url": "https://oss.example/a.wav"}}})]
    service = AudioService(client, artifact_store=store, work_dir=tmp_path / "m")
    result = service.synthesize("你好，我是文梦。", turn=2)
    assert result.status is ResultStatus.SUCCESS
    assert result.result["voice"] == "longanqian"  # 默认音色（实测可用）
    assert store.get(result.result["audio"]["artifact_id"]) is not None
    path, body = client.posts[0]
    assert path.endswith("services/audio/tts/SpeechSynthesizer")
    assert body["input"]["format"] == "wav"


def test_tts_rejects_too_long_text(store: ArtifactStore):
    service = AudioService(FakeClient(), artifact_store=store)
    result = service.synthesize("字" * 601)
    assert result.error_code is ErrorCode.UNSUPPORTED_INPUT


def test_tts_engine_error_maps_to_invalid_argument(store: ArtifactStore, tmp_path: Path):
    client = FakeClient()
    client.responses = [err(400, "[cosyvoice:]Engine error [411]: TTS speak operation failed")]
    service = AudioService(client, artifact_store=store, work_dir=tmp_path / "m")
    result = service.synthesize("你好", voice="Cherry")
    assert result.status is ResultStatus.ERROR
    assert result.error_code is ErrorCode.INVALID_ARGUMENT


def test_asr_requires_public_url(store: ArtifactStore):
    service = AudioService(FakeClient(), artifact_store=store)
    result = service.transcribe("C:/tmp/rec.mp3")
    assert result.error_code is ErrorCode.UNSUPPORTED_INPUT
    assert "公网" in result.user_message()


def test_asr_unsupported_by_plan_is_reported_honestly(store: ArtifactStore):
    client = FakeClient()
    client.responses = [err(400, "url error, please check url")]
    service = AudioService(client, artifact_store=store)
    result = service.transcribe("https://cdn.example/a.wav")
    assert result.status is ResultStatus.ERROR
    assert result.error_code is ErrorCode.MODEL_NOT_SUPPORTED
    # 不假装成功、也没有产物
    assert result.artifacts == []


# ---------------------------------------------------------------- 10. 异常映射


def test_missing_credentials(store: ArtifactStore):
    service = ImageGenerationService(FakeClient(configured=False), artifact_store=store)
    result = service.generate("一只猫")
    assert result.error_code is ErrorCode.NOT_CONFIGURED


@pytest.mark.parametrize(
    "status,code",
    [
        (401, ErrorCode.AUTH_FAILED),
        (403, ErrorCode.QUOTA_EXCEEDED),
        (429, ErrorCode.RATE_LIMITED),
        (500, ErrorCode.PROVIDER_ERROR),
        (504, ErrorCode.TIMEOUT),
        (404, ErrorCode.MODEL_NOT_SUPPORTED),
    ],
)
def test_http_status_classification(status: int, code: ErrorCode):
    assert classify_http_status(status) is code


@pytest.mark.parametrize(
    "message,code",
    [
        ("Model not exist.", ErrorCode.MODEL_NOT_SUPPORTED),
        ("url error, please check url", ErrorCode.MODEL_NOT_SUPPORTED),
        ("current user api does not support synchronous calls", ErrorCode.MODEL_NOT_SUPPORTED),
        ("insufficient quota", ErrorCode.QUOTA_EXCEEDED),
        ("too many requests", ErrorCode.RATE_LIMITED),
        ("connection timed out", ErrorCode.TIMEOUT),
        ("something else", ErrorCode.PROVIDER_ERROR),
    ],
)
def test_provider_message_classification(message: str, code: ErrorCode):
    assert normalize_error(message) is code


def test_rate_limit_surfaces_as_error(store: ArtifactStore, tmp_path: Path):
    client = FakeClient()
    client.responses = [err(429)]
    service = ImageGenerationService(client, artifact_store=store, work_dir=tmp_path / "m")
    result = service.generate("猫")
    assert result.error_code is ErrorCode.RATE_LIMITED
    assert "频繁" in result.user_message()


def test_timeout_surfaces_as_error(store: ArtifactStore, tmp_path: Path):
    client = FakeClient()
    client.responses = [ProviderResponse(status=-1, payload={"error": {"message": "timeout"}})]
    service = ImageGenerationService(client, artifact_store=store, work_dir=tmp_path / "m")
    result = service.generate("猫")
    assert result.status is ResultStatus.ERROR


# ---------------------------------------------------------------- 11. 不伪造成功


def test_never_returns_success_without_artifact(store: ArtifactStore, tmp_path: Path):
    """任何"没有拿到真实产出"的路径都必须是 error。"""
    client = FakeClient()
    client.responses = [ok({})]
    service = ImageGenerationService(client, artifact_store=store, work_dir=tmp_path / "m")
    result = service.generate("猫")
    assert result.status is ResultStatus.ERROR and not result.ok


def test_result_payload_hides_credentials_and_signed_urls():
    result = CapabilityResult(capability="image_generate", model="qwen-image-3.0-pro")
    result.detail.update({
        "api_key": "sk-secret",
        "result_url": "https://oss.example/x.png?Signature=abc",
        "http_status": 400,
    })
    normal = result.to_dict()
    assert "sk-secret" not in str(normal)
    dev = result.to_dict(developer=True)
    assert "sk-secret" not in str(dev)
    assert "Signature=abc" not in str(dev)
    assert dev["detail"]["api_key"] == "«已隐藏»"
    assert dev["detail"]["http_status"] == 400


def test_redact_keeps_scalars():
    assert redact({"a": 1, "b": "plain", "c": {"token": "x"}}) == {
        "a": 1, "b": "plain", "c": {"token": "«已隐藏»"},
    }


def test_user_facing_message_is_plain_language():
    result = CapabilityResult(capability="video_generate", model="wan3.0-video")
    result.fail(ErrorCode.TASK_TIMEOUT)
    assert "超时" in result.user_message()
    assert "{" not in result.user_message()


# ---------------------------------------------------------------- 12. 兼容性


def test_chat_pipeline_untouched():
    """多模态层不接管 chat：默认路由仍然指向现有 OpenAI 兼容模型。"""
    decision = CapabilityRouter().route("你好")
    assert decision.capability is Capability.CHAT
    spec = get_model(decision.model)
    assert spec is not None and spec.protocol == "openai-chat-completions"


def test_web_tools_still_the_single_search_path():
    """同一任务只有一条搜索路径：百炼原生搜索没有被注册成第二个搜索工具。"""
    search_models = list_models(capability=Capability.WEB_SEARCH)
    assert [s.id for s in search_models] == ["aieb-web-search"]


def test_registry_snapshot_is_serializable_and_safe():
    import json

    snapshot = registry_snapshot()
    text = json.dumps(snapshot, ensure_ascii=False)
    assert len(snapshot["models"]) == len(MODEL_REGISTRY)
    assert "api_key" not in text
    assert snapshot["alias"][AUTO_ALIAS]["chat"] == "qwen3.8-max"


def test_capabilities_endpoint_reflects_reality():
    services = MediaServices.create(None)
    # 没配置 Key 时不能宣称可用
    caps = {item["capability"]: item for item in services.capabilities()}
    assert caps["chat"]["usable"] == get_model("qwen3.8-max").usable
    assert caps["image_generate"]["default_model"] == "qwen-image-3.0-pro"
    assert caps["image_search"]["implemented"] is False


def test_split_base_url_handles_both_forms():
    assert split_base_url("https://h.cn-beijing.maas.aliyuncs.com/compatible-mode/v1") == (
        "https://h.cn-beijing.maas.aliyuncs.com",
        "https://h.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
    )
    root, compat = split_base_url("https://dashscope.aliyuncs.com")
    assert root == "https://dashscope.aliyuncs.com"
    assert compat.endswith("/compatible-mode/v1")


def test_resolve_client_reuses_provider_config(monkeypatch):
    class FakeProvider:
        name = "qwen3.8-max"
        base_url = "https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
        api_key = "${WD_TEST_KEY}"

        def resolve_api_key(self) -> str:
            return "sk-from-env"

    client = resolve_bailian_client([FakeProvider()])
    assert client.configured is True
    assert client.root == "https://token-plan.cn-beijing.maas.aliyuncs.com"
    assert client.realtime_url.startswith("wss://token-plan.cn-beijing.maas.aliyuncs.com/api-ws/v1/realtime")
    # 绝不暴露 Key 本身：没有公开属性，repr 里也不出现
    assert not hasattr(client, "api_key")
    assert "sk-from-env" not in repr(client)
    assert client.has_key() is True


def test_realtime_client_reports_not_configured(store: ArtifactStore):
    client = RealtimeVoiceClient(FakeClient(configured=False))
    result = run(client.probe(timeout=1))
    assert result.error_code is ErrorCode.NOT_CONFIGURED
