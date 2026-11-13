"""联网检索可靠性：相关性判定、无关结果自动切换、发布时间标注。

这些用例保护的是"不能拿无关网页/无日期内容冒充实时信息"这条产品约束。
"""

from __future__ import annotations

import pytest

from aieb.tools.web_fetch import extract_published_date
from aieb.tools.web_search import (
    BingProvider,
    DuckDuckGoProvider,
    SearchError,
    SearchProvider,
    SearchResult,
    WebSearch,
    _guess_published,
    _looks_irrelevant,
    _rank_by_relevance,
)

QUERY = "2026年11月 人工智能 最新进展"


def _bing_like() -> list[SearchResult]:
    """复现真实观测到的 Bing RSS 无关结果。"""
    return [
        SearchResult(title="How to get help in Windows - Microsoft Support",
                     url="https://support.microsoft.com/en-us/windows/how-to-get-help-in-windows",
                     snippet="Search for help on the taskbar, use the Tips app",
                     source="support.microsoft.com", published="Thu, 29 Oct 2026 03:53:00"),
        SearchResult(title="Windows help and support",
                     url="https://www.microsoft.com/en-us/windows/help-and-support",
                     snippet="Find the Windows help and support you need",
                     source="microsoft.com", published="Fri, 30 Oct 2026 15:47:00"),
    ]


def _relevant() -> list[SearchResult]:
    return [
        SearchResult(title="2026 年 11 月 AI 大事件盘点",
                     url="https://bigolab.com/news/archive/2026-11",
                     snippet="2026 年 11 月 AI 行业动态盘点：模型发布、产品更新",
                     source="bigolab.com"),
        SearchResult(title="人工智能最新新闻", url="https://example.com/ai",
                     snippet="人工智能 2026年11月 热点资讯", source="example.com"),
    ]


def test_irrelevant_results_are_detected() -> None:
    assert _looks_irrelevant(QUERY, _bing_like()) is True
    assert _looks_irrelevant(QUERY, _relevant()) is False
    assert _looks_irrelevant(QUERY, []) is True


def test_results_are_ranked_by_relevance_and_date() -> None:
    mixed = [
        SearchResult(title="Windows 帮助中心", url="https://a.example/x", snippet="help"),
        SearchResult(title="人工智能 2026年11月 动态", url="https://b.example/y", snippet=""),
        SearchResult(title="AI 2026年11月 人工智能", url="https://c.example/z", snippet="",
                     published="2026-11-05"),
    ]
    ranked = _rank_by_relevance(QUERY, mixed)
    assert "Windows" not in ranked[0].title, "无关结果必须排在最后"
    assert ranked[-1].title.startswith("Windows")
    # 同分时带发布时间的排前面
    assert ranked[0].published == "2026-11-05"


def test_guess_published_only_from_evidence() -> None:
    assert _guess_published("https://site.com/2026/11/03/post", "").startswith("2026-11-03")
    assert _guess_published("https://site.com/archive/2026-11", "").startswith("2026-11")
    assert _guess_published("https://site.com/a", "2026年11月3日 发布").startswith("2026-11-03")
    assert _guess_published("https://site.com/a", "没有日期") == "", "无依据时不得编造日期"


def test_extract_published_date_from_meta_and_jsonld() -> None:
    meta = '<meta property="article:published_time" content="2026-11-07T09:00:00+08:00">'
    assert extract_published_date(meta) == "2026-11-07"

    reversed_meta = ('<meta content="2026-10-31T10:00:00Z" '
                     'property="article:published_time">')
    assert extract_published_date(reversed_meta) == "2026-10-31"

    jsonld = '{"@type":"NewsArticle","datePublished":"2026-09-15T08:00:00"}'
    assert extract_published_date(jsonld) == "2026-09-15"

    time_tag = '<time datetime="2026-08-01">8 月 1 日</time>'
    assert extract_published_date(time_tag) == "2026-08-01"

    assert extract_published_date("<html><body>无日期</body></html>") == ""


class _FakeProvider:
    def __init__(self, name: str, results: list[SearchResult] | Exception) -> None:
        self.name = name
        self._results = results
        self.calls = 0

    def available(self) -> bool:
        return True

    async def search(self, query: str, *, max_results: int, timeout: float) -> list[SearchResult]:
        self.calls += 1
        if isinstance(self._results, Exception):
            raise self._results
        return list(self._results)


class _Params:
    def __init__(self, query: str, max_results: int = 5) -> None:
        self.query = query
        self.max_results = max_results


@pytest.mark.asyncio
async def test_tool_switches_source_when_results_are_irrelevant() -> None:
    primary = _FakeProvider("bing", _bing_like())
    alt = _FakeProvider("duckduckgo", _relevant())
    tool = WebSearch(primary, max_results=5, timeout=5)
    tool._alt_provider = alt  # 注入备用源，避免真实联网

    result = await tool.execute(_Params(QUERY))

    assert not result.is_error
    assert alt.calls == 1
    assert tool.fallback_used == 1
    assert "已自动切换到" in result.output
    assert "2026 年 11 月 AI 大事件盘点" in result.output
    assert "How to get help in Windows" not in result.output


