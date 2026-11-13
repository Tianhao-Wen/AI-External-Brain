"""真实浏览器端到端验收（Playwright + 本机 Chrome）。

覆盖六个页面、上传/下载、弹窗、流式回答、刷新恢复、异常提示与响应式布局，
每步保存真实截图。不修改任何产品代码或测试断言来"通过"。

用法::

    aieb --port 18889                 # 另开终端启动服务
    python scripts/browser_e2e.py --port 18889

产物：
    docs/screenshots/*.png
    docs/browser_e2e_report.md
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHOTS = ROOT / "docs" / "screenshots"
REPORT = ROOT / "docs" / "browser_e2e_report.md"
SAMPLES = ROOT / "samples"

steps: list[dict[str, object]] = []
console_errors: list[str] = []
page_errors: list[str] = []


def step(name: str, ok: bool, detail: str = "", shot: str = "") -> None:
    safe = "".join(ch if ch.isprintable() or ch in "\n\t" else " " for ch in str(detail))
    steps.append({"name": name, "ok": ok, "detail": safe, "shot": shot,
                  "at": datetime.now().strftime("%H:%M:%S")})
    try:
        print(f"[{'PASS' if ok else 'FAIL'}] {name} — {safe}", flush=True)
    except UnicodeEncodeError:
        print(f"[{'PASS' if ok else 'FAIL'}] {name}", flush=True)


def top_modal(page):
    """当前最上层的弹窗（多层弹窗叠加时不能点到被遮挡的旧按钮）。"""
    return page.locator(".mask").last


def modal_button(page, act: str = "ok"):
    return top_modal(page).locator(f".modal-foot .btn[data-act='{act}']")


def shot(page, name: str) -> str:
    SHOTS.mkdir(parents=True, exist_ok=True)
    path = SHOTS / f"{name}.png"
    page.screenshot(path=str(path), full_page=False)
    return str(path.relative_to(ROOT)).replace("\\", "/")


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001 - 老版本 Python 没有 reconfigure
        pass
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=18889)
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--skip-llm", action="store_true", help="跳过需要真实模型的长步骤")
    parser.add_argument("--smtp-configured", action="store_true",
                        help="服务端已配置真实 SMTP，且本次要做真实投递验证"
                             "（必须同时给 --email-to；不给则一律只验证草稿）")
    parser.add_argument("--email-to", default="",
                        help="真实投递的收件地址（不填则不点击「确认发送」）")
    parser.add_argument("--knowledge-pdf", default=str(SAMPLES / "产品需求说明书.pdf"),
                        help="知识库上传的 PDF 路径")
    parser.add_argument("--knowledge-question", default="验收条款 C 对引用有什么要求？",
                        help="基于 PDF 提问的问题")
    parser.add_argument("--meeting-txt", default=str(SAMPLES / "会议记录_有风险.txt"),
                        help="会议中心上传的会议记录")
    parser.add_argument("--missing-txt", default=str(SAMPLES / "会议记录_信息缺失.txt"),
                        help="用于验证『信息缺失→待确认』的会议记录")
    args = parser.parse_args()
    if args.smtp_configured and not args.email_to:
        print("--smtp-configured 时必须用 --email-to 指定真实收件地址，避免发到 example.com")
        return 2
    base = f"http://127.0.0.1:{args.port}"

    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=not args.headed)
        page = browser.new_page(viewport={"width": 1440, "height": 900}, locale="zh-CN")
        page.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)
        page.on("pageerror", lambda e: page_errors.append(str(e)))

        try:
            _run(page, base, args.skip_llm, args.smtp_configured, args.email_to, shot,
                 Path(args.knowledge_pdf), args.knowledge_question,
                 Path(args.meeting_txt), Path(args.missing_txt))
        except Exception:  # noqa: BLE001 - 记录后继续产出报告
            step("脚本异常", False, traceback.format_exc()[-400:], shot(page, "99-script-error"))

        # 响应式：移动端布局
        try:
            page.set_viewport_size({"width": 390, "height": 844})
            page.goto(f"{base}/", wait_until="networkidle")
            page.wait_for_timeout(800)
            nav_dir = page.eval_on_selector(".sidebar", "el => getComputedStyle(el).flexDirection")
            overflow = page.evaluate(
                "() => document.documentElement.scrollWidth > window.innerWidth + 2"
            )
            step("响应式 · 390px 侧栏转横向导航", nav_dir == "row", f"flex-direction={nav_dir}",
                 shot(page, "20-mobile-dashboard"))
            step("响应式 · 无横向溢出", not overflow, f"horizontal_overflow={overflow}")
            page.click('.nav-item[data-page="chat"]')
            page.wait_for_timeout(500)
            step("响应式 · 移动端对话页可用", page.is_visible("#composer"),
                 shot=shot(page, "21-mobile-chat"))
            page.set_viewport_size({"width": 1440, "height": 900})
        except Exception:  # noqa: BLE001
            step("响应式检查", False, traceback.format_exc()[-300:])

        # 数据填报后的工作台：给 README 一张"有内容"的首页截图
        try:
            page.set_viewport_size({"width": 1440, "height": 900})
            page.goto(f"{base}/", wait_until="networkidle")
            page.wait_for_selector("#dashStats .stat")
            page.wait_for_timeout(800)
            stats_after = page.inner_text("#dashStats").replace("\n", " ")
            step("工作台 · 跑完流程后统计非零（README 截图）",
                 "已上传知识文档 0" not in stats_after, stats_after[:120],
                 shot(page, "01-dashboard-populated"))
        except Exception:  # noqa: BLE001
            step("工作台 · 跑完流程后统计非零（README 截图）", False,
                 traceback.format_exc()[-200:])

        # 控制台/页面错误（CDN 加载失败不算产品缺陷，单独列出）
        js_errors = [e for e in page_errors]
        step("无 JS 运行时异常", not js_errors, "; ".join(js_errors)[:300])
        if console_errors:
            step("浏览器控制台错误（含 CDN 网络错误）", False,
                 f"{len(console_errors)} 条，示例：{console_errors[-1][:160]}")

        browser.close()

    _write_report(base)
    failed = [s for s in steps if not s["ok"]]
    print(f"\n共 {len(steps)} 步，失败 {len(failed)} 步。报告：{REPORT}")
    return 1 if failed else 0


def _run(page, base: str, skip_llm: bool, smtp_configured: bool, email_to: str, shot,
         knowledge_pdf: Path, knowledge_question: str, meeting_txt: Path,
         missing_txt: Path) -> None:
    # ── 1. 工作台 ─────────────────────────────────────────────
    page.goto(f"{base}/", wait_until="networkidle")
    page.wait_for_selector(".brand-name")
    step("工作台 · 品牌", page.inner_text(".brand-name").strip() == "AI 外脑 Demo",
         page.inner_text(".brand-name"), shot(page, "01-dashboard"))
    step("工作台 · 六个导航入口", page.locator(".nav-item").count() == 6,
         str(page.locator(".nav-item").count()))
    page.wait_for_selector("#dashStats .stat")
    stats_text = page.inner_text("#dashStats")
    step("工作台 · 统计来自后端", all(k in stats_text for k in
                                       ("已上传知识文档", "已分析会议", "待完成任务", "未解决风险")),
         stats_text.replace("\n", " "))
    step("工作台 · 快捷卡片 4 张", page.locator(".quick").count() == 4,
         str(page.locator(".quick").count()))

    # ── 2. 知识库：上传真实 PDF ───────────────────────────────
    page.click('.nav-item[data-page="knowledge"]')
    page.wait_for_selector("#docDrop")
    with page.expect_file_chooser() as fc:
        page.click("#docDrop")
    fc.value.set_files(str(knowledge_pdf))
    page.wait_for_timeout(1500)
    page.wait_for_selector(".doc-item", timeout=60_000)
    step("知识库 · PDF 上传并解析", page.locator(".doc-item").count() >= 1,
         page.inner_text(".doc-item").replace("\n", " ")[:80], shot(page, "02-knowledge"))
    page.locator(".doc-item").first.click()
    page.wait_for_selector("#docQ")
    if not skip_llm:
        page.fill("#docQ", knowledge_question)
        page.click("#docAsk")
        page.wait_for_selector("#docAnswer .card, #docAnswer .banner", timeout=180_000)
        answer = page.inner_text("#docAnswer")
        step("知识库 · 基于 PDF 回答并给出页码",
             "第2页" in answer or "第 2 页" in answer or "来源" in answer,
             answer.replace("\n", " ")[:140], shot(page, "03-knowledge-answer"))

    # ── 3. 会议中心：上传有风险会议 → 风险/待办/邮件草稿 ─────
    page.click('.nav-item[data-page="meetings"]')
    page.wait_for_selector("#mtgDrop")
    with page.expect_file_chooser() as fc2:
        page.click("#mtgDrop")
    fc2.value.set_files(str(meeting_txt))
    page.wait_for_selector("#mtgDetail .risk-card", timeout=300_000)
    risks = page.locator("#mtgDetail .risk-card").count()
    todos = page.locator("#mtgDetail table.table tbody tr").count()
    header = page.inner_text("#mtgDetail .card-head h3")
    step("会议中心 · 风险与待办结构化展示", risks >= 1 and todos >= 1,
         f"{header} / 风险 {risks} / 待办 {todos}", shot(page, "04-meeting"))
    evidence = page.locator("#mtgDetail .risk-card .ev").first.inner_text()
    step("会议中心 · 风险保留原文依据", "原文依据" in evidence or "原文未提供" in evidence,
         evidence.replace("\n", " ")[:100])

    if not skip_llm:
        page.click("#mtgEmail")
        page.wait_for_selector("#mailTo")
        page.fill("#mailTo", email_to or "qa-demo@example.com")
        modal_button(page).click()
        page.wait_for_selector("#pvTo", timeout=180_000)
        subject = page.input_value("#pvSubject")
        step("会议中心 · 生成邮件草稿（未发送）", bool(subject),
             f"主题：{subject[:50]}", shot(page, "05-email-draft"))
        # 只有显式声明「服务端已配置 SMTP」并给出真实收件地址时才点击「确认发送」。
        # 否则一律取消：服务端若恰好配置了真实账号，误点一次就会真的投递出去。
        if smtp_configured and email_to:
            modal_button(page).click()
            page.wait_for_function(
                "() => /已发送|发送成功/.test(document.querySelector('#toastWrap').textContent)",
                timeout=180_000,
            )
            shown = page.inner_text("#toastWrap")
            step("会议中心 · 邮件真实发送成功（显式开关）", "已发送" in shown,
                 shown.replace("\n", " ")[:140], shot(page, "06-email-result"))
            if page.locator(".mask").count():
                modal_button(page, "cancel").click()
                page.wait_for_selector(".mask", state="detached", timeout=10_000)
        else:
            modal_button(page, "cancel").click()
            page.wait_for_selector(".mask", state="detached", timeout=10_000)
            step("会议中心 · 草稿确认页取消发送（未投递）", True,
                 "未点击「确认发送」，避免自动化验收误发真实邮件；"
                 "需要验证真实投递请用 --smtp-configured --email-to <地址>",
                 shot(page, "06-email-result"))

    # ── 4. 信息缺失/口径冲突会议：负责人与截止时间应为"待确认" ──
    prev_title = page.inner_text("#mtgDetail .card-head h3").strip()
    page.click("#pasteMeeting")
    page.wait_for_selector("#mtgText")
    page.fill("#mtgName", "预算评审与口径冲突（信息缺失）")
    page.fill("#mtgText", missing_txt.read_text(encoding="utf-8"))
    modal_button(page).click()
    # 等弹窗关闭（分析完成）且详情切到新会议。
    # 注意：标题由模型从正文提炼，不一定等于输入框里的名称，所以断言"标题变了"而不是断言关键字。
    page.wait_for_selector(".mask", state="detached", timeout=300_000)
    page.wait_for_function(
        "(prev) => { const el = document.querySelector('#mtgDetail .card-head h3');\n"
        "  return !!el && el.textContent.trim().length > 0 && el.textContent.trim() !== prev; }",
        arg=prev_title,
        timeout=60_000,
    )
    detail = page.inner_text("#mtgDetail")
    step("会议中心 · 缺失信息标记待确认", "待确认" in detail,
         "包含『待确认』" if "待确认" in detail else detail[:80],
         shot(page, "07-meeting-missing"))

    # ── 5. 待办事项：筛选与状态修改 ──────────────────────────
    page.click('.nav-item[data-page="todos"]')
    page.wait_for_selector("#todoBody table, #todoBody .empty", timeout=60_000)
    rows = page.locator("#todoBody table tbody tr").count()
    step("待办 · 列表展示", rows >= 1, f"{rows} 条", shot(page, "08-todos"))
    if rows:
        page.select_option("#todoBody select", "done")
        page.wait_for_timeout(1500)
        after = page.inner_text("#todoBody")
        step("待办 · 状态更新并持久化", "已完成" in after, "状态列显示已完成")
        # 刷新后仍是已完成（真实持久化）
        page.reload(wait_until="networkidle")
        page.click('.nav-item[data-page="todos"]')
        page.wait_for_selector("#todoBody table", timeout=60_000)
        step("待办 · 刷新后状态仍为已完成", "已完成" in page.inner_text("#todoBody"),
             "刷新后仍显示已完成", shot(page, "09-todos-after-reload"))

    # ── 6. 长期记忆：列表 + 搜索 + 删除确认弹窗（取消） ───────
    page.click('.nav-item[data-page="memory"]')
    page.wait_for_selector("#memBody .list-item, #memBody .empty", timeout=60_000)
    mem_count = page.locator("#memBody .list-item").count()
    step("长期记忆 · 条目来自真实会议", mem_count >= 1, f"{mem_count} 条",
         shot(page, "10-memory"))
    if mem_count:
        page.fill("#memSearch", "供应商")
        page.wait_for_timeout(300)
        filtered = page.locator("#memBody .list-item").count()
        step("长期记忆 · 搜索过滤生效", filtered >= 1, f"命中 {filtered} 条")
        page.locator("#memBody .btn.danger").first.click()
        page.wait_for_selector(".mask", timeout=10_000)
        step("长期记忆 · 删除需二次确认", page.is_visible(".modal"),
             "确认弹窗已出现", shot(page, "11-memory-confirm"))
        modal_button(page, "cancel").click()
        page.wait_for_timeout(300)

    # ── 7. AI 对话：流式 + 联网 + 刷新恢复 ────────────────────
    page.click('.nav-item[data-page="chat"]')
    page.wait_for_selector("#composer")
    page.fill("#composer", "用一句话说明你能帮我做什么。")
    page.click("#sendBtn")
    page.wait_for_selector("[data-streaming]", timeout=30_000)
    step("AI 对话 · 显示流式输出中", page.is_visible("#stopBtn"), "停止按钮已出现")
    page.wait_for_selector("#sendBtn:not([hidden])", timeout=180_000)
    answer = page.inner_text("#chatScroll .msg:last-child")
    step("AI 对话 · 得到真实模型回答", len(answer) > 10, answer.replace("\n", " ")[:100],
         shot(page, "12-chat"))
    step("AI 对话 · 不暴露工具英文名/JSON",
         "SearchKnowledge" not in page.inner_text("#chatScroll")
         and '"query"' not in page.inner_text("#chatScroll"))

    # 7b. 新建对话 → 跨会话长期记忆（验收项 4 的核心场景）
    if not skip_llm:
        page.click("#newSession")
        page.wait_for_timeout(1200)
        page.fill("#composer", "上一次供应商评审会的时间、参与人和主要决策是什么？")
        page.click("#sendBtn")
        page.wait_for_selector("#sendBtn:not([hidden])", timeout=300_000)
        mem_answer = page.inner_text("#chatScroll .msg:last-child")
        recalled = any(k in mem_answer for k in ("刘洋", "陈明", "供应商", "62%", "暂停"))
        step("长期记忆 · 新建对话可召回上次会议", recalled,
             mem_answer.replace("\n", " ")[:140], shot(page, "12b-memory-recall"))

    if not skip_llm:
        page.fill("#composer", "请联网查一条最近的 AI 相关新闻，给出标题、日期和链接。")
        page.click("#sendBtn")
        page.wait_for_selector("#sendBtn:not([hidden])", timeout=300_000)
        web_answer = page.inner_text("#chatScroll .msg:last-child")
        # 链接可能被渲染成 markdown 锚文本（"阅读原文"），所以要看 DOM 里的 href，
        # 不能只在内层文本里找 "http"
        web_links = page.eval_on_selector_all(
            "#chatScroll .msg:last-child a[href^='http']", "els => els.map(e => e.href)"
        )
        step("联网搜索 · 回答含真实链接", bool(web_links) or "http" in web_answer,
             (web_links[0] if web_links else web_answer.replace("\n", " ")[:120]),
             shot(page, "13-web-search"))

    # 刷新恢复（断言当前会话的历史，而不是之前那个被新建对话替换掉的会话）
    page.reload(wait_until="networkidle")
    page.click('.nav-item[data-page="chat"]')
    page.wait_for_timeout(2500)
    restored = page.inner_text("#chatScroll")
    step("AI 对话 · 刷新后历史恢复", "上一次供应商评审会" in restored,
         "当前会话历史已重放", shot(page, "14-chat-restored"))
    step("AI 对话 · 刷新后会话列表仍可见", page.locator(".cs-item").count() >= 1,
         f"{page.locator('.cs-item').count()} 个历史会话")

    # ── 8. PPT 生成与下载 ────────────────────────────────────
    if not skip_llm:
        page.click('.quick-chip[data-chip="ppt"]')
        page.wait_for_selector("#pptTopic")
        page.fill("#pptTopic", "供应商风险复盘汇报")
        page.fill("#pptSlides", "6")
        page.fill("#pptAudience", "管理层")
        modal_button(page).click()
        page.wait_for_selector("#chatScroll a[download]", timeout=420_000)
        link = page.locator("#chatScroll a[download]").last
        href = link.get_attribute("href")
        step("PPT · 生成并出现下载卡片", bool(href), href[:80] if href else "",
             shot(page, "15-ppt-card"))
        with page.expect_download(timeout=60_000) as dl:
            link.click()
        download = dl.value
        target = ROOT / "docs" / "e2e_download.pptx"
        download.save_as(str(target))
        head = target.read_bytes()[:2]
        step("PPT · 真实下载且为有效 pptx", head == b"PK" and target.stat().st_size > 20_000,
             f"{download.suggested_filename} / {target.stat().st_size} 字节")

    # ── 9. 异常提示：上传不支持的文件 ────────────────────────
    page.click('.nav-item[data-page="knowledge"]')
    page.wait_for_selector("#docDrop")
    bad = ROOT / "docs" / "not_supported.png"
    bad.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 100)
    with page.expect_file_chooser() as fc3:
        page.click("#docDrop")
    fc3.value.set_files(str(bad))
    # 提示条 4 秒后自动消失：等到"不支持"这条真正出现再读（不要被上一条 toast 误命中）
    page.wait_for_function(
        "() => document.querySelector('#toastWrap').textContent.includes('不支持')",
        timeout=60_000,
    )
    toast_text = page.inner_text("#toastWrap")
    step("异常处理 · 不支持的文件给出可读提示", "不支持" in toast_text or "失败" in toast_text,
         toast_text.replace("\n", " ")[:120], shot(page, "16-upload-error"))
    # 失败的上传不应被当成"已解析"的文档
    page.wait_for_timeout(600)
    parsed_bad = page.locator(".doc-item", has_text="not_supported").count()
    step("异常处理 · 失败文件不进入已解析列表", parsed_bad == 0,
         f"匹配到 {parsed_bad} 条 not_supported 记录")
    bad.unlink(missing_ok=True)


def _write_report(base: str) -> None:
    lines = [
        "# 真实浏览器 E2E 报告（Playwright + 本机 Chrome）",
        "",
        f"- 时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 地址：{base}",
        "- 截图目录：`docs/screenshots/`",
        "",
        "| 步骤 | 结果 | 证据 |",
        "|---|---|---|",
    ]
    for s in steps:
        evidence = str(s["detail"]).replace("|", "/")[:120]
        shot_cell = f"`{s['shot']}`" if s["shot"] else "—"
        lines.append(f"| {s['name']} | {'PASS' if s['ok'] else '**FAIL**'} | {evidence} {shot_cell} |")
    if page_errors:
        lines += ["", "## JS 异常", ""] + [f"- {e[:200]}" for e in page_errors]
    if console_errors:
        lines += ["", "## 控制台错误（可能含 CDN 网络问题）", ""] + [
            f"- {e[:200]}" for e in console_errors[-10:]
        ]
    passed = sum(1 for s in steps if s["ok"])
    lines += ["", f"**共 {len(steps)} 步，通过 {passed}，失败 {len(steps) - passed}。**", ""]
    REPORT.write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
