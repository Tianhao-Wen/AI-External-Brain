"""联网搜索：Provider 抽象 + WebSearch 工具。

## 分层

* ``SearchProvider`` —— 只有"给我 query，还我结果列表"这一个职责。
  目前四个实现：
    - ``TavilyProvider``   需要 ``TAVILY_API_KEY``，返回质量最好（官方 API）；
    - ``BingProvider``     无需 Key，抓 Bing 的 RSS 输出，**国内网络可直连**；
    - ``DuckDuckGoProvider`` 无 Key 兜底，抓公开的 lite HTML 端点（国内经常整段不可达）；
    - ``QwenNativeSearchProvider`` provider 自带的联网能力（百炼 Responses API）。
  以后接 Brave / Serper 只需要再加一个实现，工具和 Agent 都不用改。
* ``WebSearch`` —— 对模型暴露的工具：参数、结果格式化、去重、次数预算、
  错误翻译。它不认识任何具体搜索服务的字段。

## 为什么不把 HTML 丢给模型

搜索结果统一整理成 ``标题 / URL / 摘要 / 站点 / 发布时间`` 的紧凑文本，
既不把页面源码塞进上下文，也保证每条都能追溯到真实来源。
"""

from __future__ import annotations

import asyncio
import html
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Protocol

from pydantic import BaseModel, Field

from aieb.tools.base import Tool, ToolResult
from aieb.tools.net_guard import USER_AGENT

DEFAULT_MAX_RESULTS = 5
MAX_RESULTS_CAP = 10
DEFAULT_TIMEOUT = 15.0
# 单次新闻/调研类任务实际会用到 3–6 次搜索；4 次太紧，6 次是实测下来
# “够用且不失控”的取值（硬上限 12）。
DEFAULT_MAX_SEARCHES_PER_TURN = 6
MAX_SEARCHES_PER_TURN_CAP = 12
# 同一轮里重复的 query（忽略大小写/空白/标点）在这个窗口内直接复用结果
DEDUP_TTL_SECONDS = 300.0
# 预算耗尽的统一话术：让模型停止继续搜，而不是反复撞墙
BUDGET_EXHAUSTED_HINT = (
    "【搜索预算已达上限】本轮 {limit} 次搜索额度已用完。"
    "不要再调用 WebSearch —— 重复调用只会再拿到这条相同的提示，不会产生新的检索。"
    "请立即基于已有搜索结果作答；如果某些信息仍然缺失，直接告诉用户哪部分没能查到。"
)
# 最后一次允许的搜索时附带的提醒，让模型提前规划
LAST_SEARCH_NOTE = "（本轮搜索配额已用尽，请基于以上结果作答，不要再调用 WebSearch）"
# 连续几次「网络层连不上」就停止尝试（省掉用户干等 N 次超时）
UNREACHABLE_LIMIT = 2
UNREACHABLE_HINT = (
    "【联网检索不可用】搜索源「{provider}」已连续 {count} 次连接失败（网络阻断 / 断网 / 代理问题），"
    "本轮不再继续尝试搜索。请直接告诉用户联网检索当前不可用，并基于已有信息回答；"
    "不要编造实时信息，也不要再用不同关键词反复重试。"
    "恢复方式：配置环境变量 TAVILY_API_KEY，或在 .aieb/config.yaml 里把 "
    "web_search.provider 改成 bing，或开启 web_search.native_enabled。"
)


@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str = ""
    source: str = ""
    published: str = ""

    def as_dict(self) -> dict[str, str]:
        return {
            "title": self.title,
            "url": self.url,
            "snippet": self.snippet,
            "source": self.source,
            "published": self.published,
        }


