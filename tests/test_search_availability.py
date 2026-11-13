"""搜索源可用性：Bing RSS 兜底 + 连不上时快速失败。

背景（真实故障）：DuckDuckGo 的域名在国内网络整段不可达（TCP 443 都连不上），
`provider: auto` 回落到 DDG 后每次搜索都要干等 15 秒超时，一轮里连撞 3 次，
用户看到的就是"搜索失败：无法连接搜索服务：timed out"。
本文件锁死两件事：
1. 无 Key 时 `auto` 优先用国内可直连的 Bing RSS；
2. 搜索源连续连不上时，工具层快速失败并给出可执行的恢复建议，不再反复等超时。
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from aieb.tools.web_search import (  # noqa: E402
    UNREACHABLE_HINT,
    UNREACHABLE_LIMIT,
    BingProvider,
    DuckDuckGoProvider,
    SearchError,
    SearchResult,
    TavilyProvider,
    WebSearch,
    _parse_bing_rss,
    build_provider,
)
from aieb.validator import validate_web_search  # noqa: E402


def run(coro):
    return asyncio.run(coro)


BING_SAMPLE = """<?xml version="1.0" encoding="utf-8" ?>
<rss version="2.0"><channel>
<title>必应：上海外滩</title>
<item>
  <title>上海外滩 - 官方旅游信息</title>
  <link>https://www.meet-in-shanghai.net/attractions/the-bund</link>
  <description>外滩全天开放，夜间灯光 19:00 后最佳。</description>
  <pubDate>Thu, 22 Oct 2026 08:00:00 GMT</pubDate>
</item>
<item>
  <title><![CDATA[豫园 门票 &amp; 开放时间]]></title>
  <link>https://www.shanghai.gov.cn/yu-garden</link>
  <description><![CDATA[<b>9:00</b>-16:30，周一闭园。]]></description>
</item>
<item>
  <title>重复链接</title>
  <link>https://www.shanghai.gov.cn/yu-garden</link>
  <description>重复</description>
</item>
<item>
  <title>非 http 链接</title>
  <link>javascript:alert(1)</link>
  <description>bad</description>
