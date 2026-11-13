"""阶段 A（会话状态与历史会话管理）的真实浏览器验收脚本。

覆盖用户真实反馈的问题：
1. 进入聊天 A → 切换会议中心 → 返回 → 仍是聊天 A，内容完整；
2. 刷新浏览器 → 聊天 A 及消息仍然存在；
3. 新建对话是**新建**而不是覆盖旧对话；点击历史会话能准确加载对应消息；
4. WebSocket 断线重连后不会重复渲染历史消息；
5. 重命名历史会话 → 刷新后标题仍在；
6. 删除历史会话 → 二次确认后不再出现，且不误删知识库/会议数据。

用法::

    python scripts/e2e_session_state.py --port 18889 [--headed]

依赖：playwright + 本机 Chrome；服务端需已启动且配置了真实模型 Key。
"""

from __future__ import annotations

import argparse
import sys
import traceback
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHOTS = ROOT / "docs" / "screenshots"
REPORT = ROOT / "docs" / "session_state_report.md"

steps: list[dict[str, object]] = []


def step(name: str, ok: bool, detail: str = "", shot: str = "") -> None:
    safe = "".join(ch if ch.isprintable() else " " for ch in str(detail))
    steps.append({"name": name, "ok": ok, "detail": safe, "shot": shot,
                  "at": datetime.now().strftime("%H:%M:%S")})
    print(f"[{'PASS' if ok else 'FAIL'}] {name} — {safe}", flush=True)


def shot(page, name: str) -> str:
    SHOTS.mkdir(parents=True, exist_ok=True)
    path = SHOTS / f"{name}.png"
    page.screenshot(path=str(path))
    return str(path.relative_to(ROOT)).replace("\\", "/")


def top_modal(page):
    return page.locator(".mask").last


def chat_text(page) -> str:
    return page.inner_text("#chatScroll")


def send_and_wait(page, prompt: str) -> None:
    page.fill("#composer", prompt)
    page.click("#sendBtn")
    page.wait_for_selector("#sendBtn:not([hidden])", timeout=300_000)
    page.wait_for_timeout(600)


def open_chat(page) -> None:
    page.click('.nav-item[data-page="chat"]')
    page.wait_for_selector("#chatScroll")
    page.wait_for_timeout(1500)