@pytest.mark.asyncio
async def test_tool_keeps_primary_result_when_relevant() -> None:
    primary = _FakeProvider("bing", _relevant())
    alt = _FakeProvider("duckduckgo", _bing_like())
    tool = WebSearch(primary, max_results=5, timeout=5)
    tool._alt_provider = alt

    result = await tool.execute(_Params(QUERY))

    assert alt.calls == 0, "主源结果相关时不应多打一次备用源"
    assert "已自动切换" not in result.output


@pytest.mark.asyncio
async def test_missing_published_date_is_labelled() -> None:
    tool = WebSearch(_FakeProvider("bing", _relevant()), max_results=5, timeout=5)
    result = await tool.execute(_Params(QUERY))
    assert "发布时间: 未标注" in result.output
    assert "不得当作最新信息" in result.output


@pytest.mark.asyncio
async def test_search_failure_is_reported_honestly() -> None:
    primary = _FakeProvider("bing", SearchError("无法连接搜索服务：timed out", unreachable=True))
    tool = WebSearch(primary, max_results=3, timeout=1)
    # 备用源也失败：不允许返回"成功"
    tool._alt_provider = _FakeProvider("duckduckgo", SearchError("timeout", unreachable=True))

    result = await tool.execute(_Params("最新新闻"))
    assert result.is_error
    assert "搜索失败" in result.output
    assert "不要用记忆里的数据冒充最新信息" in result.output


def test_alt_provider_is_other_keyless_source() -> None:
    tool = WebSearch(BingProvider(), max_results=3, timeout=5)
    assert isinstance(tool._relevance_fallback(), DuckDuckGoProvider)

    tool2 = WebSearch(DuckDuckGoProvider(), max_results=3, timeout=5)
    assert isinstance(tool2._relevance_fallback(), BingProvider)


def test_provider_protocols_are_satisfied() -> None:
    # 静态类型契约：备用源必须实现 SearchProvider
    provider: SearchProvider = BingProvider()
    assert provider.available() is True


# ---------------------------------------------------------------------------
# 新闻源（时效性查询）
# ---------------------------------------------------------------------------

BING_NEWS_XML = """<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0" xmlns:News="http://www.bing.com/news/search">
<channel>
  <item>
    <title>OpenAI 融资目标下调</title>
    <link>http://www.bing.com/news/apiclick.aspx?ref=FexRss&amp;aid=&amp;url=https%3a%2f%2fwww.cnbeta.com.tw%2farticles%2ftech%2f1584516.htm</link>
    <description>融资目标从 300 亿下调至约 200 亿美元</description>
    <pubDate>Sat, 31 Oct 2026 08:40:00 GMT</pubDate>
    <News:Source>cnBeta.COM</News:Source>
  </item>
  <item>
    <title>没有真实链接的条目</title>
    <link>javascript:void(0)</link>
    <description>应被丢弃</description>
    <pubDate>Sat, 31 Oct 2026 08:40:00 GMT</pubDate>
  </item>
</channel>
</rss>"""


def test_parse_bing_news_keeps_real_url_date_and_source() -> None:
    from aieb.tools.web_search import _parse_bing_news

    results = _parse_bing_news(BING_NEWS_XML, 5)
    assert len(results) == 1, "非法链接必须被丢弃"
    item = results[0]
    assert item.url.startswith("https://www.cnbeta.com.tw/"), "必须解析出真实原文链接"
    assert "bing.com" not in item.url
    assert item.published.startswith("Sat, 31 Oct 2026")
    assert item.source == "cnBeta.COM"


def test_looks_time_sensitive() -> None:
    from aieb.tools.web_search import looks_time_sensitive

    assert looks_time_sensitive("请联网查一条最近的 AI 新闻")
    assert looks_time_sensitive("What's the latest Python release?")
    assert not looks_time_sensitive("解释一下 BM25 的原理")


@pytest.mark.asyncio
async def test_time_sensitive_query_uses_news_source_first() -> None:
    from aieb.tools.web_search import _parse_bing_news

    news_results = _parse_bing_news(BING_NEWS_XML, 5)
    news = _FakeProvider("bing-news", news_results)
    primary = _FakeProvider("bing", _relevant())
    tool = WebSearch(primary, max_results=5, timeout=5, news_provider=news)

    result = await tool.execute(_Params("最近 AI 新闻"))

    assert news.calls == 1
    assert primary.calls == 0, "新闻源有结果时不应再打常规搜索"
    assert "已优先使用新闻源" in result.output
    assert "Sat, 31 Oct 2026" in result.output


@pytest.mark.asyncio
async def test_news_source_empty_falls_back_to_web_search() -> None:
    news = _FakeProvider("bing-news", [])
    primary = _FakeProvider("bing", _relevant())
    tool = WebSearch(primary, max_results=5, timeout=5, news_provider=news)

    result = await tool.execute(_Params("最近 AI 新闻"))

    assert news.calls == 1
    assert primary.calls == 1, "新闻源无结果时应继续用常规搜索"
    assert "2026 年 11 月 AI 大事件盘点" in result.output
