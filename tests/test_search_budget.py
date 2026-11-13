"""Phase H：联网预算控制 + 千问原生联网的测试。

覆盖三件事：
1. **预算计数规则**（什么算 1 次、什么不算）——这是"一次新闻查询触发 5–7 次
   搜索"这个问题的根因，必须用测试锁死语义。
2. **预算耗尽的处理**：拒绝时必须明确要求模型停止检索，而不是硬报错。
3. **原生联网**：优先级、构造、解析、兜底，以及"不编造标题/时间"。

全部离线（Mock 掉 HTTP），不需要任何 API Key。
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from aieb.config import ProviderConfig, WebSearchConfig  # noqa: E402
from aieb.tools.web_fetch import (  # noqa: E402
    DEFAULT_MAX_FETCHES_PER_TURN,
    FETCH_BUDGET_HINT,
    MAX_FETCHES_PER_TURN_CAP,
    WebFetch,
)
from aieb.tools.web_search import (  # noqa: E402
    BUDGET_EXHAUSTED_HINT,
    DEFAULT_MAX_SEARCHES_PER_TURN,
    LAST_SEARCH_NOTE,
    MAX_SEARCHES_PER_TURN_CAP,
    UNREACHABLE_HINT,
    UNREACHABLE_LIMIT,
    BingProvider,
    DuckDuckGoProvider,
    QwenNativeSearchProvider,
    SearchError,
    SearchResult,
    TavilyProvider,
    WebSearch,
    build_native_provider,
    build_provider,
    build_web_search_tool,
    normalize_query,
)
from aieb.validator import validate_web_search  # noqa: E402

WEB = (REPO_ROOT / "aieb" / "web_content.py").read_text(encoding="utf-8")
PROMPTS = (REPO_ROOT / "aieb" / "prompts.py").read_text(encoding="utf-8")


def run(coro):
    return asyncio.run(coro)


class FakeProvider:
    name = "fake"

    def __init__(self, results=None, error: Exception | None = None):
        self.results = results if results is not None else make_results(2)
        self.error = error
        self.calls: list[str] = []

    def available(self) -> bool:
        return True

    async def search(self, query, *, max_results: int, timeout: float):
        self.calls.append(query)
        if self.error:
            raise self.error
        return list(self.results)[:max_results]


def make_results(n: int = 2) -> list[SearchResult]:
    return [
        SearchResult(
            title=f"结果 {i}",
            url=f"https://example.com/news/{i}",
            snippet=f"摘要 {i}",
            source="example.com",
        )
        for i in range(1, n + 1)
    ]


def js_function(name: str) -> str:
    start = WEB.index(f"function {name}(")
    rest = WEB[start + 1:]
    candidates = [
        pos for pos in (
            rest.find("\nfunction "),
            rest.find("\nconst "),
            rest.find("\nlet "),
        ) if pos > 0
    ]
    end = start + 1 + (min(candidates) if candidates else len(rest))
    return WEB[start:end]


# ---------------------------------------------------------------- 1. 预算默认值


def test_budget_defaults_are_configurable_and_bounded():
    """默认 6 次搜索 / 6 次读网页，硬上限 12 / 10。"""
    assert DEFAULT_MAX_SEARCHES_PER_TURN == 6
    assert MAX_SEARCHES_PER_TURN_CAP == 12
    assert DEFAULT_MAX_FETCHES_PER_TURN == 6
    assert MAX_FETCHES_PER_TURN_CAP == 10
    cfg = WebSearchConfig()
    assert cfg.max_searches_per_turn == 6
    assert cfg.max_fetches_per_turn == 6


def test_search_budget_clamped_to_cap():
    tool = WebSearch(FakeProvider(), max_searches_per_turn=999)
    assert tool.max_searches_per_turn == MAX_SEARCHES_PER_TURN_CAP
    tool = WebSearch(FakeProvider(), max_searches_per_turn=0)
    assert tool.max_searches_per_turn == 1


def test_fetch_budget_clamped_to_cap():
    tool = WebFetch(max_fetches_per_turn=999)
    assert tool.max_fetches_per_turn == MAX_FETCHES_PER_TURN_CAP


def test_validator_accepts_6_and_rejects_13():
    ok = validate_web_search({"max_searches_per_turn": 8})
    assert ok["max_searches_per_turn"] == 8
    with pytest.raises(Exception):
        validate_web_search({"max_searches_per_turn": 13})
    with pytest.raises(Exception):
        validate_web_search({"max_fetches_per_turn": 11})


def test_validator_native_fields():
    ok = validate_web_search({"native_enabled": True, "native_provider": " qwen3.8-max "})
    assert ok["native_enabled"] is True
    assert ok["native_provider"] == "qwen3.8-max"
    with pytest.raises(Exception):
        validate_web_search({"native_enabled": "yes"})
    # 默认关闭：不配置就完全走原有链路
    default = validate_web_search({})
    assert default["native_enabled"] is None  # 由 AppConfig 归一化成 False


# ---------------------------------------------------------------- 2. 计数规则


def test_distinct_queries_each_cost_one():
    provider = FakeProvider()
    tool = WebSearch(provider, max_searches_per_turn=3)
    for q in ("AI 新闻 2026", "OpenAI 发布", "国产大模型 进展"):
        run(tool.execute(tool.params_model(query=q)))
    assert tool.searches_this_turn == 3
    assert len(provider.calls) == 3
    assert tool.budget_denials == 0


def test_identical_query_hits_cache_and_does_not_cost_budget():
    """重复同一个查询走缓存：不消耗预算、不重复计费。"""
    provider = FakeProvider()
    tool = WebSearch(provider, max_searches_per_turn=2)
    run(tool.execute(tool.params_model(query="AI news 2026")))
    run(tool.execute(tool.params_model(query="  ai   NEWS 2026 ")))
    assert tool.searches_this_turn == 1
    assert tool.cache_hits == 1
    assert len(provider.calls) == 1


def test_cache_hit_output_says_reused():
    tool = WebSearch(FakeProvider(), max_searches_per_turn=2)
    run(tool.execute(tool.params_model(query="AI news")))
    second = run(tool.execute(tool.params_model(query="AI news")))
    assert "复用刚才的结果" in second.output


def test_near_duplicate_still_costs_budget():
    """近义改写不是同一个 query —— 这正是一次任务烧掉 5–7 次配额的原因。"""
    tool = WebSearch(FakeProvider(), max_searches_per_turn=6)
    for q in ("AI news", "AI news October 2026", "latest AI news", "AI 新闻 2026"):
        run(tool.execute(tool.params_model(query=q)))
    assert tool.searches_this_turn == 4


def test_retry_does_not_cost_extra_budget():
    """重试是同一次搜索内部的，不算新的预算单位（也不能无限放大计费）。"""
    provider = FakeProvider(error=SearchError("网络抖动", retryable=True))
    tool = WebSearch(provider, max_searches_per_turn=2, max_retries=2)
    result = run(tool.execute(tool.params_model(query="AI news")))
    assert result.is_error is True
    assert tool.searches_this_turn == 1
    assert len(provider.calls) == 3  # 首次 + 2 次重试，全部算 1 次预算


def test_failure_costs_budget():
    """失败的搜索也真的发过一次请求，所以计入预算。"""
    provider = FakeProvider(error=SearchError("下游 500"))
    tool = WebSearch(provider, max_searches_per_turn=2)
    run(tool.execute(tool.params_model(query="q1")))
    run(tool.execute(tool.params_model(query="q2")))
    assert tool.searches_this_turn == 2


def test_empty_results_cost_budget():
    provider = FakeProvider(results=[])
    tool = WebSearch(provider, max_searches_per_turn=3)
    run(tool.execute(tool.params_model(query="没有结果的主题")))
    assert tool.searches_this_turn == 1


# ---------------------------------------------------------------- 3. 上限后的行为


def test_over_budget_refuses_and_tells_model_to_stop():
    provider = FakeProvider()
    tool = WebSearch(provider, max_searches_per_turn=2)
    run(tool.execute(tool.params_model(query="q1")))
    run(tool.execute(tool.params_model(query="q2")))
    denied = run(tool.execute(tool.params_model(query="q3")))
    assert denied.is_error is True
    assert "搜索预算已达上限" in denied.output
    assert "不要再调用 WebSearch" in denied.output
    assert "立即基于已有搜索结果作答" in denied.output
    assert tool.budget_denials == 1
    assert len(provider.calls) == 2  # 第三次根本没发请求


def test_budget_denial_lists_searched_queries():
    tool = WebSearch(FakeProvider(), max_searches_per_turn=1)
    run(tool.execute(tool.params_model(query="AI 新闻 2026")))
    denied = run(tool.execute(tool.params_model(query="AI news 2026")))
    assert "本轮已搜过" in denied.output
    assert "重复这些查询不会得到新信息" in denied.output


def test_last_allowed_search_warns_quota_used_up():
    tool = WebSearch(FakeProvider(), max_searches_per_turn=2)
    first = run(tool.execute(tool.params_model(query="q1")))
    assert "本轮还可搜索 1 次" in first.output
    second = run(tool.execute(tool.params_model(query="q2")))
    assert LAST_SEARCH_NOTE in second.output


def test_begin_turn_resets_budget_and_counters():
    tool = WebSearch(FakeProvider(), max_searches_per_turn=1)
    run(tool.execute(tool.params_model(query="q1")))
    assert tool.searches_this_turn == 1
    tool.begin_turn()
    assert tool.searches_this_turn == 0
    assert tool.budget_denials == 0
    resumed = run(tool.execute(tool.params_model(query="q2")))
    assert resumed.is_error is False


def test_budget_hint_text_is_explicit():
    assert "不要再" in LAST_SEARCH_NOTE
    assert "上限" in BUDGET_EXHAUSTED_HINT
    # 预算是“软失败”分类的关镀：文案里必须能匹配到 BUDGET_DENIAL_RE
    for needle in ("已达上限", "不要再调用"):
        assert needle in BUDGET_EXHAUSTED_HINT
    for needle in ("已达上限", "不要再调用"):
        assert needle in FETCH_BUDGET_HINT


# ---------------------------------------------------------------- 4. 原生联网构造


def _qwen_provider(**over) -> ProviderConfig:
    base = dict(
        name="qwen3.8-max",
        protocol="openai",
        base_url="https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
        model="qwen3.8-max",
        api_key="${WD_TEST_QWEN_KEY}",
    )
    base.update(over)
    return ProviderConfig(**base)


def test_native_disabled_by_default():
    cfg = WebSearchConfig()
    assert build_native_provider(cfg, [_qwen_provider()]) is None


def test_native_provider_built_by_name(monkeypatch):
    monkeypatch.setenv("WD_TEST_QWEN_KEY", "sk-test")
    cfg = WebSearchConfig(native_enabled=True, native_provider="qwen3.8-max")
    native = build_native_provider(cfg, [_qwen_provider()])
    assert isinstance(native, QwenNativeSearchProvider)
    assert native.available() is True
    assert native.model == "qwen3.8-max"
    assert native.api_key == "sk-test"
    assert native.base_url.endswith("/compatible-mode/v1")


def test_native_unknown_provider_name_returns_none():
    cfg = WebSearchConfig(native_enabled=True, native_provider="不存在")
    assert build_native_provider(cfg, [_qwen_provider()]) is None


def test_native_auto_detects_aliyun_provider(monkeypatch):
    monkeypatch.setenv("WD_TEST_QWEN_KEY", "sk-test")
    cfg = WebSearchConfig(native_enabled=True, native_provider="")
    deepseek = ProviderConfig(
        name="deepseek-flash",
        protocol="openai",
        base_url="https://api.deepseek.com/v1",
        model="deepseek-chat",
        api_key="x",
    )
    native = build_native_provider(cfg, [deepseek, _qwen_provider()])
    assert native is not None and native.model == "qwen3.8-max"


def test_native_missing_key_is_unavailable(monkeypatch):
    monkeypatch.delenv("WD_TEST_QWEN_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    cfg = WebSearchConfig(native_enabled=True, native_provider="qwen3.8-max")
    native = build_native_provider(cfg, [_qwen_provider()])
    assert native is not None
    assert native.available() is False


# ---------------------------------------------------------------- 5. 优先级


def test_provider_priority_auto_prefers_native(monkeypatch):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    native = QwenNativeSearchProvider(
        base_url="https://x/aliyuncs.com/v1", api_key="k", model="m"
    )
    chosen = build_provider("auto", tavily_key="tvly-x", native=native)
    assert chosen is native


def test_provider_priority_auto_falls_back_without_native():
    chosen = build_provider("auto", tavily_key="tvly-x", native=None)
    assert isinstance(chosen, TavilyProvider)
    chosen = build_provider("auto", tavily_key="", native=None)
    # 没有任何 Key 时优先 Bing（国内可直连）；DuckDuckGo 需显式指定
    assert isinstance(chosen, BingProvider)


def test_provider_explicit_choice_wins_over_native():
    native = QwenNativeSearchProvider(
        base_url="https://x/aliyuncs.com/v1", api_key="k", model="m"
    )
    assert isinstance(build_provider("tavily", tavily_key="tvly-x", native=native), TavilyProvider)
    assert isinstance(
        build_provider("duckduckgo", tavily_key="tvly-x", native=native),
        DuckDuckGoProvider,
    )


def test_build_tool_attaches_fallback_only_for_native(monkeypatch):
    monkeypatch.setenv("WD_TEST_QWEN_KEY", "sk-test")
    cfg = WebSearchConfig(native_enabled=True, native_provider="qwen3.8-max")
    tool = build_web_search_tool(cfg, [_qwen_provider()])
    assert isinstance(tool.provider, QwenNativeSearchProvider)
    assert tool.fallback_provider is not None
    assert tool.fallback_provider is not tool.provider


def test_build_tool_without_native_has_no_fallback():
    cfg = WebSearchConfig(native_enabled=False)
    tool = build_web_search_tool(cfg, [])
    assert tool.fallback_provider is None


# ---------------------------------------------------------------- 6. 原生检索解析


NATIVE_PAYLOAD = {
    "output": [
        {
            "type": "web_search_call",
            "action": {
                "query": "AI 新闻 2026",
                "sources": [
                    {"url": "https://www.reuters.com/technology/ai-news-2026/"},
                    {"url": "https://example.org/posts/2026/10/ai-weekly.html"},
                ],
            },
        },
        {
            "type": "web_extractor_call",
            "action": {"query": "https://www.reuters.com/technology/ai-news-2026/"},
        },
        {
            "type": "message",
            "content": [{"type": "output_text", "text": "本周 AI 领域的真实进展是……"}],
        },
    ]
}


def test_native_parse_sources_and_summary():
    provider = QwenNativeSearchProvider(
        base_url="https://x/v1", api_key="k", model="m"
    )
    results = provider._parse(json.dumps(NATIVE_PAYLOAD).encode(), 5)
    assert [r.url for r in results] == [
        "https://www.reuters.com/technology/ai-news-2026/",
        "https://example.org/posts/2026/10/ai-weekly.html",
    ]
    assert provider.last_answer.startswith("本周 AI 领域")
    # 标题来自 URL 本身，不编造
    assert results[0].title.startswith("reuters.com")
    assert "ai news 2026" in results[0].title.lower()


def test_native_parse_drops_non_http_and_duplicates():
    payload = {
        "output": [
            {
                "type": "web_search_call",
                "action": {
                    "query": "q",
                    "sources": [
                        {"url": "javascript:alert(1)"},
                        {"url": "https://a.com/x"},
                        {"url": "https://a.com/x"},
                        {"url": "ftp://a.com/y"},
                    ],
                },
            }
        ]
    }
    provider = QwenNativeSearchProvider(base_url="https://x/v1", api_key="k", model="m")
    results = provider._parse(json.dumps(payload).encode(), 5)
    assert [r.url for r in results] == ["https://a.com/x"]


def test_native_parse_error_payload_raises():
    provider = QwenNativeSearchProvider(base_url="https://x/v1", api_key="k", model="m")
    with pytest.raises(SearchError):
        provider._parse(b'{"error": {"message": "quota exceeded"}}', 5)


def test_native_parse_unparsable_raises():
    provider = QwenNativeSearchProvider(base_url="https://x/v1", api_key="k", model="m")
    with pytest.raises(SearchError):
        provider._parse(b"<html>oops</html>", 5)


def test_native_unavailable_raises_clean_message():
    provider = QwenNativeSearchProvider(base_url="", api_key="", model="")
    with pytest.raises(SearchError) as exc:
        run(provider.search("q", max_results=3, timeout=1))
    assert "原生联网不可用" in str(exc.value)


def test_native_payload_contains_both_tools():
    """实测结论：只传 web_search 会被静默忽略，两个必须一起传。"""
    provider = QwenNativeSearchProvider(
        base_url="https://x/v1", api_key="k", model="m"
    )
    captured: dict = {}

    def fake_post(payload, timeout):
        captured["body"] = json.loads(payload.decode())
        return json.dumps(NATIVE_PAYLOAD).encode()

    provider._post = fake_post  # type: ignore[assignment]
    run(provider.search("AI 新闻 2026", max_results=3, timeout=5))
    tools = [t["type"] for t in captured["body"]["tools"]]
    assert tools == ["web_search", "web_extractor"]
    assert captured["body"]["model"] == "m"
    assert captured["body"]["input"] == "AI 新闻 2026"


# ---------------------------------------------------------------- 7. 原生失败兜底


def test_native_failure_falls_back_to_secondary_provider():
    class BrokenNative(QwenNativeSearchProvider):
        async def search(self, query, *, max_results, timeout):
            raise SearchError("原生联网被限流", retryable=True)

    native = BrokenNative(base_url="https://x/v1", api_key="k", model="m")
    fallback = FakeProvider(make_results(1))
    tool = WebSearch(native, fallback_provider=fallback, max_searches_per_turn=3)
    result = run(tool.execute(tool.params_model(query="AI news")))
    assert result.is_error is False
    assert "结果 1" in result.output
    assert tool.fallback_used == 1
    assert tool.searches_this_turn == 1  # 兜底不再额外计一次


def test_fallback_failure_returns_original_error():
    class Broken(QwenNativeSearchProvider):
        async def search(self, query, *, max_results, timeout):
            raise SearchError("原生联网被限流")

    class AlsoBroken(FakeProvider):
        def __init__(self):
            super().__init__(error=SearchError("DDG 也挂了"))

    tool = WebSearch(
        Broken(base_url="https://x/v1", api_key="k", model="m"),
        fallback_provider=AlsoBroken(),
        max_searches_per_turn=3,
    )
    result = run(tool.execute(tool.params_model(query="q")))
    assert result.is_error is True
    assert "原生联网被限流" in result.output
    assert "不要用记忆里的数据冒充" in result.output


# ---------------------------------------------------------------- 8. normalize_query


def test_normalize_query_case_space_punct():
    assert normalize_query("  AI  News,  October 2026 ") == "ai news october 2026"
    assert normalize_query("ＡＢＣ") == "ａｂｃ"


def test_normalize_query_keeps_year_distinct():
    assert normalize_query("AI news 2025") != normalize_query("AI news 2026")


# ---------------------------------------------------------------- 9. 展示层：软失败


def test_budget_denial_is_classified_as_soft_failure():
    classifier = js_function("execIsBudgetDenial")
    assert "BUDGET_DENIAL_RE" in classifier
    assert "execIsBudgetDenial" in js_function("execSummary")
    # 正则覆盖预算类拒绝的真实文案
    assert "已达上限|配额已用尽|不要再调用" in WEB
    assert "本轮.*上限" in WEB


def test_exec_summary_ignores_soft_failures():
    summary = js_function("execSummary")
    assert "execIsBudgetDenial" in summary
    assert "realFailed" in summary
    # 只有真实失败才渲染成「N 个步骤未完成」
    assert "realFailed.length + ' 个步骤未完成'" in summary


def test_exec_failure_text_has_human_copy_for_budget():
    hint = js_function("execFailureText")
    assert "检索次数已到本轮上限" in hint


def test_prompts_forbid_repeating_queries_and_refetching_native_links():
    assert "原生检索摘要" in PROMPTS
    assert "搜索预算已达上限" in PROMPTS
    assert "一次问清一个问题" in PROMPTS


def test_fetch_budget_hint_demands_stop():
    assert "不要再调用 WebFetch" in FETCH_BUDGET_HINT
    assert "读取预算已达上限" in FETCH_BUDGET_HINT