def run(page, base: str) -> None:
    # ── 0. 进入对话页，新建一个干净会话并产生真实内容 ──────
    page.goto(f"{base}/", wait_until="networkidle")
    open_chat(page)
    step("对话页可打开", page.is_visible("#composer"), "输入框可见")

    page.click("#newSession")
    page.wait_for_timeout(1200)
    prompt_a = "用一句话说明你能帮我做什么"
    send_and_wait(page, prompt_a)
    text_before = chat_text(page)
    step("发送消息并收到回复（会话 A）", prompt_a in text_before and len(text_before) > 40,
         f"{len(text_before)} 字符", shot(page, "session-01-chat-a"))

    # ── 1. 切到会议中心再回来：必须还是同一个会话 ───────────
    page.click('.nav-item[data-page="meetings"]')
    page.wait_for_timeout(900)
    open_chat(page)
    text_after = chat_text(page)
    step("切到会议中心再返回，原对话仍在", prompt_a in text_after,
         f"{len(text_after)} 字符", shot(page, "session-02-back-to-chat"))
    step("返回后不退回初始欢迎页",
         prompt_a in text_after and "我是你的 AI 外脑" not in text_after,
         "原提问与回答都在" if prompt_a in text_after else "退回欢迎页")

    # ── 2. 刷新浏览器：当前会话仍然完整 ─────────────────────
    page.reload(wait_until="networkidle")
    open_chat(page)
    reloaded = chat_text(page)
    step("刷新后当前会话与消息仍在", prompt_a in reloaded,
         f"{len(reloaded)} 字符", shot(page, "session-03-after-reload"))

    # ── 3. 新建对话 = 新会话；历史会话能准确切换 ────────────
    page.click("#newSession")
    page.wait_for_timeout(1200)
    step("新建对话后是空会话（不覆盖旧对话）", prompt_a not in chat_text(page),
         "新会话为空", shot(page, "session-04-new-session"))
    prompt_b = "会话 B 校验标记：只回一句「收到 B」"
    send_and_wait(page, prompt_b)
    text_b = chat_text(page)
    step("会话 B 有独立内容", prompt_b in text_b and prompt_a not in text_b,
         f"{len(text_b)} 字符")

    page.locator(".cs-item", has_text=prompt_a[:12]).first.click()
    page.wait_for_timeout(2200)
    back_a = chat_text(page)
    step("点击历史会话 A 准确加载 A 的消息",
         prompt_a in back_a and prompt_b not in back_a, f"{len(back_a)} 字符",
         shot(page, "session-05-switch-to-a"))

    # ── 4. WebSocket 断线重连：历史不能重复渲染 ────────────
    # 真实断开：调用页面里暴露的钩子关闭 WebSocket（等同网络瞬断），
    # 前端 onclose → 2 秒后自动重连 → 服务端重新下发历史。
    page.evaluate("() => window.__aiebDropSocket && window.__aiebDropSocket()")
    page.wait_for_timeout(1200)
    label = page.inner_text("#connLabel")
    step("断线时提示正在重连", "重连" in label or "断开" in label, label,
         shot(page, "session-06-reconnecting"))
    page.wait_for_function(
        "() => /已连接/.test(document.querySelector('#connLabel').textContent)",
        timeout=30_000,
    )
    page.wait_for_timeout(2500)
    after_reconnect = chat_text(page)
    step("重连后历史消息不重复渲染", after_reconnect.count(prompt_a) == 1,
         f"提问出现 {after_reconnect.count(prompt_a)} 次",
         shot(page, "session-07-reconnected"))

    # ── 5. 重命名：行内编辑 + 持久化 ───────────────────────
    active = page.locator(".cs-item.active")
    step("当前会话在侧边栏高亮", active.count() == 1, f"active={active.count()}")
    active.hover()
    active.locator("[data-rename]").click()
    page.wait_for_selector(".cs-input")
    new_title = "E2E 重命名-" + datetime.now().strftime("%H%M%S")
    page.fill(".cs-input", new_title)
    page.press(".cs-input", "Enter")
    page.wait_for_timeout(1000)
    step("重命名后侧边栏标题已更新", new_title in page.inner_text("#csList"),
         new_title, shot(page, "session-07-renamed"))

    page.reload(wait_until="networkidle")
    open_chat(page)
    step("刷新后重命名标题仍是新的", new_title in page.inner_text("#csList"),
         new_title, shot(page, "session-08-rename-persisted"))

    # ── 6. 删除：二次确认 + 不误删业务数据 ──────────────────
    stats_before = read_dashboard_stats(page)
    target = page.locator(".cs-item", has_text=new_title).first
    target.hover()
    target.locator("[data-del]").click()
    page.wait_for_selector(".mask")
    dialog_text = page.inner_text(".mask")
    step("删除前有二次确认弹窗", "删除这段对话" in dialog_text,
         dialog_text.replace("\n", " ")[:70], shot(page, "session-09-delete-confirm"))
    top_modal(page).locator(".modal-foot .btn[data-act='ok']").click()
    page.wait_for_timeout(2000)
    step("删除后会话不再出现在列表", new_title not in page.inner_text("#csList"),
         "已移除", shot(page, "session-10-after-delete"))

    stats_after = read_dashboard_stats(page)
    step("删除对话不影响知识库/会议/待办", stats_before == stats_after,
         f"{stats_before} -> {stats_after}")


def read_dashboard_stats(page) -> str:
    """读取工作台四个统计数字，用于确认删除对话没有波及业务数据。"""
    page.click('.nav-item[data-page="dashboard"]')
    page.wait_for_selector("#dashStats .stat", timeout=30_000)
    page.wait_for_timeout(600)
    text = page.inner_text("#dashStats").replace("\n", " ")
    page.click('.nav-item[data-page="chat"]')
    page.wait_for_timeout(800)
    return text


def write_report(base: str) -> None:
    failed = [s for s in steps if not s["ok"]]
    lines = [
        "# 会话状态与历史会话管理 · 浏览器验收报告",
        "",
        f"- 时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 目标：{base}",
        "- 方式：Playwright 驱动本机 Chrome（真实点击，不 mock 后端）",
        f"- 结果：**{len(steps) - len(failed)}/{len(steps)} 步通过**",
        "",
        "| # | 步骤 | 结果 | 细节 | 截图 |",
        "|---|---|---|---|---|",
    ]
    for i, s in enumerate(steps, start=1):
        shot_cell = f"[{Path(str(s['shot'])).name}]({s['shot']})" if s["shot"] else ""
        lines.append(
            f"| {i} | {s['name']} | {'PASS' if s['ok'] else 'FAIL'} | "
            f"{str(s['detail']).replace('|', '/')} | {shot_cell} |"
        )
    if failed:
        lines += ["", "## 失败项", ""]
        lines += [f"- {s['name']}：{s['detail']}" for s in failed]
    REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=18889)
    parser.add_argument("--headed", action="store_true")
    args = parser.parse_args()
    base = f"http://127.0.0.1:{args.port}"

    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=not args.headed)
        page = browser.new_page(viewport={"width": 1440, "height": 900}, locale="zh-CN")
        try:
            run(page, base)
        except Exception:  # noqa: BLE001 - 记录后继续产出报告
            step("脚本异常", False, traceback.format_exc()[-400:], shot(page, "session-99-error"))
        browser.close()

    write_report(base)
    failed = [s for s in steps if not s["ok"]]
    print(f"\n共 {len(steps)} 步，失败 {len(failed)} 步。报告：{REPORT}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
