"""JS 渲染兜底：用本机 Chrome / Edge（Playwright）读取「脚本渲染」的页面。

很多官方网站的正文是前端脚本渲染出来的 —— 例如中国货币网的 LPR 专栏，
静态 HTML 里只有空壳，``WebFetch`` 直接抓会得到「页面没有可提取的正文」，
于是模型只能如实回答「查不到」。这里在本机已装浏览器时补一次真实渲染，
把可见文本取回来。

约束（与 ``net_guard`` 保持一致）：

* 只有 Playwright 与本机浏览器都可用时才生效，否则 ``available()`` 为 False，
  上层沿用原来的静态读取，不引入硬依赖；
* 导航前后都校验地址，私有网段 / 云元数据地址一律拒绝；
* 只读可见文本，不登录、不带 Cookie、不填表单。

环境变量 ``AIEB_WEB_RENDER=0`` 可整体关闭这个兜底。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from aieb.tools import net_guard

# 依次尝试的浏览器通道（本机安装了哪个就用哪个）
_CHANNELS = ("chrome", "msedge")

# 常见安装位置：用于在真正启动浏览器前快速判断「有没有浏览器可用」，
# 避免每次 fetch 都付出一次失败的启动开销。
_WINDOWS_PATHS = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
)

_AVAILABLE: bool | None = None


class RenderUnavailable(RuntimeError):
    """本机没有可用的渲染能力（缺 Playwright 或浏览器）。"""


def _env_disabled() -> bool:
    value = (os.environ.get("AIEB_WEB_RENDER") or "").strip().lower()
    return value in {"0", "false", "no", "off"}


def _chrome_installed() -> bool:
    local = os.environ.get("LOCALAPPDATA") or ""
    candidates = list(_WINDOWS_PATHS)
    if local:
        candidates.append(str(Path(local) / "Google" / "Chrome" / "Application" / "chrome.exe"))
    return any(Path(p).is_file() for p in candidates)


def available() -> bool:
    """本机是否具备「渲染后再读」的能力（结果缓存，避免重复探测）。"""
    global _AVAILABLE
    if _AVAILABLE is not None:
        return _AVAILABLE
    if _env_disabled():
        _AVAILABLE = False
        return False
    try:
        import playwright  # noqa: F401  # 只探测是否安装
    except ImportError:
        _AVAILABLE = False
        return False
    _AVAILABLE = _chrome_installed() if os.name == "nt" else True
    return _AVAILABLE


@dataclass
class RenderResult:
    url: str        # 最终地址（跟随重定向之后）
    title: str
    text: str       # 渲染后的可见文本
    html: str


async def render(url: str, *, timeout: float = 20.0) -> RenderResult:
    """用本机浏览器渲染页面并返回可见文本。"""
    checked = net_guard.check_url(url)
    if not available():
        raise RenderUnavailable("本机没有可用的浏览器渲染能力")

    from playwright.async_api import async_playwright

    last_error: Exception | None = None
    async with async_playwright() as pw:
        browser = None
        for channel in _CHANNELS:
            try:
                browser = await pw.chromium.launch(channel=channel, headless=True)
                break
            except Exception as exc:  # noqa: BLE001 - 换下一个浏览器再试
                last_error = exc
        if browser is None:
            raise RenderUnavailable(f"无法启动本机浏览器：{last_error}")
        try:
            page = await browser.new_page(locale="zh-CN", viewport={"width": 1366, "height": 900})
            await page.goto(checked, wait_until="domcontentloaded", timeout=int(timeout * 1000))
            # 数据型页面（如 LPR）是异步拉接口填表：等网络空闲，等不到也不阻塞
            try:
                await page.wait_for_load_state("networkidle", timeout=min(8000, int(timeout * 1000)))
            except Exception:  # noqa: BLE001 - 超时不致命，下面照样取已有内容
                pass
            await page.wait_for_timeout(500)
            final_url = page.url or checked
            # 重定向后可能落到内网/元数据地址，必须再校验一次
            net_guard.check_url(final_url)
            title = ""
            try:
                title = (await page.title()) or ""
            except Exception:  # noqa: BLE001
                title = ""
            text = ""
            try:
                text = await page.evaluate(
                    "() => (document.body && document.body.innerText) || ''"
                )
            except Exception:  # noqa: BLE001
                text = ""
            html = ""
            try:
                html = await page.content()
            except Exception:  # noqa: BLE001
                html = ""
            return RenderResult(url=final_url, title=title.strip(), text=text or "", html=html)
        finally:
            await browser.close()
