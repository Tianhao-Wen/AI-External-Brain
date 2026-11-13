"""功能栏前端契约测试（web_content.py 源码层面）。

这里不做"看起来对不对"的主观判断，只断言几条硬规则：
* 服务端说不可用的功能，前端必须置灰并给出原因；
* 切换功能不发任何请求、不丢已经输入的内容；
* 生成类功能一定走 capability_request，对话类功能走 user_message；
* 结果只按 Artifact 的签名地址渲染，不把服务端返回的原始字段直接塞进页面；
* 普通模式看不到内部记录，开发者模式才看得到；
* 任务状态只显示真实文案，不编造百分比进度。
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

WEB = (REPO_ROOT / "aieb" / "web_content.py").read_text(encoding="utf-8")
HOST = (REPO_ROOT / "aieb" / "models" / "host.py").read_text(encoding="utf-8")
REMOTE = (REPO_ROOT / "aieb" / "remote.py").read_text(encoding="utf-8")


def js_function(name: str) -> str:
    """截取一个顶层 JS 函数体（到下一个顶层声明之前）。"""
    start = WEB.index(f"function {name}(")
    rest = WEB[start + 1:]
    candidates = [
        pos for pos in (
            rest.find("\nfunction "),
            rest.find("\nconst "),
            rest.find("\nlet "),
            rest.find("\nvar "),
            rest.find("\ndocument."),
        ) if pos > 0
    ]
    end = start + 1 + (min(candidates) if candidates else len(rest))
    return WEB[start:end]


# ---------------------------------------------------------------- 结构


def test_capability_bar_markup_is_below_the_input():
    bar = WEB.index('id="cap-bar"')
    area = WEB.index('id="input-area"')
    hint = WEB.index('id="input-hint"')
    assert area < bar < hint
    assert 'role="toolbar"' in WEB[bar - 120: bar + 120]


def test_primary_entries_and_more_menu_match_the_spec():
    primary = re.search(r"var CAP_PRIMARY = \[(.*?)\];", WEB, re.S).group(1)
    order = [item.strip().strip("'\"") for item in primary.split(",") if item.strip()]
    assert order == ["chat", "image_generate", "video_generate", "writing", "audio_transcribe"]

    more = re.search(r"var CAP_MORE_ORDER = \[(.*?)\];", WEB, re.S).group(1)
    more_items = [item.strip().strip("'\"") for item in more.split(",") if item.strip()]
    # 「更多」里必须有语音生成 / 联网搜索 / 网页读取，并预留后续扩展
    for cap in ("audio_speech", "web_search", "web_fetch"):
        assert cap in more_items
    assert len(more_items) > 3


def test_bar_is_built_from_server_data_only():
    assert "function applyCapabilities(" in WEB
    assert "case 'capabilities':" in WEB
    # 服务端没给状态之前，非对话功能一律不可点
    enabled = js_function("capEnabled")
    assert "cap === 'chat'" in enabled
    assert "info.enabled" in enabled
    assert "return !!(info && info.enabled)" in enabled


def test_unavailable_entries_are_greyed_with_reason():
    sync = js_function("syncCapabilityBar")
    assert "el.disabled = !enabled" in sync
    assert "el.title = reason" in sync
    # 点击不可用功能时给一句人话，而不是默默无视
    setter = js_function("setActiveCapability")
    assert "capEnabled(cap)" in setter
    assert "addSystem(" in setter


def test_placeholder_switches_per_capability():
    assert "inputEl.placeholder = capPlaceholder(active)" in WEB
    # 每个功能的提示语由服务端下发（前后端不会各说一套）
    for text in ("描述你想生成的图片", "描述你想生成的视频", "告诉我你想写什么内容"):
        assert text in HOST
    # 提示语来自服务端
    assert "info.placeholder" in js_function("capPlaceholder")


# ---------------------------------------------------------------- 交互规则


def test_switching_capability_never_calls_the_model():
    setter = js_function("setActiveCapability")
    assert "ws.send" not in setter
    assert "fetch(" not in setter
    assert "inputEl.value" not in setter     # 不丢用户已输入的内容


def test_generative_capabilities_use_capability_request():
    send = js_function("sendMessage")
    assert "CAP_AGENT_SIDE.indexOf(activeCap) < 0" in send
    assert "submitCapabilityRequest()" in send
    dispatch = js_function("dispatchCapability")
    assert "type: 'capability_request'" in dispatch
    assert "capability: capability" in dispatch
    assert "request_id" in dispatch
    # 发送与「重新生成」走同一条提交路径
    assert "dispatchCapability" in js_function("submitCapabilityRequest")


def test_agent_side_capabilities_keep_the_chat_pipeline():
    send = js_function("sendMessage")
    assert "type: 'user_message'" in send
    assert "capability: activeCap" in send
    # 写作/搜索这类能力必须继续用现有对话能力（保留多轮上下文）
    assert "CAP_AGENT_SIDE" in WEB
    for cap in ("'writing'", "'web_search'", "'web_fetch'"):
        assert cap in re.search(r"var CAP_AGENT_SIDE = \[(.*?)\];", WEB, re.S).group(1)


def test_duplicate_submit_is_blocked_client_side():
    dispatch = js_function("dispatchCapability")
    assert "capSubmitInFlight(capability, prompt)" in dispatch
    assert "已经在进行中" in dispatch


def test_events_update_the_same_card():
    handler = js_function("handleCapabilityEvent")
    for field in ("capability_started", "capability_progress", "capability_result"):
        assert f"case '{field}':" in WEB
    assert "findCapTaskEl" in handler
    assert "updateCapTask" in handler
    # 进度事件不会凭空造卡片
    assert "capability_progress" in handler and "return" in handler


def test_task_status_is_truthful():
    status = js_function("capStatusText")
    assert "正在生成" in status and "已完成" in status
    assert "%" not in status                 # 不编造百分比进度
    assert "Math.round" not in status or "elapsed" in status
    # 卡片计时用的是真实时间差
    assert "Date.now()" in js_function("capElapsed")


def test_progress_ui_shows_real_stages():
    """进度 UI 必须基于真实阶段（提交/排队/生成/下载）+ 真实计时。"""
    # 每种生成能力都有自己的阶段序列
    steps = re.search(r"var CAP_STEPS = \{(.*?)\n\};", WEB, re.S).group(1)
    for cap in ("image_generate", "audio_speech", "video_generate"):
        assert cap in steps
    assert "downloading" in steps and "排队" in steps
    # 状态文案覆盖"下载"阶段
    assert "正在下载结果" in js_function("capStatusText")
    # 卡片结构：顶部流动条 + 阶段条 + 独立计时
    assert 'class="cap-progress-bar"' in js_function("createCapTaskCard")
    assert "renderCapSteps(el, cap)" in js_function("updateCapTask")
    # 计时是 mm:ss 等宽数字，不做假百分比
    clock = js_function("capClock")
    assert "'0'" in clock and ":" in clock
    assert "'%'" not in clock        # 只是 mm:ss，没有百分比文案
    assert "tabular-nums" in WEB
    # 生成中显示骨架，完成后被真实结果替换
    skel = js_function("renderCapSkeleton")
    for kind in ("k-image", "k-video", "k-audio"):
        assert kind in skel
    assert "renderCapSkeleton(el, cap)" in js_function("updateCapTask")
    # 参考耗时只是文案提示，标了"通常"
    assert "通常需要" in WEB
    # 尊重"减少动效"设置
    assert "prefers-reduced-motion" in WEB


def test_results_use_artifact_links_only():
    result = js_function("renderCapTaskResult")
    assert "f.url" in result and "f.preview_url" in result
    assert "openLightbox(" in result                 # 图片可点开放大
    assert "video" in result and "controls = true" in result
    assert "audio" in result and "controls = true" in result
    assert "download" in result                      # 可下载
    # 不把服务端原始 result 字段（可能含临时地址）直接渲染出来
    assert "cap.result" not in result
    assert "JSON.stringify(cap.result" not in WEB


def test_timeout_offers_resume_without_resubmitting():
    actions = js_function("renderCapActions")
    assert "继续查询" in actions
    assert "capability_resume" in actions
    assert "capability_request" not in actions


def test_content_block_gives_actionable_hint():
    error = js_function("renderCapTaskError")
    assert "content_blocked" in error
    assert "cap-task-tip" in error
    assert "避开" in error            # 告诉用户改描述，而不是"请重试"
    assert ".cap-task-tip" in WEB     # 样式也要有


def test_generated_prompt_bubble_has_inline_edit():
    """生成任务的用户气泡要和对话消息一样有「编辑」，不是光秃秃一条。"""
    add = js_function("addUser")
    assert "genTask" in add
    assert "startEditGeneratedPrompt" in add
    # 气泡与卡片互相引用，编辑时才能停掉旧任务
    assert "__wdGen" in WEB
    assert "genTask.taskEl = card" in js_function("submitCapabilityRequest")

    edit = js_function("startEditGeneratedPrompt")
    assert "capability_cancel" in edit       # 保存前先停掉作废的任务
    assert "dispatchCapability" in edit      # 再按新描述重新生成
    assert "保存并重新生成" in edit
    # 生成气泡不再隐藏操作栏
    assert "no_actions" not in js_function("dispatchCapability")


def test_generating_task_can_be_stopped_and_edited():
    """生成任务要像对话一样可控：停止 / 编辑提示词（不再有重复的「重新生成」）。"""
    actions = js_function("renderCapActions")
    assert "停止" in actions
    assert "capability_cancel" in actions
    assert "编辑提示词" in actions
    # 「重新生成」与「编辑提示词」重复，已经删掉
    assert "textContent = '重新生成'" not in actions
    # 编辑会把描述回填输入框，并在运行中先停掉旧的
    edit = js_function("editCapTaskPrompt")
    assert "capability_cancel" in edit
    assert "inputEl.value = cap.prompt" in edit
    assert "inputEl.focus()" in edit
    # 取消态有独立渲染与样式，不能显示成"失败"
    assert "function renderCapTaskCancelled" in WEB
    assert "is-cancelled" in WEB
    assert "case 'cancelled'" in js_function("capStatusText")
    # 取消回执单独处理
    assert "ack.cancelled" in WEB


def test_history_restore_rebuilds_cards():
    handler = js_function("handleCapabilityEvent")
    # 恢复事件带 restored 标记；没有卡片就地补一张
    assert "createCapTaskCard" in handler
    clear = [line for line in WEB.splitlines() if "capTasks = {}" in line]
    assert len(clear) >= 2          # 清屏时索引也要清，避免旧卡片残留


def test_clear_resets_task_index():
    start = WEB.index("case 'clear':")
    block = WEB[start: start + 900]
    assert "capTasks = {}" in block
    assert "capUnbound = []" in block


# ---------------------------------------------------------------- 展示模式与安全


def test_normal_mode_hides_internal_records():
    assert "html[data-mode='developer'] .cap-dev { display: block; }" in WEB
    assert "document.documentElement.dataset.mode = mode" in WEB
    dev = js_function("renderCapDev")
    for label in ("任务:", "模型:", "状态:", "错误码:"):
        assert label in dev


def test_no_credentials_or_raw_urls_in_frontend():
    for bad in ("api_key", "Authorization", "aliyuncs.com", "Bearer"):
        assert bad not in WEB
    # 不允许把真实 Key 样式的东西写进前端
    assert not re.search(r"sk-[A-Za-z0-9]{8,}", WEB)


def test_narrow_screen_styles_exist():
    # 芯片区自己滚动，「更多」固定在最右，弹层不被 overflow 裁掉
    assert "#cap-bar-scroll::-webkit-scrollbar" in WEB
    assert "overflow-x: auto" in WEB
    assert 'id="cap-bar-scroll"' in WEB
    assert re.search(r"@media \(max-width: 640px\)[\s\S]{0,4000}#cap-bar", WEB)
    # 菜单不能被祖先的 overflow 剪掉：`#cap-bar` 自身不能再有 overflow
    start = WEB.index("#cap-bar {")
    bar_css = WEB[start:WEB.index("}", start)]
    assert "overflow" not in bar_css


# ---------------------------------------------------------------- 后端契约


def test_server_side_validation_exists():
    for snippet in (
        "if not spec.supports(capability)",
        "if not spec.user_selectable and resolved != default_id",
        "KNOWN_UNAVAILABLE",
        "if request_id in self._seen_requests",
    ):
        assert snippet in HOST


def test_video_polling_never_blocks_the_websocket_loop():
    assert "asyncio.create_task(" in HOST
    assert "to_thread" in HOST
    # 视频提交后立刻播报，不等云端跑完
    assert '_set_status(task, "queued")' in HOST
    assert "capability_progress" in HOST


def test_remote_dispatches_capability_messages():
    for kind in ("capability_request", "capability_resume", "capability_describe"):
        assert f'msg_type == "{kind}"' in REMOTE
    assert '"type": "capabilities"' in REMOTE
    assert "self._refresh_capabilities()" in REMOTE


def test_inline_script_is_valid_javascript():
    node = shutil.which("node")
    if not node:  # pragma: no cover - 环境没有 node 时跳过
        pytest.skip("node 不可用")
    blocks = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", WEB, re.S)
    assert blocks
    target = Path(tempfile.gettempdir()) / "wd_cap_bar_check.js"
    target.write_text("\n;\n".join(blocks), encoding="utf-8")
    proc = subprocess.run([node, "--check", str(target)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr[-2000:]
