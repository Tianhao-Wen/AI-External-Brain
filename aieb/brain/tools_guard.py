"""把内部异常翻译成"人话"错误，避免把调用栈或绝对路径抛给普通用户。"""

from __future__ import annotations

import re

from aieb.brain import llm, meetings

_ABS_PATH_RE = re.compile(r"[A-Za-z]:\\[^\s\"']+|/[^\s\"']*/[^\s\"']*")


def scrub(text: str) -> str:
    """去掉本地绝对路径（产品界面不展示服务器文件系统结构）。"""
    return _ABS_PATH_RE.sub("<路径已隐藏>", text or "")


def describe_error(exc: Exception, fallback: str) -> str:
    if isinstance(exc, (llm.LLMError, meetings.MeetingAnalysisError)):
        return scrub(str(exc)) or fallback
    if isinstance(exc, KeyError):
        return str(exc).strip("'\"") or fallback
    message = scrub(str(exc)) or exc.__class__.__name__
    return f"{fallback}：{message}"