class SearchError(Exception):
    """搜索失败（已翻译成人话，可以直接展示给用户）。

    ``retryable``：值得原样重试（限流/5xx/网络抖动）。
    ``unreachable``：网络层根本连不上（DNS/连接被阻断/超时）。这类错误重试
    基本没用，工具层会统计次数并快速失败，避免用户干等 N × timeout 秒。
    """

    def __init__(
        self, message: str, *, retryable: bool = False, unreachable: bool = False
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.unreachable = unreachable


class SearchProvider(Protocol):
    name: str

    def available(self) -> bool: ...

    async def search(
        self, query: str, *, max_results: int, timeout: float
    ) -> list[SearchResult]: ...


def _domain_of(url: str) -> str:
    try:
        host = urllib.parse.urlsplit(url).hostname or ""
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def normalize_query(query: str) -> str:
    """用于去重缓存的归一化 query。

    只做大小写/空白/标点的归一化，**不做**语义合并：把
    ``AI news, October 2026`` 与 ``ai  news october 2026`` 视为同一个查询，
    但 ``AI news October 2025`` 与 ``AI news October 2026`` 仍然是两个查询
    （年份、专有名词不能合并）。
    """
    text = (query or "").lower()
    text = re.sub(r"[\s\u3000]+", " ", text).strip()
    text = re.sub(r"[\.,\u3001\uff0c;\uff1b:\uff1a!\uff01?\uff1f\"'\u201c\u201d\u2018\u2019()\uff08\uff09\[\]\u3010\u3011]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


# ---------------------------------------------------------------------------
# Tavily（官方 API，需要 Key）
# ---------------------------------------------------------------------------


class TavilyProvider:
    name = "tavily"
    endpoint = "https://api.tavily.com/search"

    def __init__(self, api_key: str = "") -> None:
        self.api_key = (api_key or "").strip()

    def available(self) -> bool:
        return bool(self.api_key)

    async def search(
        self, query: str, *, max_results: int, timeout: float
    ) -> list[SearchResult]:
        if not self.available():
            raise SearchError("未配置 TAVILY_API_KEY，无法使用 Tavily 搜索")

        payload = json.dumps({
            "api_key": self.api_key,
            "query": query,
            "max_results": max_results,
            "search_depth": "basic",
            "include_answer": False,
            "include_raw_content": False,
        }).encode("utf-8")

        raw = await asyncio.to_thread(self._post, payload, timeout)
        return _parse_tavily(raw)

    def _post(self, payload: bytes, timeout: float) -> bytes:
        request = urllib.request.Request(
            self.endpoint,
            data=payload,
            headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read(2 * 1024 * 1024)
        except urllib.error.HTTPError as exc:
            raise _translate_http_error(exc.code) from exc
        except urllib.error.URLError as exc:
            raise SearchError(f"无法连接搜索服务：{getattr(exc, 'reason', exc)}", retryable=True) from exc
        except TimeoutError as exc:
            raise SearchError("搜索请求超时", retryable=True) from exc


def _translate_http_error(code: int) -> SearchError:
    if code in (401, 403):
        return SearchError("搜索服务的 API Key 无效或无权限（请检查 TAVILY_API_KEY）")
    if code == 429:
        return SearchError("搜索服务限流（请求过于频繁），请稍后再试", retryable=True)
    if code == 432 or code == 433:
        return SearchError("搜索服务配额已用尽，请检查套餐或稍后再试")
    if 500 <= code < 600:
        return SearchError(f"搜索服务暂时不可用（HTTP {code}），请稍后再试", retryable=True)
    return SearchError(f"搜索请求失败（HTTP {code}）")


def _parse_tavily(raw: bytes) -> list[SearchResult]:
    try:
        data = json.loads(raw.decode("utf-8", "replace"))
    except json.JSONDecodeError as exc:
        raise SearchError("搜索服务返回了无法解析的内容", retryable=True) from exc
    items = data.get("results") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return []
    results: list[SearchResult] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        title = str(item.get("title") or "").strip()
        if not url or not title:
            continue
        results.append(SearchResult(
            title=title,
            url=url,
            snippet=" ".join(str(item.get("content") or "").split())[:600],
            source=_domain_of(url),
            published=str(item.get("published_date") or "").strip(),
        ))
    return results


# ---------------------------------------------------------------------------
# DuckDuckGo Lite（无 Key 兜底）
# ---------------------------------------------------------------------------

_DDG_LINK_RE = re.compile(
    r"<a[^>]+href=\"(?P<href>[^\"]+)\"[^>]*class=['\"]result-link['\"][^>]*>(?P<title>.*?)</a>",
    re.S | re.I,
)
_DDG_SNIPPET_RE = re.compile(
    r"<td[^>]*class=['\"]result-snippet['\"][^>]*>(?P<snippet>.*?)</td>",
    re.S | re.I,
)
_TAG_RE = re.compile(r"<[^>]+>")


def _strip_tags(raw: str) -> str:
    """去掉标签并归一化空白。

    注意顺序：先解实体、再删一次标签。搜索结果的标题/摘要里经常出现
    ``&lt;script&gt;`` 这种被转义的标签，如果只删一次，解实体之后它会重新变成
    一个"标签形状"的字符串混进上下文；这里多做一次删除把它清掉。
    """
    text = html.unescape(_TAG_RE.sub(" ", raw))
    text = _TAG_RE.sub(" ", text)
    return " ".join(html.unescape(text).split())


def _unwrap_ddg(href: str) -> str:
    """把 ``//duckduckgo.com/l/?uddg=<encoded>`` 还原成真实地址。"""
    if not href:
        return ""
    if href.startswith("//"):
        href = "https:" + href
    parsed = urllib.parse.urlsplit(href)
    query = urllib.parse.parse_qs(parsed.query)
    target = (query.get("uddg") or [""])[0]
    return target or href


class DuckDuckGoProvider:
    name = "duckduckgo"
    endpoint = "https://lite.duckduckgo.com/lite/"

    def available(self) -> bool:
        return True

    async def search(
        self, query: str, *, max_results: int, timeout: float
    ) -> list[SearchResult]:
        raw = await asyncio.to_thread(self._get, query, timeout)
        return _parse_ddg(raw, max_results)

    def _get(self, query: str, timeout: float) -> str:
        url = f"{self.endpoint}?q={urllib.parse.quote(query)}"
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                "Accept": "text/html",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read(1024 * 1024).decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                raise SearchError("搜索服务限流（请求过于频繁），请稍后再试", retryable=True) from exc
            raise _translate_http_error(exc.code) from exc
        except urllib.error.URLError as exc:
            raise SearchError(
                f"无法连接搜索服务：{getattr(exc, 'reason', exc)}",
                retryable=True,
                unreachable=True,
            ) from exc
        except TimeoutError as exc:
            raise SearchError("搜索请求超时", retryable=True, unreachable=True) from exc


def _parse_ddg(page: str, max_results: int) -> list[SearchResult]:
    links = list(_DDG_LINK_RE.finditer(page))
    snippets = [_strip_tags(m.group("snippet")) for m in _DDG_SNIPPET_RE.finditer(page)]
    results: list[SearchResult] = []
    for index, match in enumerate(links[:max_results]):
        url = _unwrap_ddg(html.unescape(match.group("href")))
        title = _strip_tags(match.group("title"))
        if not url or not title or not url.startswith(("http://", "https://")):
            continue
        snippet = snippets[index] if index < len(snippets) else ""
        results.append(SearchResult(
            title=title,
            url=url,
            snippet=snippet,
            source=_domain_of(url),
            # DDG lite 不返回发布时间：只能从 URL 路径或摘要开头的日期尽量推断，
            # 推不出来就留空（格式化时会明确写"未标注"）。
            published=_guess_published(url, snippet),
        ))
    return results


_URL_DATE_RE = re.compile(r"/(20\d{2})[-_/](\d{1,2})[-_/](\d{1,2})(?:/|$)")
_URL_MONTH_RE = re.compile(r"/(20\d{2})[-_/](\d{1,2})(?:/|$)")
_SNIPPET_DATE_RE = re.compile(
    r"(?:^|\s)(20\d{2})\s*[-年/]\s*(\d{1,2})\s*[-月/]\s*(\d{1,2})\s*日?"
)


def _guess_published(url: str, snippet: str) -> str:
    """尽量给出可核对的日期；推不出来就返回空串，绝不编一个日期填进去。"""
    match = _URL_DATE_RE.search(url or "")
    if match:
        year, month, day = match.groups()
        return f"{year}-{int(month):02d}-{int(day):02d}（来自链接路径）"
    match = _SNIPPET_DATE_RE.search(snippet or "")
    if match:
        year, month, day = match.groups()
        return f"{year}-{int(month):02d}-{int(day):02d}（来自摘要）"
    match = _URL_MONTH_RE.search(url or "")
    if match:
        year, month = match.groups()
        return f"{year}-{int(month):02d}（来自链接路径，精确到月）"
    return ""


# ---------------------------------------------------------------------------
# Bing RSS（无需 Key，国内网络通常可直连）
# ---------------------------------------------------------------------------
#
# 背景：DuckDuckGo 在国内网络经常整个域名不可达（TCP 443 都连不上），
# WebSearch 会连续超时，用户看到的就是"连不上搜索服务"。Bing 提供
# ``?format=rss`` 的搜索结果输出（实测 0.3–0.5s 返回 10 条），既能直连、
# 又不需要任何 API Key，因此作为 Tavily 之后的第二兜底。


class BingProvider:
    name = "bing"
    endpoint = "https://www.bing.com/search"

    def available(self) -> bool:
        return True

    async def search(
        self, query: str, *, max_results: int, timeout: float
    ) -> list[SearchResult]:
        raw = await asyncio.to_thread(self._get, query, timeout, max_results)
        return _parse_bing_rss(raw, max_results)

    def _get(self, query: str, timeout: float, max_results: int) -> str:
        url = (
            f"{self.endpoint}?q={urllib.parse.quote(query)}"
            f"&format=rss&setlang=zh-CN&count={max(10, min(max_results, 20))}"
        )
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                "Accept": "application/rss+xml, application/xml;q=0.9, */*;q=0.8",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read(1024 * 1024).decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                raise SearchError("Bing 搜索限流（请求过于频繁），请稍后再试", retryable=True) from exc
            raise _translate_http_error(exc.code) from exc
        except urllib.error.URLError as exc:
            raise SearchError(
                f"无法连接 Bing 搜索：{getattr(exc, 'reason', exc)}",
                retryable=True,
                unreachable=True,
            ) from exc
        except TimeoutError as exc:
            raise SearchError("Bing 搜索请求超时", retryable=True, unreachable=True) from exc


_BING_ITEM_RE = re.compile(r"<item>(.*?)</item>", re.S | re.I)

def _xml_field(item: str, tag: str) -> str:
    match = re.search(rf"<{tag}[^>]*>(.*?)</{tag}>", item, re.S | re.I)
    if not match:
        return ""
    raw = match.group(1).strip()
    # 处理 CDATA
    cdata = re.match(r"^<!\[CDATA\[(.*?)\]\]>$", raw, re.S)
    if cdata:
        raw = cdata.group(1)
    return _strip_tags(raw)


def _parse_bing_rss(xml: str, max_results: int) -> list[SearchResult]:
    """解析 Bing 的 RSS 搜索结果（标题/链接/摘要/发布时间）。"""
    results: list[SearchResult] = []
    seen: set[str] = set()
    for block in _BING_ITEM_RE.findall(xml):
        url = html.unescape(_xml_field(block, "link")).strip()
        title = _xml_field(block, "title")
        if not url.startswith(("http://", "https://")) or not title or url in seen:
            continue
        seen.add(url)
        results.append(SearchResult(
            title=title,
            url=url,
            snippet=_xml_field(block, "description"),
            source=_domain_of(url),
            published=_xml_field(block, "pubDate")[:40],
        ))
        if len(results) >= max_results:
            break
    return results


# ---------------------------------------------------------------------------
# Bing 新闻 RSS（无需 Key；带真实发布时间，用于时效性问题）
# ---------------------------------------------------------------------------


class BingNewsProvider:
    """Bing 新闻 RSS：返回标题 / 真实原文链接 / 摘要 / 发布时间 / 媒体名。

    实测（2026-11）：普通网页搜索对"最新新闻"这类查询经常只返回各站首页，
    拿不到可核对的时间；而 ``/news/search?format=rss`` 返回的是真正的新闻条目，
    每条都带 ``pubDate`` 与 ``News:Source``，正好满足"要日期、要来源"的要求。
    ``<link>`` 是 Bing 的跳转地址，其中 ``url=`` 参数才是原文链接，这里解析出来。
    """

    name = "bing-news"
    endpoint = "https://www.bing.com/news/search"

    def available(self) -> bool:
        return True

    async def search(
        self, query: str, *, max_results: int, timeout: float
    ) -> list[SearchResult]:
        raw = await asyncio.to_thread(self._get, query, timeout, max_results)
        return _parse_bing_news(raw, max_results)

    def _get(self, query: str, timeout: float, max_results: int) -> str:
        url = (
            f"{self.endpoint}?q={urllib.parse.quote(query)}"
            f"&format=rss&setlang=zh-CN&count={max(10, min(max_results, 20))}"
        )
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                "Accept": "application/rss+xml, application/xml;q=0.9, */*;q=0.8",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read(1024 * 1024).decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                raise SearchError("Bing 新闻搜索限流（请求过于频繁），请稍后再试", retryable=True) from exc
            raise _translate_http_error(exc.code) from exc
        except urllib.error.URLError as exc:
            raise SearchError(
                f"无法连接 Bing 新闻搜索：{getattr(exc, 'reason', exc)}",
                retryable=True,
                unreachable=True,
            ) from exc
        except TimeoutError as exc:
            raise SearchError("Bing 新闻搜索请求超时", retryable=True, unreachable=True) from exc


_NEWS_TIME_HINTS = (
    "最新", "新闻", "今天", "今日", "昨天", "本周", "近期", "最近", "刚刚", "发布",
    "新闻稿", "股价", "涨跌", "天气", "赛程", "赛果",
    "latest", "news", "today", "yesterday", "this week", "just released",
    "release notes", "announced", "price", "weather", "score",
)


def looks_time_sensitive(query: str) -> bool:
    """查是不是"有时效性要求"的问题（决定要不要优先走新闻源）。"""
    text = (query or "").lower()
    return any(hint in text for hint in _NEWS_TIME_HINTS)


def _unwrap_bing_news_link(link: str) -> str:
    """Bing 新闻 RSS 的 link 是跳转地址，``url=`` 参数才是真正的原文地址。"""
    try:
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(link).query)
    except ValueError:
        return link
    target = (query.get("url") or [""])[0]
    if target.startswith(("http://", "https://")):
        return target
    return link


def _parse_bing_news(xml: str, max_results: int) -> list[SearchResult]:
    results: list[SearchResult] = []
    seen: set[str] = set()
    for block in _BING_ITEM_RE.findall(xml):
        raw_link = html.unescape(_xml_field(block, "link")).strip()
        url = _unwrap_bing_news_link(raw_link)
        title = _xml_field(block, "title")
        if not title or not url.startswith(("http://", "https://")) or url in seen:
            continue
        seen.add(url)
        results.append(SearchResult(
            title=title,
            url=url,
            snippet=_xml_field(block, "description"),
            source=_xml_field(block, "News:Source") or _domain_of(url),
            published=_xml_field(block, "pubDate")[:41],
        ))
        if len(results) >= max_results:
            break
    return results


# ---------------------------------------------------------------------------
# 千问原生联网（阿里云百炼 Responses API 的 web_search + web_extractor）
# ---------------------------------------------------------------------------

class QwenNativeSearchProvider:
    """调用 provider 自带的联网能力，而不是我们自己去爬。

    实测要点（2026-10，百炼 Token Plan）：

    * ``/responses`` 端点可用；
    * 只传 ``web_search`` 会被**静默忽略**（模型会说自己不能联网）；
    * 只传 ``web_extractor`` → 400 ``must be executed with web_search tool``；
    * **必须两个一起传**才会真的检索：``[{type: web_search}, {type: web_extractor}]``。

    返回结构里 ``web_search_call`` 项的 ``action.query`` / ``action.sources``
    携带真实检索词与来源 URL——这就是我们可以引用的可追溯来源。

    成本特征：一次原生检索 = 一次完整模型调用 + 检索上下文（实测 1.8万~4.4万
    input tokens），比 Tavily/DDG 贵得多，但一次就能拿到可直接引用的结果，
    通常省掉后续多次 WebFetch。
    """

    name = "qwen-native"

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        label: str = "",
    ) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = (api_key or "").strip()
        self.model = model
        self.label = label or model
        # 最近一次原生检索里模型给的摘要（由工具决定是否展示给上层）
        self.last_answer: str = ""

    def available(self) -> bool:
        return bool(self.base_url and self.api_key and self.model)

    async def search(
        self, query: str, *, max_results: int, timeout: float
    ) -> list[SearchResult]:
        if not self.available():
            raise SearchError("原生联网不可用（缺少 base_url / API Key / model）")
        self.last_answer = ""
        payload = json.dumps({
            "model": self.model,
            "input": query,
            "tools": [{"type": "web_search"}, {"type": "web_extractor"}],
        }).encode("utf-8")
        raw = await asyncio.to_thread(self._post, payload, timeout)
        return self._parse(raw, max_results)

    def _post(self, payload: bytes, timeout: float) -> bytes:
        request = urllib.request.Request(
            f"{self.base_url}/responses",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
                "User-Agent": USER_AGENT,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read(4 * 1024 * 1024)
        except urllib.error.HTTPError as exc:
            detail = exc.read(2000).decode("utf-8", "replace")
            if exc.code in (401, 403):
                raise SearchError(
                    f"原生联网鉴权失败（HTTP {exc.code}），请检查该 Provider 的 API Key"
                ) from exc
            if exc.code == 429:
                raise SearchError(
                    "原生联网被限流或额度不足（HTTP 429）", retryable=True
                ) from exc
            if "must be executed with web_search" in detail:
                raise SearchError("原生联网工具组合不被接受（需要 web_search + web_extractor）") from exc
            raise SearchError(f"原生联网请求失败（HTTP {exc.code}）") from exc
        except urllib.error.URLError as exc:
            raise SearchError(
                f"无法连接原生联网端点：{getattr(exc, 'reason', exc)}",
                retryable=True,
                unreachable=True,
            ) from exc
        except TimeoutError as exc:
            raise SearchError("原生联网请求超时", retryable=True, unreachable=True) from exc

    def _parse(self, raw: bytes, max_results: int) -> list[SearchResult]:
        try:
            data = json.loads(raw.decode("utf-8", "replace"))
        except json.JSONDecodeError as exc:
            raise SearchError("原生联网返回了无法解析的内容", retryable=True) from exc
        if isinstance(data, dict) and data.get("error"):
            err = data["error"]
            message = err.get("message") if isinstance(err, dict) else str(err)
            raise SearchError(f"原生联网返回错误：{str(message)[:200]}")

        items = data.get("output") if isinstance(data, dict) else None
        if not isinstance(items, list):
            return []

        seen: set[str] = set()
        results: list[SearchResult] = []
        queries: list[str] = []
        summary_parts: list[str] = []

        for item in items:
            if not isinstance(item, dict):
                continue
            kind = item.get("type")
            if kind in ("web_search_call", "web_extractor_call"):
                action = item.get("action") or {}
                q = str(action.get("query") or "").strip()
                if q and q not in queries:
                    queries.append(q)
                for src in action.get("sources") or []:
                    url = ""
                    if isinstance(src, dict):
                        url = str(src.get("url") or "").strip()
                    elif isinstance(src, str):
                        url = src.strip()
                    if not url.startswith(("http://", "https://")) or url in seen:
                        continue
                    seen.add(url)
                    results.append(
                        SearchResult(
                            # 不编造标题：只用 URL 里真实存在的信息做标识
                            title=_title_from_url(url),
                            url=url,
                            snippet="",
                            source=_domain_of(url),
                        )
                    )
            elif kind == "message":
                for block in item.get("content") or []:
                    if isinstance(block, dict) and block.get("type") == "output_text":
                        text = " ".join(str(block.get("text") or "").split())
                        if text:
                            summary_parts.append(text)

        if summary_parts:
            self.last_answer = "\n".join(summary_parts)[:2000]
        if queries:
            self.last_queries = queries[:5]

        return results[: max(1, max_results)]


def _title_from_url(url: str) -> str:
    """从 URL 推出一个**不含虚构成分**的展示名（域名 + 路径里最有信息量的一段）。"""
    parts = urllib.parse.urlsplit(url)
    host = _domain_of(url) or parts.netloc
    segments = [
        re.sub(r"\.(html?|php|aspx?|jsp)$", "", urllib.parse.unquote(s), flags=re.I)
        for s in parts.path.split("/")
        if s
    ]
    if not segments:
        return host
    last = segments[-1]
    # 有些站点用 /20260128/<hash>/c.html 这种无语义末段：不要拿 "c" 当标题，
    # 退回到路径里真实存在的日期（只展示 URL 里真实有的信息）。
    if len(last) < 3 or re.fullmatch(r"[0-9a-f]{8,}", last, flags=re.I):
        date = next(
            (s for s in reversed(segments) if re.fullmatch(r"20\d{6}", s)), ""
        )
        return f"{host} · {date}" if date else host
    label = last.replace("-", " ").replace("_", " ").strip()
    if not label or len(label) > 80:
        return host
    return f"{host} · {label}"


# ---------------------------------------------------------------------------
# Provider 选择
# ---------------------------------------------------------------------------


def build_provider(
    name: str,
    *,
    tavily_key: str = "",
    native: SearchProvider | None = None,
) -> SearchProvider:
    """按配置挑 Provider。

    优先级：显式指定 > 原生 > Tavily > **Bing** > DuckDuckGo。
    Bing 放在 DuckDuckGo 前面：DDG 在国内网络经常整个域名不可达（连续超时，
    表现就是"连不上搜索服务"），而 Bing RSS 实测国内可直连、且无需 Key。
    """
    choice = (name or "auto").strip().lower()

    if choice == "qwen-native":
        return native or TavilyProvider("")  # 返回一个 unavailable 的实例
    if choice == "tavily":
        return TavilyProvider(tavily_key)
    if choice == "bing":
        return BingProvider()
    if choice == "duckduckgo":
        return DuckDuckGoProvider()

    # auto：原生 > Tavily > Bing > DuckDuckGo
    if native is not None and native.available():
        return native
    tavily = TavilyProvider(tavily_key)
    if tavily.available():
        return tavily
    return BingProvider()


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


class Params(BaseModel):
    query: str = Field(
        min_length=1,
        description=(
            "Search keywords. Use natural language, include years/dates for "
            "time-sensitive topics (e.g. '2026 AI news', 'Python 3.14 what's new')."
        ),
    )
    max_results: int = Field(
        default=0,
        description="How many results to return (default from config, max 10).",
    )


class WebSearch(Tool):
    name = "WebSearch"
    description = (
        "Search the public web for up-to-date information (news, prices, schedules, "
        "releases, weather, product specs). Returns titles, URLs, snippets, site names "
        "and publish dates. Use it whenever the answer depends on current facts that "
        "may have changed after your training data — never guess live data. "
        "Then use WebFetch to read a result in full when the snippet is not enough."
    )
    params_model = Params
    category = "read"
    is_concurrency_safe = True

    def __init__(
        self,
        provider: SearchProvider | None = None,
        *,
        max_results: int = DEFAULT_MAX_RESULTS,
        timeout: float = DEFAULT_TIMEOUT,
        max_searches_per_turn: int = DEFAULT_MAX_SEARCHES_PER_TURN,
        max_retries: int = 0,
        enabled: bool = True,
        fallback_provider: SearchProvider | None = None,
        news_provider: SearchProvider | None = None,
    ) -> None:
        self.provider = provider or DuckDuckGoProvider()
        self.fallback_provider = fallback_provider
        # 时效性查询专用来源（带发布时间）；None = 不启用
        self.news_provider = news_provider
        self.max_results = max(1, min(int(max_results or DEFAULT_MAX_RESULTS), MAX_RESULTS_CAP))
        self.timeout = timeout
        self.max_searches_per_turn = max(
            1, min(int(max_searches_per_turn), MAX_SEARCHES_PER_TURN_CAP)
        )
        # 默认 0 次重试：搜索是计费/限流敏感的接口，让模型决定要不要换词重搜，
        # 比工具层盲目重试更划算。需要时可配置成 1–3 次。
        self.max_retries = max(0, min(int(max_retries or 0), 3))
        self.enabled = enabled
        # 每次用户发言重置的预算与缓存
        self._turn_searches = 0
        self._turn_queries: list[str] = []
        self._cache: dict[str, tuple[float, list[SearchResult]]] = {}
        self._seen_urls: set[str] = set()
        self.last_results: list[SearchResult] = []
        # 观测计数（测试与诊断用，不参与决策）
        self.cache_hits = 0
        self.budget_denials = 0
        self.fallback_used = 0
        # 连续「网络层连不上」的次数：达到上限后本轮不再尝试，直接告诉模型放弃
        self.unreachable_failures = 0
        # 「结果不相干」时惰性创建的备用搜索源
        self._alt_provider: SearchProvider | None = None

    # -- 预算 -------------------------------------------------------------
    def begin_turn(self) -> None:
        """新的一轮用户请求：重置次数预算与"已搜过的查询"。"""
        self._turn_searches = 0
        self._turn_queries = []
        self._seen_urls = set()
        self.cache_hits = 0
        self.budget_denials = 0
        self.fallback_used = 0
        self.unreachable_failures = 0

    @property
    def searches_this_turn(self) -> int:
        return self._turn_searches

    # -- 执行 -------------------------------------------------------------
    async def execute(self, params: Params) -> ToolResult:
        if not self.enabled:
            return ToolResult(
                output="联网搜索当前未启用（配置 web_search.enabled=false），请直接说明无法联网查询。",
                is_error=True,
            )
        if not self.provider.available():
            return ToolResult(
                output=(
                    "联网搜索不可用：服务端未配置可用的搜索来源"
                    "（TAVILY_API_KEY 或原生联网）。"
                    "请直接告诉用户暂时无法联网检索，不要编造实时信息。"
                ),
                is_error=True,
            )

        query = " ".join((params.query or "").split())
        if not query:
            return ToolResult(output="搜索关键词不能为空", is_error=True)

        wanted = params.max_results if params.max_results > 0 else self.max_results
        wanted = max(1, min(int(wanted), MAX_RESULTS_CAP))

        # 1) 同一轮内相同/等价 query：复用缓存，不消耗预算、不重复计费
        key = normalize_query(query)
        cached = self._cache.get(key)
        if cached and time.monotonic() - cached[0] < DEDUP_TTL_SECONDS:
            self.last_results = cached[1]
            self.cache_hits += 1
            return ToolResult(
                output=_format_results(
                    query,
                    cached[1],
                    cached_query=True,
                    note=self._budget_note(),
                )
            )

        # 2) 预算：超限直接拒绝，并明确要求停止继续搜索（不要反复撞墙）
        if self._turn_searches >= self.max_searches_per_turn:
            self.budget_denials += 1
            return ToolResult(
                output=BUDGET_EXHAUSTED_HINT.format(limit=self.max_searches_per_turn)
                + self._searched_queries_hint(),
                is_error=True,
            )

        # 3) 搜索源连续连不上：不再干等 timeout，直接告诉模型/用户真实原因
        if self.unreachable_failures >= UNREACHABLE_LIMIT:
            return ToolResult(
                output=UNREACHABLE_HINT.format(
                    provider=getattr(self.provider, "name", "unknown"),
                    count=self.unreachable_failures,
                ),
                is_error=True,
            )

        self._turn_searches += 1
        started = time.monotonic()
        last_error: SearchError | None = None
        results: list[SearchResult] = []
        used_news = False
        saw_success = False

        # 候选来源：时效性问题先试新闻源（带 pubDate），再回到常规搜索源。
        candidates: list[SearchProvider] = []
        if (
            self.news_provider is not None
            and self.news_provider.available()
            and looks_time_sensitive(query)
        ):
            candidates.append(self.news_provider)
        if self.provider not in candidates:
            candidates.append(self.provider)

        for candidate in candidates:
            provider_error: SearchError | None = None
            for attempt in range(self.max_retries + 1):
                try:
                    results = await candidate.search(
                        query, max_results=wanted, timeout=self.timeout
                    )
                    provider_error = None
                    break
                except SearchError as exc:
                    provider_error = exc
                    # 只对“可重试”错误重试（网络/5xx/限流），且次数有硬上限
                    if not exc.retryable or attempt >= self.max_retries:
                        break
                    await asyncio.sleep(0.5 * (attempt + 1))
                except Exception as exc:  # noqa: BLE001 - 网络层什么都可能抛
                    return ToolResult(
                        output=f"搜索失败：{exc}。请如实告知用户未取到实时信息。",
                        is_error=True,
                    )
            if provider_error is None:
                # 来源正常响应：空结果是"没搜到"，不是"搜索失败"
                saw_success = True
                if results:
                    last_error = None
                    used_news = candidate is self.news_provider
                    break
                continue
            last_error = provider_error
            results = []

        # 只要有任一来源正常响应过（哪怕都是空），就按"未找到结果"返回，而不是报错
        if saw_success and not results:
            last_error = None

        if last_error is not None:
            # 原生联网失败时兜底一次：不让用户因为 provider 抖动就拿不到任何信息
            fallback = self._fallback_provider()
            if fallback is not None:
                try:
                    results = await fallback.search(
                        query, max_results=wanted, timeout=self.timeout
                    )
                    last_error = None
                    self.fallback_used += 1
                except Exception:  # noqa: BLE001 - 兜底失败就按原错误返回
                    pass

        if last_error is not None:
            if last_error.unreachable:
                # 网络层连不上：累计到上限后本轮直接放弃，避免 N 次 15 秒干等
                self.unreachable_failures += 1
            return ToolResult(
                output=(
                    f"搜索失败：{last_error}。\n"
                    "请如实告诉用户这次没能联网检索，不要用记忆里的数据冒充最新信息。"
                    + (
                        "\n如果换个关键词再试仍然连不上，就只能说明联网检索不可用。"
                        if last_error.unreachable
                        else ""
                    )
                ),
                is_error=True,
            )

        elapsed = time.monotonic() - started
        results = _dedupe(results)[:wanted]
        switched_note = ""

        # 主搜索源"返回了结果但完全不相干"（典型：Bing RSS 对中文查询返回
        # "Windows 帮助中心"这类首页）或直接空结果时，自动换另一种无 Key 来源再试。
        # 宁可多一次请求，也不要让模型拿着无关网页编出一个看似有来源的答案。
        if (not results or _looks_irrelevant(query, results)):
            alternative = self._relevance_fallback()
            if alternative is not None:
                try:
                    alt = await alternative.search(
                        query, max_results=wanted, timeout=self.timeout
                    )
                    alt = _dedupe(alt)[:wanted]
                    if alt and not _looks_irrelevant(query, alt):
                        results = alt
                        switched_note = (
                            "（主搜索源未返回相关结果，已自动切换到"
                            f"「{getattr(alternative, 'name', '备用')}」）"
                        )
                        self.fallback_used += 1
                except Exception:  # noqa: BLE001 - 备用源失败不影响主结果
                    pass

        results = _rank_by_relevance(query, results)
        if used_news:
            switched_note = (
                "（时效性查询已优先使用新闻源 bing-news，结果含发布时间与媒体名）"
                + switched_note
            )
        # 相关性很弱时提前提醒模型：这些结果可能答非所问，别硬套
        best_score = max((_relevance_score(query, item) for item in results), default=0)
        if results and best_score < 2:
            switched_note += "（注意：结果与查询相关性较弱，请核对是否切题；不切题就换关键词或如实说明没查到）"
        self._cache[key] = (time.monotonic(), results)
        self.last_results = results
        self._turn_queries.append(query)
        self.unreachable_failures = 0  # 一旦成功，清空不可达计数

        if not results:
            return ToolResult(
                output=(
                    f"没有找到与「{query}」相关的网页结果（耗时 {elapsed:.1f}s）。"
                    "可以换一组关键词再试一次；仍然没有就如实说明查不到。"
                )
            )

        return ToolResult(
            output=_format_results(
                query,
                results,
                elapsed=elapsed,
                note=self._budget_note() + switched_note,
                answer=getattr(self.provider, "last_answer", "") or "",
            )
        )

    # -- 内部辅助 ---------------------------------------------------------
    def _budget_note(self) -> str:
        """最后一次允许的搜索要提前告知模型，避免下一轮撞上限。"""
        if self._turn_searches >= self.max_searches_per_turn:
            return LAST_SEARCH_NOTE
        left = self.max_searches_per_turn - self._turn_searches
        return f"（本轮还可搜索 {left} 次）" if left == 1 else ""

    def _searched_queries_hint(self) -> str:
        if not self._turn_queries:
            return ""
        shown = "、".join(f"「{q}」" for q in self._turn_queries[-4:])
        return f"\n本轮已搜过：{shown}，重复这些查询不会得到新信息。"

    def _fallback_provider(self) -> SearchProvider | None:
        """原生联网失败时的兜底（只用一次，避免成本翻倍）。"""
        if self.fallback_provider is None or self.provider is self.fallback_provider:
            return None
        if not self.fallback_provider.available():
            return None
        return self.fallback_provider

    def _relevance_fallback(self) -> SearchProvider | None:
        """"结果不相干"时换用的另一种无 Key 搜索源（惰性创建、每轮只一份）。

        Bing 的 RSS 输出对部分中文查询会返回完全无关的首页（实测：查询
        “2026年11月 人工智能 最新进展” 得到 Windows 帮助中心），此时换成
        DuckDuckGo lite 往往能拿到真正相关内容；反过来 DDG 被网络阻断时也不
        影响原本的 Bing 主路径。Tavily / 原生联网不参与这层切换。
        """
        if isinstance(self.provider, TavilyProvider):
            return None
        if getattr(self.provider, "name", "") not in {"bing", "duckduckgo"}:
            return None
        if self._alt_provider is None:
            if isinstance(self.provider, BingProvider):
                self._alt_provider = DuckDuckGoProvider()
            else:
                self._alt_provider = BingProvider()
        return self._alt_provider


def _dedupe(results: list[SearchResult]) -> list[SearchResult]:
    seen: set[str] = set()
    out: list[SearchResult] = []
    for item in results:
        url = item.url.strip()
        if not url or url in seen:
            continue
        seen.add(url)
        out.append(item)
    return out


# ---------------------------------------------------------------------------
# 相关性判定
# ---------------------------------------------------------------------------

_ASCII_TERM_RE = re.compile(r"[a-z0-9][a-z0-9\-]{1,}")
_CJK_SEQ_RE = re.compile(r"[\u4e00-\u9fff]+")


def _query_terms(query: str) -> set[str]:
    """把查询拆成可用于相关性比对的词元（英文单词 + 中文 bigram）。

    不引入分词依赖：中文 bigram 已足够识别"查询说的是什么主题"，
    又不会像单字那样把语气词、助词也当成命中。
    """
    text = (query or "").lower()
    terms = {t for t in _ASCII_TERM_RE.findall(text) if not t.isdigit()}
    for seq in _CJK_SEQ_RE.findall(text):
        if len(seq) == 1:
            terms.add(seq)
        else:
            terms.update(seq[i:i + 2] for i in range(len(seq) - 1))
    return terms


def _relevance_score(query: str, item: SearchResult) -> int:
    terms = _query_terms(query)
    if not terms:
        return 0
    haystack = f"{item.title} {item.snippet} {item.url}".lower()
    return sum(1 for term in terms if term in haystack)


def _looks_irrelevant(query: str, results: list[SearchResult]) -> bool:
    """所有结果都与查询毫无字面交集 → 判定为"搜索源给了不相干内容"。

    只用字面命中做判断，宁可漏判（仍然把结果给模型），也不误判：
    只有当**每一条**结果的相关性都为 0 时才算不相关。
    """
    if not results:
        return True
    return all(_relevance_score(query, item) == 0 for item in results)


def _rank_by_relevance(query: str, results: list[SearchResult]) -> list[SearchResult]:
    """按相关性稳定排序（同分保持原顺序），并把没有发布时间的条目排在后面。"""
    if len(results) <= 1:
        return results
    indexed = list(enumerate(results))
    indexed.sort(key=lambda pair: (-_relevance_score(query, pair[1]), 0 if pair[1].published else 1, pair[0]))
    return [item for _, item in indexed]


def _format_results(
    query: str,
    results: list[SearchResult],
    *,
    elapsed: float | None = None,
    cached_query: bool = False,
    note: str = "",
    answer: str = "",
) -> str:
    """把结果整理成紧凑、可追溯的文本（不把网页源码交给模型）。"""
    head = f'搜索结果（query: "{query}"'
    if elapsed is not None:
        head += f"，耗时 {elapsed:.1f}s"
    if cached_query:
        head += "，与本次查询相同，复用刚才的结果"
    head += f"，共 {len(results)} 条）："
    if note:
        head += " " + note

    lines = [head]
    # 原生联网会直接给出带引用的摘要：先把摘要放上来，再接来源清单
    if answer:
        lines.append("")
        lines.append("【原生检索摘要（由 provider 联网后生成，引用其下方来源）】")
        lines.append(answer.strip())
        lines.append("")
        lines.append("【检索命中的来源】")
    for index, item in enumerate(results, start=1):
        lines.append(f"{index}. {item.title}")
        lines.append(f"   URL: {item.url}")
        if item.source:
            lines.append(f"   站点: {item.source}")
        if item.published:
            lines.append(f"   发布时间: {item.published}")
        else:
            # 不能让模型把"没标日期"当成"今天刚发布"
            lines.append("   发布时间: 未标注（不得当作最新信息，引用时请说明未经核实）")
        if item.snippet:
            lines.append(f"   摘要: {item.snippet}")
    lines.append(
        "\n注意：以上是检索返回的公开信息，摘要可能过时或不完整；"
        "需要细节时用 WebFetch 打开原文核对。"
        "回答里必须给出至少一条完整可点击的来源链接（以 http:// 或 https:// 开头的原文 URL），"
        "并写明发布时间；没有发布时间的条目要说“未标注”，不要编造标题、时间或链接。"
    )
    return "\n".join(lines)


def build_native_provider(
    cfg: Any, llm_providers: list[Any] | None
) -> QwenNativeSearchProvider | None:
    """按配置构造原生联网 Provider。

    ``web_search.native_provider`` 指向 ``providers:`` 里某个 Provider 的 **name**，
    复用它的 base_url / model / API Key（Key 仍然来自环境变量，不复制）。
    """
    if not getattr(cfg, "native_enabled", False):
        return None
    name = (getattr(cfg, "native_provider", "") or "").strip()
    candidates = list(llm_providers or [])
    target = next((p for p in candidates if getattr(p, "name", "") == name), None)
    if target is None and name:
        return None
    if target is None:
        # 未指定时：找第一个百炼/通义的 OpenAI 兼容 provider
        for p in candidates:
            base = (getattr(p, "base_url", "") or "").lower()
            if "aliyuncs.com" in base or "dashscope" in base:
                target = p
                break
    if target is None:
        return None
    try:
        api_key = target.resolve_api_key()
    except Exception:  # noqa: BLE001 - 未配置 Key 时安静降级
        api_key = ""
    return QwenNativeSearchProvider(
        base_url=getattr(target, "base_url", ""),
        api_key=api_key,
        model=getattr(target, "model", ""),
        label=getattr(target, "name", ""),
    )


def build_web_search_tool(cfg: Any, llm_providers: list[Any] | None = None) -> WebSearch:
    """按 AppConfig.web_search 构造工具。

    优先级：``provider`` 显式指定 > 原生联网（若启用且可用）> Tavily > DuckDuckGo。
    原生失败时会用兜底 Provider 再试一次，保证用户至少拿到一条检索路径。
    """
    import os

    native = build_native_provider(cfg, llm_providers)
    provider = build_provider(
        getattr(cfg, "provider", "auto"),
        tavily_key=os.environ.get("TAVILY_API_KEY", ""),
        native=native,
    )

    fallback: SearchProvider | None = None
    if provider is not native:
        fallback = None
    elif os.environ.get("TAVILY_API_KEY", ""):
        fallback = TavilyProvider(os.environ["TAVILY_API_KEY"])
    else:
        # 原生联网抖动时用无需 Key 且国内可直连的 Bing 兜底
        fallback = BingProvider()

    return WebSearch(
        provider,
        max_results=getattr(cfg, "max_results", DEFAULT_MAX_RESULTS),
        timeout=getattr(cfg, "timeout", DEFAULT_TIMEOUT),
        max_searches_per_turn=getattr(
            cfg, "max_searches_per_turn", DEFAULT_MAX_SEARCHES_PER_TURN
        ),
        max_retries=getattr(cfg, "max_retries", 0) or 0,
        enabled=getattr(cfg, "enabled", True),
        fallback_provider=fallback,
        # 非原生来源一律附带新闻源：时效性问题能直接拿到带日期的真实新闻
        news_provider=BingNewsProvider() if provider is not native else None,
    )
