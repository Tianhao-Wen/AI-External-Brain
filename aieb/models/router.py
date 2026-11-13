"""CapabilityRouter：把一次用户请求路由成 (能力, 模型)。

优先级（与 Phase 1 要求一致）：

1. **用户手动指定**（功能栏点"图片生成"、或显式传 model）；
2. **当前输入内容与附件类型**（只有图片 → 视觉理解；只有音频 → 转写）；
3. **自然语言表达的明确意图**（加权信号，不是单纯关键词命中）；
4. 兜底 **chat**。

为什么不是"关键词匹配"：中文需求里关键词经常是干扰项（"帮我写一段视频文案"
里有"视频"但真正要的是 writing）。这里给每条信号不同权重、设阈值、并且让
"强意图"压倒"附件默认动作"——但仍不引入额外的模型调用，判定完全可解释、
可测试、零成本。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from aieb.models.catalog import (
    AUTO_ALIAS,
    DEFAULT_MODELS,
    Capability,
    get_model,
    resolve_model_id,
)

#: 意图判定阈值：低于它视为"没说清楚"，回落到附件/默认
INTENT_THRESHOLD = 1.8

#: 强意图：命中任意一条就足以（权重 ≥ 2.0）
_INTENT_PATTERNS: dict[Capability, tuple[tuple[str, float], ...]] = {
    Capability.IMAGE_GENERATE: (
        # "画" 必须出现在句首或"帮我/给我/请"之后：避免"动画""漫画""计划"误命中
        (r"(?:^|[，。！？；、\s]|帮我|给我|请你?)(?:画|绘制)[一]?[张副幅只个]?", 2.0),
        (r"(?:生成|做|出|输出)(?:一张|一幅|一个|几张)?.{0,12}"
         r"(?:图片|图像|插画|海报|头像|封面|照片)", 2.2),
        (r"文生图|图生图|文生图片", 2.2),
        (r"(?:配|做|出)(?:一张|几张)?(?:图|插图)", 1.6),
        (r"(?:改|换|修)(?:一下)?这张图的", 1.9),
        (r"把这张图(?:改成|变成|换成|做成)", 2.0),
        (r"图片(?:生成|编辑|改造)", 1.8),
        (r"(?:图片|图像|海报|插画)", 0.7),
    ),
    Capability.VIDEO_GENERATE: (
        (r"(文生|图生|参考)视频", 2.4),
        (r"(生成|做|制作)(一个|一段|个)?.{0,12}(短视频|视频|动画)", 2.2),
        (r"(做成|变成|剪成)(一个|一段)?(短视频|视频)", 2.2),
        (r"(短视频|视频)", 1.5),
    ),
    Capability.AUDIO_TRANSCRIBE: (
        (r"(语音|录音|音频).{0,6}(转文字|转成文字|变文字|转写|转录)", 2.4),
        (r"(转写|转录|听写)", 2.0),
        (r"(录音|音频|会议记录).{0,8}(文字|文稿|纪要)", 2.0),
        (r"\basr\b", 1.8),
        (r"(字幕|会议纪要|逐字稿)", 1.4),
    ),
    Capability.AUDIO_SPEECH: (
        (r"(语音合成|文字转语音)", 2.4),
        (r"\btts\b", 2.0),
        (r"(朗读|念一遍|读出来|念出来)", 1.9),
        (r"(配音|生成语音|合成一段(语音|音频))", 1.9),
    ),
    Capability.REALTIME_VOICE: (
        (r"(实时|即时).{0,4}(语音|通话|对话)", 2.2),
        (r"语音(通话|对话|连麦)", 2.0),
    ),
    Capability.WEB_SEARCH: (
        (r"(最新|最近|今天|昨天|本周|今年).{0,10}(新闻|消息|动态|价格|票价|政策)", 2.2),
        (r"(新闻|资讯|热点)", 1.4),
        (r"(查一下|搜一下|搜搜|搜索|查查)", 1.2),
        (r"(现在|当前|实时).{0,8}(多少钱|多少|怎么样|情况)", 1.4),
        (r"(官网|官方).{0,6}(链接|地址|公告)", 1.2),
        (r"(天气|航班|高铁|门票|开放时间)", 1.2),
        (r"(最新|最近|实时)", 0.8),
    ),
    Capability.WRITING: (
        (r"写(一篇|一封|一段|个)", 1.9),
        (r"(作文|游记|文案|周报|简历|演讲稿|邮件)", 1.6),
        (r"(润色|改写|扩写|缩写|续写)", 1.8),
        (r"(提纲|大纲|草稿)", 0.9),
    ),
    Capability.VISION_UNDERSTANDING: (
        (r"(这|那)(张|幅|个).{0,2}(图|照片|截图)", 1.9),
        (r"(图|照片|截图)(里|中).{0,6}(是什么|有什么|写了什么|是谁)", 2.0),
        (r"(识别|看看|分析).{0,4}(图片|照片|截图)", 1.8),
    ),
    Capability.WEB_FETCH: (
        (r"(打开|读取|抓取).{0,6}(网页|链接|网址)", 2.0),
    ),
    Capability.CODE_INTERPRETER: (
        (r"(运行|执行).{0,6}(这段|我给的)?.{0,4}代码", 1.6),
    ),
}

_COMPILED: dict[Capability, list[tuple[re.Pattern[str], float]]] = {
    cap: [(re.compile(pat, re.IGNORECASE), weight) for pat, weight in items]
    for cap, items in _INTENT_PATTERNS.items()
}

#: 这些能力是"生成/转换"类：一旦意图成立，就压过"附件默认动作"
_OVERRIDES_ATTACHMENT = frozenset({
    Capability.IMAGE_GENERATE,
    Capability.VIDEO_GENERATE,
    Capability.AUDIO_TRANSCRIBE,
    Capability.AUDIO_SPEECH,
    Capability.REALTIME_VOICE,
})


@dataclass
class RouteDecision:
    capability: Capability
    model: str
    source: str            # "explicit" | "attachment" | "intent" | "default"
    reason: str = ""
    scores: dict[str, float] = field(default_factory=dict)
    #: 用户显式选了模型，但它不支持该能力（调用方应给出明确错误）
    model_mismatch: bool = False

    @property
    def implemented(self) -> bool:
        spec = get_model(self.model)
        return bool(spec and spec.implemented)

    def to_dict(self) -> dict[str, object]:
        return {
            "capability": self.capability.value,
            "model": self.model,
            "source": self.source,
            "reason": self.reason,
            "scores": {k: round(v, 2) for k, v in self.scores.items()},
            "model_mismatch": self.model_mismatch,
        }


class CapabilityRouter:
    """无状态路由器；同一个实例可以复用。"""

    def __init__(self, *, threshold: float = INTENT_THRESHOLD) -> None:
        self.threshold = threshold

    # -- 意图打分 ---------------------------------------------------------
    def score_intents(self, text: str) -> dict[Capability, float]:
        body = (text or "").strip()
        if not body:
            return {}
        scores: dict[Capability, float] = {}
        for capability, patterns in _COMPILED.items():
            total = 0.0
            for pattern, weight in patterns:
                if pattern.search(body):
                    total += weight
            if total > 0:
                scores[capability] = total
        return scores

    def detect_intent(self, text: str) -> tuple[Capability | None, float, dict[str, float]]:
        scores = self.score_intents(text)
        if not scores:
            return None, 0.0, {}
        # 多个能力都过线时取最高分；同分时按固定优先级（生成类 > 检索类 > 写作）
        order = [
            Capability.REALTIME_VOICE,
            Capability.VIDEO_GENERATE,
            Capability.IMAGE_GENERATE,
            Capability.AUDIO_TRANSCRIBE,
            Capability.AUDIO_SPEECH,
            Capability.WEB_FETCH,
            Capability.WEB_SEARCH,
            Capability.VISION_UNDERSTANDING,
            Capability.WRITING,
        ]
        ranked = sorted(
            scores.items(),
            key=lambda kv: (-kv[1], order.index(kv[0]) if kv[0] in order else 99),
        )
        best_capability, best_score = ranked[0]
        readable = {cap.value: score for cap, score in scores.items()}
        if best_score < self.threshold:
            return None, best_score, readable
        return best_capability, best_score, readable

    # -- 路由 -------------------------------------------------------------
    def route(
        self,
        text: str = "",
        *,
        attachments: list[dict[str, object]] | None = None,
        capability: Capability | str | None = None,
        model: str | None = None,
    ) -> RouteDecision:
        kinds = _attachment_kinds(attachments)

        # 1) 用户显式指定能力（功能栏按钮 / API 参数）
        if capability is not None:
            cap = _as_capability(capability)
            if cap is None:
                cap = Capability.CHAT
            return self._decide(cap, model, source="explicit",
                                reason=f"用户显式指定 {cap.value}", mismatch_check=True)

        # 2) 只有显式指定了模型：能力从模型推出来
        if model and model != AUTO_ALIAS:
            spec = get_model(model)
            if spec is not None and spec.capabilities:
                # 生成类能力优先，其次对话
                cap = _primary_capability(spec.capabilities)
                return self._decide(cap, model, source="explicit",
                                    reason=f"按用户选择的模型 {model} 推导能力 {cap.value}")

        intent_cap, intent_score, scores = self.detect_intent(text)

        # 3) 附件类型（但要给"生成类意图"让路）
        if kinds:
            if intent_cap is not None and intent_cap in _OVERRIDES_ATTACHMENT:
                return self._decide(
                    intent_cap, model, source="intent", scores=scores,
                    reason=f"附件 + 明确意图（{intent_score:.1f}）→ {intent_cap.value}",
                )
            if "audio" in kinds:
                return self._decide(
                    Capability.AUDIO_TRANSCRIBE, model, source="attachment",
                    scores=scores, reason="附件是音频 → 语音转写",
                )
            if "video" in kinds:
                return self._decide(
                    Capability.VIDEO_GENERATE, model, source="attachment",
                    scores=scores, reason="附件是视频 → 视频处理",
                )
            if "image" in kinds:
                return self._decide(
                    Capability.VISION_UNDERSTANDING, model, source="attachment",
                    scores=scores, reason="附件是图片 → 视觉理解",
                )

        # 4) 自然语言意图
        if intent_cap is not None:
            return self._decide(
                intent_cap, model, source="intent", scores=scores,
                reason=f"意图信号得分 {intent_score:.1f} ≥ {self.threshold} → {intent_cap.value}",
            )

        # 5) 默认聊天
        return self._decide(
            Capability.CHAT, model, source="default", scores=scores,
            reason="没有明确的多模态意图 → 默认聊天",
        )

    def available_capabilities(self) -> list[dict[str, object]]:
        """功能栏需要的"能力清单"（哪些能力现在真的能调）。"""
        out: list[dict[str, object]] = []
        for capability in Capability:
            default_id = DEFAULT_MODELS.get(capability)
            spec = get_model(default_id) if default_id else None
            out.append({
                "capability": capability.value,
                "default_model": default_id or "",
                "implemented": bool(spec and spec.implemented),
                "configured": bool(spec and spec.configured),
                "verified": bool(spec and spec.verified),
                "usable": bool(spec and spec.usable),
            })
        return out

    # -- 内部 -------------------------------------------------------------
    def _decide(
        self,
        capability: Capability,
        requested_model: str | None,
        *,
        source: str,
        reason: str,
        scores: dict[str, float] | None = None,
        mismatch_check: bool = False,
    ) -> RouteDecision:
        default_id = DEFAULT_MODELS.get(capability) or ""
        resolved = resolve_model_id(requested_model, capability) or default_id
        spec = get_model(resolved)
        mismatch = False
        if spec is not None and not spec.supports(capability):
            mismatch = True
            if not mismatch_check:
                # 用户没显式选模型时，静默回退；显式选了就保留，让服务层报错
                resolved = default_id
                mismatch = False
        return RouteDecision(
            capability=capability,
            model=resolved,
            source=source,
            reason=reason,
            scores=scores or {},
            model_mismatch=mismatch,
        )


def _as_capability(value: Capability | str) -> Capability | None:
    if isinstance(value, Capability):
        return value
    try:
        return Capability(str(value))
    except ValueError:
        return None


def _attachment_kinds(attachments: list[dict[str, object]] | None) -> set[str]:
    kinds: set[str] = set()
    for item in attachments or []:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or item.get("type") or "").lower()
        name = str(item.get("name") or "").lower()
        mime = str(item.get("media_type") or item.get("mime") or "").lower()
        if kind in {"image", "audio", "video", "document"}:
            kinds.add(kind)
            continue
        if mime.startswith("image/"):
            kinds.add("image")
        elif mime.startswith("audio/"):
            kinds.add("audio")
        elif mime.startswith("video/"):
            kinds.add("video")
        elif name.endswith((".png", ".jpg", ".jpeg", ".webp", ".gif")):
            kinds.add("image")
        elif name.endswith((".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac")):
            kinds.add("audio")
        elif name.endswith((".mp4", ".mov", ".webm", ".mkv")):
            kinds.add("video")
        elif name.endswith(".pdf"):
            kinds.add("document")
    return kinds


_GENERATIVE_ORDER = (
    Capability.IMAGE_GENERATE,
    Capability.VIDEO_GENERATE,
    Capability.AUDIO_SPEECH,
    Capability.AUDIO_TRANSCRIBE,
    Capability.REALTIME_VOICE,
)


def _primary_capability(capabilities: frozenset[Capability]) -> Capability:
    for capability in _GENERATIVE_ORDER:
        if capability in capabilities:
            return capability
    for preferred in (Capability.CHAT, Capability.WRITING, Capability.VISION_UNDERSTANDING):
        if preferred in capabilities:
            return preferred
    return sorted(capabilities, key=lambda c: c.value)[0]
