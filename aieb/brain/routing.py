"""轻量级工具路由：判断一个问题是否应当先检索知识库。

为什么需要它
------------
Benchmark 里出现过"该查不查"：用户问的是公司制度里的具体数字，模型既不调用
``SearchKnowledge``，也不承认自己没查，而是直接反问用户或凭常识作答。
根因不是工具缺失（工具就在 schema 里），而是模型对"这个问题属于知识库问题"
缺少稳定的判断。把判断做成一条明确的运行时提示，比让模型每次凭感觉决定更可靠。

设计边界（很重要）
------------------
* **只做提示，不强制**：命中时在当轮注入一句系统提醒，模型仍可按上下文决定是否真的检索；
  没有命中就什么都不加，避免把所有闲聊都拖进 RAG。
* **纯规则、可单测**：不调用模型，不引入延迟。
* **可关闭**：``AIEB_KB_ROUTING=off`` 时完全退化到原行为，便于做消融对比。
"""

from __future__ import annotations

import os
import re

# 数量 / 事实型疑问
FACT_INTERROGATIVES: tuple[str, ...] = (
    "多少",
    "几",
    "多久",
    "多长时间",
    "几天",
    "几小时",
    "比例",
    "百分比",
    "占比",
    "标准",
    "规定",
    "条款",
    "流程",
    "门槛",
    "上限",
    "下限",
    "周期",
    "时限",
    "多久",
    "怎么算",
    "如何计算",
    "是否允许",
    "能不能",
    "可以吗",
    "要求是什么",
)

# 企业事实语境
ENTERPRISE_CONTEXT: tuple[str, ...] = (
    "公司",
    "我们",
    "制度",
    "手册",
    "规范",
    "政策",
    "审批",
    "报销",
    "预算",
    "合同",
    "供应商",
    "项目",
    "客户",
    "设备",
    "维保",
    "保密",
    "密级",
    "合规",
    "评分",
    "考核",
    "文档",
    "资料",
    "条款",
    "流程",
)

# 明显不需要检索的场景
NON_RETRIEVAL: tuple[str, ...] = (
    "翻译",
    "润色",
    "改写",
    "写一封",
    "起个名字",
    "起名",
    "讲个笑话",
    "你是谁",
    "你好",
    "谢谢",
    "总结下面这段",
    "帮我改",
    "算一下 1",
)

_HINT = (
    "# 本轮路由提示\n"
    "这个问题在问企业制度/项目资料里的**具体事实**。请先调用 `SearchKnowledge` 检索，"
    "再基于检索结果回答并注明文档名与页码；检索结果为空时明确说知识库中没有相关内容，"
    "不要在未检索的情况下直接回答或反问用户是否需要检索。"
)


def routing_enabled() -> bool:
    return os.environ.get("AIEB_KB_ROUTING", "").strip().lower() not in {"0", "off", "false"}


def _hit(text: str, terms: tuple[str, ...]) -> bool:
    return any(term in text for term in terms)


def needs_knowledge(question: str) -> bool:
    """问题是否应当先检索知识库。"""
    text = (question or "").strip()
    if len(text) < 4:
        return False
    if _hit(text, NON_RETRIEVAL):
        return False
    if not _hit(text, FACT_INTERROGATIVES):
        return False
    return _hit(text, ENTERPRISE_CONTEXT)


def hint_for(question: str, has_documents: bool) -> str:
    """返回当轮应注入的系统提醒；不需要时返回空串。"""
    if not has_documents or not routing_enabled():
        return ""
    return _HINT if needs_knowledge(question) else ""
