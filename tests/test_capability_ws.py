"""功能栏（Phase 2）后端契约测试：CapabilityHost + WebSocket 事件。

全部离线：假的 DashScope 客户端替掉真实 HTTP，但走真实的服务/宿主代码路径，
所以「谁被调用、什么被播报、什么被拒绝」都是被测实现自己决定的。

重点覆盖：
* 能力清单只报真实可用性（不可用的必须带原因，绝不假装能用）；
* 显式选择优先（选了「图片生成」不会被意图识别改道）；
* 任务生命周期事件（started / progress / result）与真实状态；
* 视频异步：提交即播报、轮询真实状态、超时不丢云端 task_id、重连不重复提交；
* 模型/参数的**服务端**校验；
* 任务记录持久化与恢复；
* 开发者模式的 detail 已脱敏。
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from aieb.artifacts import ArtifactStore  # noqa: E402
from aieb.conversation import ConversationManager  # noqa: E402
from aieb.memory.session import SessionManager  # noqa: E402
from aieb.models.catalog import Capability, set_provider_configured  # noqa: E402
from aieb.models.host import (  # noqa: E402
    AGENT_SIDE,
    CAPABILITY_LABELS,
    CAPABILITY_PLACEHOLDERS,
    CapabilityHost,
)
from aieb.remote import RemoteServer  # noqa: E402
from tests.test_model_capabilities import FakeClient, err, image_payload, ok  # noqa: E402

ALLOWED_PROGRESS = {"submitting", "queued", "running", "downloading"}
TERMINAL = {"success", "failed", "timeout", "cancelled"}


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- 夹具


def make_host(tmp_path: Path, client: FakeClient | None = None, *, state: Path | None = None,
              media_capable: bool = True, provider_label: str = "千问 Qwen3.8-Max"):
    """建一个宿主实例；返回 (host, sent, client)。"""
    fake = client or FakeClient()
    set_provider_configured(True)
    sent: list[dict] = []

    async def broadcast(payload: dict) -> None:
        sent.append(payload)

    store = ArtifactStore("cap-test", root=tmp_path / "artifacts", workspace=tmp_path)
    host = CapabilityHost(
        providers=None,
        artifact_store=store,
        broadcast=broadcast,
        state_path=state or (tmp_path / "tasks.json"),
        work_dir=tmp_path / "media",
        artifact_payload=lambda items: [
            {"id": item.get("id"), "name": item.get("name"),
             "kind": item.get("kind"), "size": item.get("size"),
             "url": f"/file?id={item.get('id')}&sig=test"}
            for item in items
        ],
        provider_probe=lambda: (media_capable, provider_label),
    )
    # 替掉网络边界，同时把视频轮询压到毫秒级
    host.services.client = fake
    host.services.image.client = fake
    host.services.video.client = fake
    host.services.audio.client = fake
    host.services.realtime.client = fake
    host.services.video.poll_interval = 0.01
    host.services.video.max_wait = 2.0
    return host, sent, fake


async def start_and_wait(host: CapabilityHost, payload: dict, *, timeout: float = 5.0) -> dict:
    ack = await host.start(payload)
    assert ack.get("ok"), ack
    task_id = ack["task_id"]
    runner = host._running.get(task_id)  # noqa: SLF001 - 测试里等后台任务收尾
    if runner is not None:
        await asyncio.wait_for(runner, timeout=timeout)
    return ack


def events(sent: list[dict], kind: str) -> list[dict]:
    return [m["data"] for m in sent if m["type"] == kind]


def video_posts(client: FakeClient) -> list[tuple[str, dict]]:
    return [p for p in client.posts if "video-synthesis" in p[0]]


# ---------------------------------------------------------------- 能力清单


def test_describe_reports_only_real_availability(tmp_path: Path):
    host, _sent, _client = make_host(tmp_path)
    info = host.describe()
    caps = {item["capability"]: item for item in info["capabilities"]}

    # 真实跑通的能力必须可用
    for cap in ("image_generate", "video_generate", "audio_speech", "chat", "writing"):
        assert caps[cap]["enabled"] is True, cap
    # 没适配器 / 套餐不支持的必须置灰，并且给出原因
    for cap in ("audio_transcribe", "realtime_voice", "image_search",
                "text_image_search", "code_interpreter"):
        assert caps[cap]["enabled"] is False, cap
        assert caps[cap]["reason"], f"{cap} 置灰却没有说明原因"
    # 不允许出现"可用但没有默认模型"的假条目
    for item in info["capabilities"]:
        if item["enabled"]:
            assert item["default_model"], item
    # 不泄露凭据
    dumped = json.dumps(info, ensure_ascii=False)
    assert "sk-" not in dumped and "api_key" not in dumped


def test_generation_is_bound_to_current_model(tmp_path: Path):
    """选了 DeepSeek 就不该能用图片/视频/语音——不能背着用户换成千问。"""
    host, _sent, client = make_host(
        tmp_path, media_capable=False, provider_label="DeepSeek V4 Flash"
    )
    info = host.describe()
    caps = {item["capability"]: item for item in info["capabilities"]}

    # 对话类照常可用（那是 DeepSeek 的本职）
    for cap in ("chat", "writing", "web_search", "web_fetch"):
        assert caps[cap]["enabled"] is True, cap
    # 生成类全部置灰，并且原因里点明是"当前对话模型"的问题
    for cap in ("image_generate", "video_generate", "audio_speech"):
        assert caps[cap]["enabled"] is False, cap
        assert "DeepSeek V4 Flash" in caps[cap]["reason"]
        assert "切换到" in caps[cap]["reason"]
    assert info["active_provider"]["media_capable"] is False
    assert "DeepSeek" in info["active_provider"]["hint"]

    # 服务端兜底：即便前端被绕过，也不允许提交
    ack = run(host.start({
        "request_id": "deepseek-1", "capability": "image_generate", "prompt": "一只猫",
    }))
    assert ack["ok"] is False
    assert ack["error_code"] == "model_not_supported"
    assert "DeepSeek" in ack["error_message"]
    assert client.posts == []          # 一个请求都没发出去


def test_generation_reenabled_after_switching_to_qwen(tmp_path: Path):
    """切回千问后能力栏要恢复可用（同一套服务端代码，只换探测结果）。"""
    capable = {"value": False}

    def probe():
        return capable["value"], "DeepSeek V4 Flash" if not capable["value"] else "千问 Qwen3.8-Max"

    host, _sent, _client = make_host(tmp_path)
    host._provider_probe = probe  # noqa: SLF001

    assert {c["capability"]: c for c in host.describe()["capabilities"]}["image_generate"]["enabled"] is False
    capable["value"] = True
    caps = {c["capability"]: c for c in host.describe()["capabilities"]}
    assert caps["image_generate"]["enabled"] is True
    assert caps["image_generate"]["reason"] == ""


def test_describe_has_labels_and_placeholders(tmp_path: Path):
    host, _sent, _client = make_host(tmp_path)
    caps = {item["capability"]: item for item in host.describe()["capabilities"]}
    for value, label in CAPABILITY_LABELS.items():
        assert caps[value]["label"] == label
    for value, placeholder in CAPABILITY_PLACEHOLDERS.items():
        assert caps[value]["placeholder"] == placeholder
    # 对话类功能走现有 Agent 链路，生成类走 MediaAPI
    assert caps["writing"]["agent_side"] is True
    assert caps["image_generate"]["agent_side"] is False


def test_describe_marks_unconfigured_provider(tmp_path: Path):
    host, _sent, _client = make_host(tmp_path)
    fake = FakeClient(configured=False)
    host.services.client = fake
    set_provider_configured(False)
    try:
        caps = {item["capability"]: item for item in host.describe()["capabilities"]}
        assert caps["image_generate"]["enabled"] is False
        assert caps["image_generate"]["reason"]
        # 对话仍然可用：它走的是用户自己的 provider，不依赖百炼
        assert caps["chat"]["enabled"] is True
    finally:
        set_provider_configured(True)


# ---------------------------------------------------------------- 路由与执行


def test_explicit_capability_beats_prompt_intent(tmp_path: Path):
    client = FakeClient()
    client.responses = [ok(image_payload())]
    host, sent, _ = make_host(tmp_path, client)
    # 提示词写的是"写文章"，但用户明确选了图片生成 → 必须走图片服务
    run(start_and_wait(host, {
        "request_id": "r1", "capability": "image_generate", "prompt": "帮我写一篇作文",
    }))
    assert any("multimodal-generation" in path for path, _ in client.posts)
    result = events(sent, "capability_result")[-1]
    assert result["capability"] == "image_generate"
    assert result["status"] == "success"


def test_image_task_lifecycle_events(tmp_path: Path):
    client = FakeClient()
    client.responses = [ok(image_payload())]
    host, sent, _ = make_host(tmp_path, client)
    ack = run(start_and_wait(host, {
        "request_id": "r-img", "capability": "image_generate", "prompt": "一只橘猫在窗台上晒太阳",
    }))

    started = events(sent, "capability_started")
    assert started and started[0]["task_id"] == ack["task_id"]
    assert started[0]["request_id"] == "r-img"
    progress = [p["status"] for p in events(sent, "capability_progress")]
    # 真实阶段：生成 → 下载（下载由服务层回调上报，前端据此显示"正在下载结果"）
    assert progress[:1] == ["running"]
    assert progress[-1] == "downloading"
    assert set(progress) <= ALLOWED_PROGRESS
    # 进度事件一定在结果之前（不能先报完成再报下载）
    kinds = [m["type"] for m in sent]
    assert kinds.index("capability_result") > max(
        i for i, k in enumerate(kinds) if k == "capability_progress"
    )
    result = events(sent, "capability_result")[-1]
    assert result["status"] == "success"
    assert result["artifacts"] and result["artifacts"][0]["kind"] == "image"
    assert result["artifacts"][0]["url"].startswith("/file?id=")
    # 落盘的文件真的存在
    files = list((tmp_path / "artifacts").rglob("*.png"))
    assert files and files[0].stat().st_size > 0
    # 任务记录已持久化
    saved = json.loads((tmp_path / "tasks.json").read_text(encoding="utf-8"))
    assert saved["tasks"][0]["status"] == "success"


def test_tts_task_produces_audio_artifact(tmp_path: Path):
    client = FakeClient()
    client.responses = [ok({"output": {"audio": {"url": "https://oss.example/a.wav"}}})]
    host, sent, _ = make_host(tmp_path, client)
    run(start_and_wait(host, {
        "request_id": "r-tts", "capability": "audio_speech", "prompt": "你好，我是文梦",
    }))
    result = events(sent, "capability_result")[-1]
    assert result["status"] == "success"
    assert result["artifacts"][0]["kind"] == "audio"
    assert any("SpeechSynthesizer" in path for path, _ in client.posts)


def test_video_task_reports_pending_then_success(tmp_path: Path):
    client = FakeClient()
    client.responses = [ok({"output": {"task_id": "vt-1", "task_status": "PENDING"}})]
    client.task_polls = [
        ok({"output": {"task_status": "RUNNING"}}),
        ok({"output": {"task_status": "SUCCEEDED",
                      "video_url": "https://oss.example/v.mp4"}}),
    ]
    host, sent, _ = make_host(tmp_path, client)
    run(start_and_wait(host, {
        "request_id": "r-vid", "capability": "video_generate", "prompt": "海边日落延时",
    }))

    progress = [p["status"] for p in events(sent, "capability_progress")]
    assert progress[0] == "queued"          # 提交成功立刻让用户看到
    assert "running" in progress            # 云端真实状态
    assert progress[-1] == "downloading"    # 下载阶段也要能看到
    assert set(progress) <= ALLOWED_PROGRESS
    result = events(sent, "capability_result")[-1]
    assert result["status"] == "success"
    assert result["provider_task_id"] == "vt-1"
    assert result["artifacts"][0]["kind"] == "video"
    # 只提交了一次
    assert len(video_posts(client)) == 1


def test_video_timeout_keeps_task_id_and_never_resubmits(tmp_path: Path):
    client = FakeClient()
    client.responses = [ok({"output": {"task_id": "vt-long", "task_status": "PENDING"}})]
    client.task_polls = [ok({"output": {"task_status": "RUNNING"}}) for _ in range(20)]
    state = tmp_path / "tasks.json"
    host, sent, _ = make_host(tmp_path, client, state=state)
    host.services.video.max_wait = 0.15
    run(start_and_wait(host, {
        "request_id": "r-vid2", "capability": "video_generate", "prompt": "下雪的城市",
    }))

    result = events(sent, "capability_result")[-1]
    assert result["status"] == "timeout"
    assert result["provider_task_id"] == "vt-long"   # 云端任务号必须留给用户
    assert "继续查询" in result["error_message"] or "超时" in result["error_message"]

    # 模拟重启：新宿主读同一份记录，继续查**老任务**，绝不重新提交
    client2 = FakeClient()
    client2.task_polls = [ok({"output": {
        "task_status": "SUCCEEDED", "video_url": "https://oss.example/v2.mp4"}})]
    host2, sent2, _ = make_host(tmp_path, client2, state=state)

    async def restore() -> None:
        await host2.restore()
        for runner in list(host2._running.values()):  # noqa: SLF001
            await asyncio.wait_for(runner, timeout=10.0)

    run(restore())
    assert video_posts(client2) == []            # 没有重新提交
    first_restore = [d for d in events(sent2, "capability_result") if d.get("restored")]
    assert first_restore and first_restore[0]["status"] == "timeout"   # 先把旧状态摆出来
    final = events(sent2, "capability_result")[-1]
    assert final["status"] == "success"          # 接着查老任务，真的查到了
    assert final["provider_task_id"] == "vt-long"


def test_cancel_really_cancels_queued_video(tmp_path: Path):
    """视频在云端排队（PENDING）时停止 = 调云端取消接口，真的不再计费。"""
    client = FakeClient()
    client.responses = [ok({"output": {"task_id": "vt-q", "task_status": "PENDING"}})]
    client.task_polls = [ok({"output": {"task_status": "RUNNING"}}) for _ in range(200)]
    host, sent, _ = make_host(tmp_path, client)
    host.services.video.max_wait = 30.0
    host.services.video.poll_interval = 0.05

    async def scenario():
        ack = await host.start({"request_id": "r-q", "capability": "video_generate",
                                "prompt": "排队中就取消"})
        await asyncio.sleep(0.2)
        return ack, await host.cancel(ack["task_id"])

    ack, stopped = run(scenario())
    assert stopped is True
    task = host._tasks[ack["task_id"]]  # noqa: SLF001
    assert task.status == "cancelled"
    assert task.cancel_mode == "remote_cancelled"
    assert "已撤销" in task.error_message
    assert client.cancels == ["vt-q"]               # 真的调了取消接口
    assert len(video_posts(client)) == 1            # 没有重新提交


def test_cancel_running_video_is_honest_about_billing(tmp_path: Path):
    """云端已经在生成：取消接口会拒绝，必须如实说"无法撤销、仍会计费"。"""
    client = FakeClient()
    client.responses = [ok({"output": {"task_id": "vt-r", "task_status": "PENDING"}})]
    client.task_polls = [ok({"output": {"task_status": "RUNNING"}}) for _ in range(200)]
    client.cancel_responses = [err(
        200, "Failed to cancel the task, please confirm if the task is in PENDING status."
    )]
    host, sent, _ = make_host(tmp_path, client)
    host.services.video.max_wait = 30.0
    host.services.video.poll_interval = 0.05

    async def scenario():
        ack = await host.start({"request_id": "r-r", "capability": "video_generate",
                                "prompt": "已经在生成"})
        await asyncio.sleep(0.3)
        return ack, await host.cancel(ack["task_id"])

    ack, stopped = run(scenario())
    assert stopped is True
    task = host._tasks[ack["task_id"]]  # noqa: SLF001
    assert task.cancel_mode == "salvaging"
    assert "无法撤销" in task.error_message
    assert "结果会保留" in task.error_message


def test_stopped_task_still_keeps_the_paid_result(tmp_path: Path):
    """停止时请求已经发出、撤销不了 —— 云端算完后结果必须保留，不能让用户白付钱。"""
    client = FakeClient()
    client.post_delay = 0.5
    client.responses = [ok(image_payload())]
    host, sent, _ = make_host(tmp_path, client)

    async def scenario():
        ack = await host.start({"request_id": "r-keep", "capability": "image_generate",
                                "prompt": "一只猫"})
        await asyncio.sleep(0.2)          # 请求已发出，但结果还没回来
        stopped = await host.cancel(ack["task_id"])
        assert stopped is True
        runner = host._running.get(ack["task_id"])  # noqa: SLF001
        assert runner is not None          # 没有被硬取消：还要把结果接住
        await asyncio.wait_for(runner, timeout=5)

    run(scenario())
    task = next(iter(host._tasks.values()))  # noqa: SLF001
    assert task.status == "cancelled"
    assert task.cancel_mode == "salvaged"
    assert task.artifacts                  # 结果保留下来了
    assert "结果已保留" in task.error_message
    salvaged = [d for d in events(sent, "capability_result") if d.get("result_salvaged")]
    assert salvaged and salvaged[-1]["artifacts"]
    assert salvaged[-1]["status"] == "cancelled"      # 状态仍是"已停止"，不冒充完成


def test_cancel_before_request_is_sent_costs_nothing(tmp_path: Path):
    """请求还没发出去就停止：不产生任何请求。"""
    client = FakeClient()
    client.responses = [ok(image_payload())]
    host, sent, _ = make_host(tmp_path, client)

    async def scenario():
        ack = await host.start({"request_id": "r-early", "capability": "image_generate",
                                "prompt": "还没发出去"})
        # 不等 _run 真正开跑就取消
        await host.cancel(ack["task_id"])
        await asyncio.sleep(0.3)

    run(scenario())
    task = next(iter(host._tasks.values()))  # noqa: SLF001
    assert task.status == "cancelled"
    assert task.cancel_mode == "before_send"
    assert "还没有发出" in task.error_message
    assert client.posts == []            # 一个请求都没发 → 0 费用


def test_cancel_stops_waiting_and_reports_honestly(tmp_path: Path):
    """停止：立刻结束等待、标记 cancelled，并说明"模型端可能已生成"。"""
    client = FakeClient()
    client.responses = [ok({"output": {"task_id": "vt-c", "task_status": "PENDING"}})]
    client.task_polls = [ok({"output": {"task_status": "RUNNING"}}) for _ in range(200)]
    host, sent, _ = make_host(tmp_path, client)
    host.services.video.max_wait = 30.0
    host.services.video.poll_interval = 0.05

    async def scenario():
        ack = await host.start({"request_id": "r-cancel", "capability": "video_generate",
                                "prompt": "一段很长的视频"})
        assert ack["ok"] is True
        await asyncio.sleep(0.25)                      # 让它真的进入轮询
        stopped = await host.cancel(ack["task_id"])
        await asyncio.sleep(0.25)
        return ack, stopped

    ack, stopped = run(scenario())
    assert stopped is True
    task = host._tasks[ack["task_id"]]  # noqa: SLF001
    assert task.status == "cancelled"
    # 只提交了一次，停止不会重发
    assert len(video_posts(client)) == 1

    cancelled_events = [d for d in events(sent, "capability_result")
                        if d.get("cancelled_by_user")]
    assert cancelled_events and cancelled_events[-1]["status"] == "cancelled"
    # 文案必须按真实结果给出（此处云端还在排队 → 真的被撤销）
    assert cancelled_events[-1]["cancel_mode"] in ("remote_cancelled", "local_only")
    assert "已停止" in cancelled_events[-1]["error_message"]
    # 停止之后不再继续轮询
    polls_before = len(client.task_polls)
    run(asyncio.sleep(0.3))
    assert len(client.task_polls) == polls_before


def test_cancel_after_finish_is_rejected(tmp_path: Path):
    client = FakeClient()
    client.responses = [ok(image_payload())]
    host, _sent, _ = make_host(tmp_path, client)
    ack = run(start_and_wait(host, {"request_id": "r-done", "capability": "image_generate",
                                    "prompt": "猫"}))
    assert run(host.cancel(ack["task_id"])) is False


def test_duplicate_request_id_is_deduplicated(tmp_path: Path):
    client = FakeClient()
    client.responses = [ok(image_payload()), ok(image_payload())]
    host, _sent, _ = make_host(tmp_path, client)

    async def scenario():
        first = await host.start({"request_id": "dup", "capability": "image_generate",
                                  "prompt": "一朵花"})
        second = await host.start({"request_id": "dup", "capability": "image_generate",
                                   "prompt": "一朵花"})
        runner = host._running.get(first["task_id"])  # noqa: SLF001
        if runner is not None:
            await asyncio.wait_for(runner, timeout=5.0)
        return first, second

    first, second = run(scenario())
    assert first["ok"] is True
    assert second["ok"] is False and second["deduplicated"] is True
    assert len(client.posts) == 1               # 只花了一次钱


# ---------------------------------------------------------------- 服务端校验


def test_server_rejects_bad_models(tmp_path: Path):
    client = FakeClient()
    host, _sent, _ = make_host(tmp_path, client)

    async def scenario():
        unknown = await host.start({"request_id": "m1", "capability": "image_generate",
                                    "prompt": "猫", "model": "no-such-model"})
        mismatch = await host.start({"request_id": "m2", "capability": "image_generate",
                                     "prompt": "猫", "model": "wan3.0-video"})
        return unknown, mismatch

    unknown, mismatch = run(scenario())
    assert unknown["ok"] is False and unknown["error_code"] == "model_not_found"
    assert mismatch["ok"] is False and mismatch["error_code"] == "model_not_supported"
    assert client.posts == []                   # 校验失败不发任何请求


def test_server_rejects_unavailable_and_empty_prompt(tmp_path: Path):
    host, _sent, _client = make_host(tmp_path)

    async def scenario():
        asr = await host.start({"request_id": "a1", "capability": "audio_transcribe",
                                "prompt": "", "audio_url": "https://example/a.wav"})
        empty = await host.start({"request_id": "a2", "capability": "image_generate",
                                  "prompt": "   "})
        search = await host.start({"request_id": "a3", "capability": "code_interpreter",
                                   "prompt": "1+1"})
        return asr, empty, search

    asr, empty, search = run(scenario())
    assert asr["ok"] is False and "转写" in asr["error_message"]
    assert empty["ok"] is False and empty["error_code"] == "invalid_argument"
    assert search["ok"] is False and "解释器" in search["error_message"]


def test_agent_side_capabilities_are_not_run_as_tasks(tmp_path: Path):
    host, _sent, _client = make_host(tmp_path)
    for cap in AGENT_SIDE:
        ack = run(host.start({"request_id": f"ag-{cap.value}", "capability": cap.value,
                              "prompt": "帮我写点东西"}))
        assert ack["ok"] is False
        assert ack.get("agent_side") is True


def test_error_detail_is_redacted(tmp_path: Path):
    client = FakeClient()
    client.responses = [err(500, "internal error at https://oss.example/secret.png?Signature=abc")]
    host, sent, _ = make_host(tmp_path, client)
    run(start_and_wait(host, {
        "request_id": "r-err", "capability": "image_generate", "prompt": "一只狗",
    }))
    result = events(sent, "capability_result")[-1]
    assert result["status"] == "failed"
    assert result["error_message"]                    # 普通模式的人话
    dumped = json.dumps(result, ensure_ascii=False)
    assert "Signature" not in dumped
    assert "https://oss.example" not in dumped        # 签名地址不外泄
    assert "sk-" not in dumped


def test_content_review_block_is_reported_actionably(tmp_path: Path):
    """百炼的内容/版权审核拦截（400 + IP infringement）要单独成码，别让人盲目重试。"""
    client = FakeClient()
    client.responses = [
        err(400, "Output data is suspected of being involved in IP infringement")
    ]
    host, sent, _ = make_host(tmp_path, client)
    run(start_and_wait(host, {
        "request_id": "r-ip", "capability": "image_generate", "prompt": "我想要史迪奇吃饭",
    }))
    result = events(sent, "capability_result")[-1]
    assert result["status"] == "failed"
    assert result["error_code"] == "content_blocked"
    assert "审核" in result["error_message"]
    assert "稍后再试" not in result["error_message"]     # 重试没用，不要误导
    # 原始厂商文案仍留给开发者模式（已脱敏）
    assert "IP infringement" in json.dumps(result, ensure_ascii=False)


# ---------------------------------------------------------------- 恢复与持久化


def test_restore_replays_finished_task_with_artifacts(tmp_path: Path):
    client = FakeClient()
    client.responses = [ok(image_payload())]
    state = tmp_path / "tasks.json"
    host, _sent, _ = make_host(tmp_path, client, state=state)
    run(start_and_wait(host, {
        "request_id": "r-hist", "capability": "image_generate", "prompt": "历史会话里的图",
    }))

    host2, sent2, _ = make_host(tmp_path, FakeClient(), state=state)
    run(host2.restore())
    restored = [d for d in events(sent2, "capability_result") if d.get("restored")]
    assert len(restored) == 1
    assert restored[0]["status"] == "success"
    assert restored[0]["artifacts"]
    # 恢复时必须重新签一次地址，否则刷新后图片渲染不出来
    assert restored[0]["artifacts"][0]["url"].startswith("/file?id=")
    assert restored[0]["prompt"] == "历史会话里的图"


def test_corrupt_state_file_is_ignored(tmp_path: Path):
    state = tmp_path / "tasks.json"
    state.write_text("{not json", encoding="utf-8")
    host, sent, _ = make_host(tmp_path, FakeClient(), state=state)
    assert host.describe()["capabilities"]       # 仍然可用
    run(host.restore())
    assert sent == []


def test_restore_marks_interrupted_task_instead_of_hanging(tmp_path: Path):
    """进程被杀 → 同步任务不可能还在跑：必须如实说"被打断"，不能永远转圈。"""
    state = tmp_path / "tasks.json"
    state.write_text(json.dumps({
        "version": 1,
        "tasks": [{
            "id": "cap_dead", "capability": "image_generate", "model": "qwen-image-3.0-pro",
            "prompt": "画一只猫", "turn": 100001, "status": "running",
            "created_at": 1.0, "updated_at": 1.0, "provider_task_id": "",
            "artifacts": [], "error_code": "", "error_message": "", "detail": {},
            "restore_note": "", "request_id": "dead-1",
        }],
    }, ensure_ascii=False), encoding="utf-8")
    host, sent, client = make_host(tmp_path, FakeClient(), state=state)
    run(host.restore())
    result = events(sent, "capability_result")[-1]
    assert result["status"] == "failed"
    assert "中断" in result["error_message"]
    assert client.posts == []                    # 不会擅自重试（可能已花过钱）
    assert host._tasks["cap_dead"].status == "failed"   # noqa: SLF001


def test_capability_turns_never_collide_with_chat_turns(tmp_path: Path):
    client = FakeClient()
    client.responses = [ok(image_payload()), ok(image_payload())]
    host, _sent, _ = make_host(tmp_path, client)
    first = run(start_and_wait(host, {"request_id": "t1", "capability": "image_generate",
                                      "prompt": "a"}))
    second = run(start_and_wait(host, {"request_id": "t2", "capability": "image_generate",
                                   "prompt": "b"}))
    records = [t for t in host._tasks.values() if t.id in (first["task_id"], second["task_id"])]
    assert records[0].turn != records[1].turn
    assert all(r.turn >= 100_000 for r in records)   # 不与聊天轮次重叠


# ---------------------------------------------------------------- WebSocket 层


class DispatchHarness(RemoteServer):
    """只跑 WebSocket 分发相关的逻辑，宿主与广播全部用测试替身。"""

    def __init__(self, tmp_path: Path, host: CapabilityHost) -> None:
        self.manager = SessionManager(str(tmp_path))
        self.session = self.manager.create()
        self.session_id = self.session.session_id
        self.conversation = ConversationManager()
        self.work_dir = tmp_path
        self.artifact_root = tmp_path / "artifacts"
        self._stores = {}
        self._cap_hosts = {self.session_id: host}
        self._persisted_ids = set()
        self._streaming = False
        self.sent: list[dict] = []
        # 宿主事件也进同一个收集器，才能断言"广播了什么"
        host._broadcast = self._broadcast  # noqa: SLF001

    async def _send(self, websocket, msg):  # noqa: ANN001
        self.sent.append(msg)

    async def _broadcast(self, msg):
        self.sent.append(msg)


def test_ws_capability_request_returns_ack(tmp_path: Path):
    client = FakeClient()
    client.responses = [ok(image_payload())]
    host, _host_sent, _ = make_host(tmp_path, client)
    server = DispatchHarness(tmp_path, host)

    async def scenario():
        await server._handle_capability_request(None, {  # noqa: SLF001
            "request_id": "ws-1", "capability": "image_generate", "prompt": "一只猫",
        })
        await asyncio.sleep(0)

    run(scenario())
    ack = [m for m in server.sent if m["type"] == "capability_ack"]
    assert ack and ack[0]["data"]["ok"] is True
    assert ack[0]["data"]["task_id"].startswith("cap_")


def test_ws_capability_request_rejection_is_reported(tmp_path: Path):
    host, _sent, _ = make_host(tmp_path)
    server = DispatchHarness(tmp_path, host)

    async def scenario():
        await server._handle_capability_request(None, {  # noqa: SLF001
            "request_id": "ws-2", "capability": "realtime_voice", "prompt": "hi",
        })

    run(scenario())
    ack = [m for m in server.sent if m["type"] == "capability_ack"][-1]["data"]
    assert ack["ok"] is False
    assert ack["request_id"] == "ws-2"
    assert ack["error_message"]


def test_refresh_capabilities_broadcasts_list(tmp_path: Path):
    client = FakeClient()
    client.responses = [ok(image_payload())]
    host, _sent, _ = make_host(tmp_path, client)
    server = DispatchHarness(tmp_path, host)
    run(start_and_wait(host, {"request_id": "rc", "capability": "image_generate",
                              "prompt": "猫"}))
    server.sent.clear()
    run(server._refresh_capabilities())  # noqa: SLF001
    kinds = [m["type"] for m in server.sent]
    assert "capabilities" in kinds
    assert "capability_result" in kinds          # 历史任务一起补发
    caps = [m for m in server.sent if m["type"] == "capabilities"][0]["data"]
    assert caps["capabilities"]
    assert any(item["capability"] == "image_generate" for item in caps["capabilities"])


def test_capability_hint_only_for_agent_side(tmp_path: Path):
    host, _sent, _ = make_host(tmp_path)
    server = DispatchHarness(tmp_path, host)

    assert server._apply_capability_hint("writing")   # noqa: SLF001
    tail = server.conversation.history[-1]
    assert "帮我写作" in (tail.content or "")
    assert tail.content.startswith("<system-reminder>") or "写作" in tail.content

    before = len(server.conversation.history)
    for cap in ("", "chat", "image_generate", "no_such_cap"):
        assert server._apply_capability_hint(cap) == ""  # noqa: SLF001
    assert len(server.conversation.history) == before    # 不污染上下文


def test_capability_host_is_lazy_and_cached(tmp_path: Path):
    host, _sent, _ = make_host(tmp_path)
    server = DispatchHarness(tmp_path, host)
    server.providers = []
    first = server._capability_host()      # noqa: SLF001
    second = server._capability_host()     # noqa: SLF001
    assert first is second is host
