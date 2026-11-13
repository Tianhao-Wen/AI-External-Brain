"""统一的业务结果结构与错误码。

为什么要单独一层：不同模态的产出形态差别很大（图片是文件、视频是异步任务、
语音是音频文件或文本），如果强行拼成一段纯文本，上层就没法渲染卡片、也没法
区分"失败"和"成功但没有内容"。这里只统一**外壳**，``result`` 里保留各模态
自己合理的数据结构。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ResultStatus(str, Enum):
    """一次能力调用的终态。"""

    SUCCESS = "success"    # 真的拿到了产出
    PENDING = "pending"    # 异步任务已提交，还在跑（带 task_id）
    ERROR = "error"        # 明确失败，不需要用户猜


class ErrorCode(str, Enum):
    """错误分类。

    这些码是给上层（Web 普通模式 / 开发者模式）分流用的：
    普通模式按码给一句人话，开发者模式额外看 ``detail``。
    """

    NOT_CONFIGURED = "not_configured"          # 没有 base_url / API Key
    MODEL_NOT_FOUND = "model_not_found"        # 目录里没有这个模型
    MODEL_NOT_SUPPORTED = "model_not_supported"  # 模型不支持这个能力 / 套餐不覆盖
    UNSUPPORTED_INPUT = "unsupported_input"    # 输入格式/大小/时长不合法
    AUTH_FAILED = "auth_failed"                # 401/403
    QUOTA_EXCEEDED = "quota_exceeded"          # 额度/套餐不足
    RATE_LIMITED = "rate_limited"              # 429
    TIMEOUT = "timeout"                        # 连接或等待超时
    NETWORK_ERROR = "network_error"            # DNS/连接失败等
    TASK_FAILED = "task_failed"                # 异步任务明确失败
    TASK_TIMEOUT = "task_timeout"              # 轮询超时（任务可能仍在跑）
    CONTENT_BLOCKED = "content_blocked"        # 被厂商内容审核拦截（重试无意义）
    PROVIDER_ERROR = "provider_error"          # 其它 4xx/5xx
    NOT_IMPLEMENTED = "not_implemented"        # 能力已注册但没有适配器
    INVALID_ARGUMENT = "invalid_argument"


#: 普通模式给用户看的一句话（不出现内部字段名、URL、Key）
USER_FACING_MESSAGE: dict[ErrorCode, str] = {
    ErrorCode.NOT_CONFIGURED: "服务端还没有配置这个能力所需的凭据，暂时无法使用。",
    ErrorCode.MODEL_NOT_FOUND: "没有找到这个模型。",
    ErrorCode.MODEL_NOT_SUPPORTED: "当前套餐或模型不支持这个功能。",
    ErrorCode.UNSUPPORTED_INPUT: "输入的内容格式不受支持，换个文件或描述试试。",
    ErrorCode.AUTH_FAILED: "模型服务鉴权失败，请检查服务端配置。",
    ErrorCode.QUOTA_EXCEEDED: "模型额度不足，稍后或更换套餐后再试。",
    ErrorCode.RATE_LIMITED: "请求太频繁了，等几秒再试。",
    ErrorCode.TIMEOUT: "等待结果超时了，可以稍后重试。",
    ErrorCode.NETWORK_ERROR: "暂时连不上模型服务，请稍后再试。",
    ErrorCode.TASK_FAILED: "生成任务失败了，可以调整描述后重试。",
    ErrorCode.TASK_TIMEOUT: "生成任务还在进行，但等待超时了；可以稍后用任务号查询。",
    ErrorCode.CONTENT_BLOCKED: (
        "这次的画面被模型的内容审核判定为可能涉及版权内容（例如受保护的角色或形象），"
        "换一种描述再试一次通常就好了。"
    ),
    ErrorCode.PROVIDER_ERROR: "模型服务返回了一个错误，请稍后再试。",
    ErrorCode.NOT_IMPLEMENTED: "这个功能还在接入中，暂时不可用。",
    ErrorCode.INVALID_ARGUMENT: "请求参数不合法。",
}


_SENSITIVE_KEY_RE = re.compile(
    r"(api[_-]?key|token|secret|password|authorization|signature|"
    r"access[_-]?key|security[_-]?token|session[_-]?id)",
    re.IGNORECASE,
)
#: 带签名的临时地址（OSS/S3 预签名、dashscope 结果地址）一律不外泄
_URL_VALUE_RE = re.compile(r"^https?://", re.IGNORECASE)
#: 夹在句子中间的结果地址（错误信息里常见）同样要抹掉
_EMBEDDED_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)


def redact(value: Any, *, _depth: int = 0) -> Any:
    """递归脱敏：抹掉凭据字段与所有 http(s) 临时地址。

    开发者模式要看的是"为什么失败"，不需要看到带 Signature 的结果地址——
    那些地址本身就有访问权限，泄露等于把用户的产物公开。
    """
    if _depth > 6:
        return "…"
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            if _SENSITIVE_KEY_RE.search(str(key)):
                out[str(key)] = "«已隐藏»"
            else:
                out[str(key)] = redact(item, _depth=_depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [redact(item, _depth=_depth + 1) for item in value[:50]]
    if isinstance(value, str):
        if _URL_VALUE_RE.match(value.strip()) or len(value) > 500:
            return "«已隐藏地址或超长文本»"
        # 夹在长句里的地址同样要抹掉：带 Signature 的结果地址等同于产物访问权
        return _EMBEDDED_URL_RE.sub("«已隐藏地址»", value)
    return value


@dataclass
class CapabilityResult:
    """所有能力共用的返回外壳。"""

    capability: str
    model: str
    status: ResultStatus = ResultStatus.ERROR
    #: 各模态自己的合理结构（图片列表 / 视频信息 / 转写文本 / 音频信息……）
    result: Any = None
    #: 已登记进 ArtifactStore 的产物（前端拿 id 换签名下载地址）
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    error_code: ErrorCode | None = None
    error_message: str = ""
    #: 异步任务号（视频/长音频）；失败时也要保留，方便用户稍后查询
    task_id: str = ""
    #: 开发者模式的诊断信息（已脱敏）
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status is ResultStatus.SUCCESS

    def fail(
        self,
        code: ErrorCode,
        *,
        message: str = "",
        detail: dict[str, Any] | None = None,
    ) -> CapabilityResult:
        """置为失败并保留原始错误（原始信息只进 detail）。"""
        self.status = ResultStatus.ERROR
        self.error_code = code
        self.error_message = message or USER_FACING_MESSAGE.get(code, "操作失败")
        if detail:
            self.detail.update(detail)
        return self

    def user_message(self) -> str:
        """普通模式展示的一句话。"""
        if self.status is not ResultStatus.ERROR:
            return ""
        # 自己写的、已经是人话的错误优先（例如"音频超过 10MB"）
        if self.error_message and not self.error_message.startswith("{"):
            return self.error_message
        return USER_FACING_MESSAGE.get(self.error_code or ErrorCode.PROVIDER_ERROR, "操作失败")

    def to_dict(self, *, developer: bool = False) -> dict[str, Any]:
        data: dict[str, Any] = {
            "capability": self.capability,
            "model": self.model,
            "status": self.status.value,
            "result": self.result,
            "artifacts": self.artifacts,
            "error_code": self.error_code.value if self.error_code else "",
            "error_message": self.user_message(),
            "task_id": self.task_id,
        }
        if developer:
            data["detail"] = redact(self.detail)
            data["raw_error_message"] = redact(self.error_message)
        return data
