"""下一阶段功能栏接口（MediaAPI）的测试。"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from aieb.artifacts import ArtifactStore  # noqa: E402
from aieb.models.api import MediaAPI, MediaRequest, create_media_api  # noqa: E402
from aieb.models.catalog import Capability  # noqa: E402
from aieb.models.dashscope import ProviderResponse  # noqa: E402
from aieb.models.result import ErrorCode  # noqa: E402
from tests.test_model_capabilities import FakeClient, err, image_payload, ok  # noqa: E402


def run(coro):
    return asyncio.run(coro)


def make_api(tmp_path: Path, client: FakeClient | None = None) -> MediaAPI:
    api = create_media_api(
        None,
        artifact_store=ArtifactStore("s", root=tmp_path / "a", workspace=tmp_path),
        work_dir=tmp_path / "media",
    )
    if client is not None:
        api.services.client = client
        api.services.image.client = client
        api.services.video.client = client
        api.services.audio.client = client
        api.services.realtime.client = client
    return api


def test_describe_shape(tmp_path: Path):
    api = make_api(tmp_path)
    info = api.describe()
    assert set(info) == {"configured", "capabilities", "catalog"}
    caps = {item["capability"] for item in info["capabilities"]}
    assert "image_generate" in caps and "realtime_voice" in caps
    # 不泄露凭据
    assert "api_key" not in str(info)


def test_handle_routes_before_dispatch(tmp_path: Path):
    client = FakeClient()
    client.responses = [ok(image_payload())]
    api = make_api(tmp_path, client)

    out = run(api.handle(MediaRequest(prompt="帮我生成一张小猫吃饭的图片")))
    assert out["capability"] == "image_generate"
    assert out["status"] == "success"
    assert out["artifacts"] and out["artifacts"][0]["id"].startswith("art_")


def test_handle_explicit_capability(tmp_path: Path):
    client = FakeClient()
    client.responses = [ok({"output": {"audio": {"url": "https://oss.example/a.wav"}}})]
    api = make_api(tmp_path, client)
    out = run(api.handle(MediaRequest(text="你好，文梦", capability="audio_speech")))
    assert out["capability"] == "audio_speech"
    assert out["status"] == "success"
    assert out["artifacts"]


def test_handle_agent_side_capabilities_pass_through(tmp_path: Path):
    api = make_api(tmp_path)
    for text in ("你好", "今天有什么 AI 新闻", "帮我写一篇游记"):
        out = run(api.handle(MediaRequest(prompt=text)))
        assert out["status"] == "success"
        assert out["result"]["handled_by"] == "agent"


def test_handle_model_mismatch_is_rejected(tmp_path: Path):
    api = make_api(tmp_path, FakeClient())
    out = run(api.handle(
        MediaRequest(prompt="画一只猫", capability="image_generate", model="qwen3.8-max")
    ))
    assert out["status"] == "error"
    assert out["error_code"] == ErrorCode.MODEL_NOT_SUPPORTED.value


def test_handle_unimplemented_capability(tmp_path: Path):
    api = make_api(tmp_path, FakeClient())
    out = run(api.handle(MediaRequest(prompt="以图搜图", capability="image_search")))
    assert out["status"] == "error"
    assert out["error_code"] == ErrorCode.NOT_IMPLEMENTED.value


def test_handle_video_waits_and_reports_task_id(tmp_path: Path):
    client = FakeClient()
    client.responses = [ok({"output": {"task_id": "vt-1", "task_status": "PENDING"}})]
    client.task_polls = [ok({"output": {"task_status": "SUCCEEDED",
                                        "video_url": "https://oss.example/v.mp4"}})]
    api = make_api(tmp_path, client)
    api.services.video.poll_interval = 1.0
    out = run(api.handle(MediaRequest(prompt="一只橘猫在草地上跑", capability="video_generate")))
    assert out["status"] == "success"
    assert out["result"]["task_id"] == "vt-1"
    assert out["artifacts"][0]["kind"] == "video"


def test_handle_video_submit_failure_surfaces_error(tmp_path: Path):
    client = FakeClient()
    client.responses = [err(403, "current user api does not support synchronous calls")]
    api = make_api(tmp_path, client)
    out = run(api.handle(MediaRequest(prompt="猫", capability="video_generate")))
    assert out["status"] == "error"
    assert out["error_code"] == ErrorCode.MODEL_NOT_SUPPORTED.value


def test_handle_developer_mode_adds_redacted_detail(tmp_path: Path):
    client = FakeClient()
    client.responses = [err(429)]
    api = make_api(tmp_path, client)
    out = run(api.handle(MediaRequest(prompt="画猫", developer=True)))
    assert out["status"] == "error"
    assert out["detail"]["http_status"] == 429
    assert "sk-test" not in str(out)


def test_handle_asr_honest_failure(tmp_path: Path):
    client = FakeClient()
    client.responses = [err(400, "url error, please check url")]
    api = make_api(tmp_path, client)
    out = run(api.handle(MediaRequest(
        capability="audio_transcribe", audio_url="https://cdn.example/a.wav"
    )))
    assert out["status"] == "error"
    assert out["artifacts"] == []
    assert out["error_code"] == ErrorCode.MODEL_NOT_SUPPORTED.value


def test_handle_requires_no_credentials_to_describe(tmp_path: Path):
    api = make_api(tmp_path)
    info = api.describe()
    assert info["configured"] in (True, False)  # 取决于环境，但不能抛异常