</item>
</channel></rss>
"""


class UnreachableProvider:
    """模拟 DNS 被阻断/连接超时：每次都抛 unreachable 错误。"""

    name = "unreachable"

    def __init__(self) -> None:
        self.calls = 0

    def available(self) -> bool:
        return True

    async def search(self, query, *, max_results: int, timeout: float):
        self.calls += 1
        raise SearchError("无法连接搜索服务：timed out", retryable=True, unreachable=True)


# ---------------------------------------------------------------- 1. Bing 解析


def test_bing_rss_parsing():
    results = _parse_bing_rss(BING_SAMPLE, 5)
    assert [r.url for r in results] == [
        "https://www.meet-in-shanghai.net/attractions/the-bund",
        "https://www.shanghai.gov.cn/yu-garden",
    ]
    first = results[0]
    assert first.title == "上海外滩 - 官方旅游信息"
    assert "全天开放" in first.snippet
    assert first.source == "meet-in-shanghai.net"
    assert "2026" in first.published
    # CDATA + 实体 + 标签都要清掉
    assert results[1].title == "豫园 门票 & 开放时间"
    assert results[1].snippet == "9:00 -16:30，周一闭园。"


def test_bing_rss_limits_results():
    assert len(_parse_bing_rss(BING_SAMPLE, 1)) == 1


def test_bing_provider_always_available():
    assert BingProvider().available() is True
    assert BingProvider().name == "bing"


# ---------------------------------------------------------------- 2. provider 选择


def test_auto_prefers_bing_over_duckduckgo():
    assert isinstance(build_provider("auto", tavily_key=""), BingProvider)
    assert isinstance(build_provider("bing"), BingProvider)
    # 显式选择仍然尊重用户配置
    assert isinstance(build_provider("duckduckgo"), DuckDuckGoProvider)
    assert isinstance(build_provider("tavily", tavily_key="k"), TavilyProvider)


def test_validator_accepts_bing():
    assert validate_web_search({"provider": "bing"})["provider"] == "bing"


# ---------------------------------------------------------------- 3. 快速失败


def test_unreachable_failure_fast_fails_after_limit():
    provider = UnreachableProvider()
    tool = WebSearch(provider, max_searches_per_turn=6)
    tool._fallback_provider = lambda: None  # type: ignore[assignment]

    # 前 UNREACHABLE_LIMIT 次会真的去请求（每次都失败）
    for _ in range(UNREACHABLE_LIMIT):
        result = run(tool.execute(tool.params_model(query=f"q{provider.calls}")))
        assert result.is_error is True
        assert "搜索失败" in result.output
    assert provider.calls == UNREACHABLE_LIMIT
    assert tool.unreachable_failures == UNREACHABLE_LIMIT

    # 达到上限后：不再发起请求，直接给出可执行的恢复建议
    blocked = run(tool.execute(tool.params_model(query="再试一次")))
    assert blocked.is_error is True
    assert "联网检索不可用" in blocked.output
    assert "TAVILY_API_KEY" in blocked.output
    assert "bing" in blocked.output
    assert provider.calls == UNREACHABLE_LIMIT  # 没有第 3 次真实请求


def test_unreachable_hint_mentions_provider_and_count():
    hint = UNREACHABLE_HINT.format(provider="duckduckgo", count=2)
    assert "duckduckgo" in hint
    assert "2" in hint
    assert "不要编造实时信息" in hint


def test_success_resets_unreachable_counter():
    class FlakyProvider:
        name = "flaky"

        def __init__(self) -> None:
            self.n = 0

        def available(self) -> bool:
            return True

        async def search(self, query, *, max_results: int, timeout: float):
            self.n += 1
            if self.n == 1:
                raise SearchError("无法连接", retryable=True, unreachable=True)
            return [
                SearchResult(title="ok", url="https://example.com/a", snippet="s")
            ]

    tool = WebSearch(FlakyProvider(), max_searches_per_turn=6)
    tool._fallback_provider = lambda: None  # type: ignore[assignment]
    run(tool.execute(tool.params_model(query="q1")))
    assert tool.unreachable_failures == 1
    run(tool.execute(tool.params_model(query="q2")))
    assert tool.unreachable_failures == 0  # 成功后清零


def test_begin_turn_resets_unreachable_counter():
    tool = WebSearch(UnreachableProvider(), max_searches_per_turn=6)
    tool._fallback_provider = lambda: None  # type: ignore[assignment]
    for _ in range(UNREACHABLE_LIMIT):
        run(tool.execute(tool.params_model(query=f"q{tool.unreachable_failures}")))
    assert tool.unreachable_failures == UNREACHABLE_LIMIT
    tool.begin_turn()
    assert tool.unreachable_failures == 0


def test_non_network_error_does_not_trip_breaker():
    class ServerErrorProvider:
        name = "server-error"

        def available(self) -> bool:
            return True

        async def search(self, query, *, max_results: int, timeout: float):
            raise SearchError("搜索服务返回错误（HTTP 502）")

    tool = WebSearch(ServerErrorProvider(), max_searches_per_turn=6)
    tool._fallback_provider = lambda: None  # type: ignore[assignment]
    for _ in range(UNREACHABLE_LIMIT + 1):
        run(tool.execute(tool.params_model(query="q")))
    assert tool.unreachable_failures == 0
    # 不是网络问题：仍然允许继续尝试（每次都真的发了请求）
    result = run(tool.execute(tool.params_model(query="q2")))
    assert "502" in result.output


def test_search_error_flags():
    plain = SearchError("x")
    assert plain.unreachable is False and plain.retryable is False
    blocked = SearchError("x", retryable=True, unreachable=True)
    assert blocked.retryable is True and blocked.unreachable is True


def test_unreachable_error_message_is_actionable():
    """给模型/用户的文案必须说明"别再用同义词重试"。"""
    assert "不要再用不同关键词反复重试" in UNREACHABLE_HINT
    assert "web_search.provider" in UNREACHABLE_HINT
