"""真实邮件发送的浏览器验收：界面里走完 草稿 → 确认 → 真实发送。

前提：
  1) 服务端进程已带 SMTP 环境变量启动（AIEB_SMTP_*）；
  2) 数据库里至少有一次会议分析结果。

用法::

    python scripts/verify_email_ui.py --port 18889 --to you@example.com

成功后会在 docs/screenshots/17-email-sent.png 留下现场截图。
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHOTS = ROOT / "docs" / "screenshots"


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=18889)
    parser.add_argument("--to", required=True, help="真实收件地址")
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--yes", action="store_true",
                        help="确认明知会真实投递（不带此参数只打印提示并退出）")
    args = parser.parse_args()

    if os.environ.get("AIEB_EMAIL_DRY_RUN", "").strip().lower() in {"1", "true", "yes", "on"}:
        print("服务端处于发送保护模式（AIEB_EMAIL_DRY_RUN=1），不会真实投递。"
              "如需验证真实发送，请先取消该变量并重启服务。")
        return 3
    if not args.yes:
        print(f"本脚本会在界面上真实点击「确认发送」，收件人：{args.to}")
        print("确认要真实投递请加 --yes 重跑。")
        return 3

    from playwright.sync_api import sync_playwright

    base = f"http://127.0.0.1:{args.port}"
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=not args.headed)
        page = browser.new_page(viewport={"width": 1440, "height": 900}, locale="zh-CN")
        try:
            page.goto(f"{base}/", wait_until="networkidle")
            page.click('.nav-item[data-page="meetings"]')
            page.wait_for_selector(".doc-item[data-mtg]", timeout=60_000)
            page.locator(".doc-item[data-mtg]").first.click()
            page.wait_for_selector("#mtgEmail", timeout=60_000)
            page.click("#mtgEmail")

            page.wait_for_selector("#mailTo", timeout=30_000)
            page.fill("#mailTo", args.to)
            page.locator(".mask").last.locator(".modal-foot .btn[data-act='ok']").click()

            page.wait_for_selector("#pvTo", timeout=180_000)
            subject = page.input_value("#pvSubject")
            print(f"草稿已生成，主题：{subject}")

            page.locator(".mask").last.locator(".modal-foot .btn[data-act='ok']").click()
            page.wait_for_function(
                "() => /已发送|发送成功/.test(document.querySelector('#toastWrap').textContent)",
                timeout=120_000,
            )
            toast = page.inner_text("#toastWrap")
            SHOTS.mkdir(parents=True, exist_ok=True)
            shot = SHOTS / "17-email-sent.png"
            page.screenshot(path=str(shot))
            print(f"界面发送结果：{toast.strip()[:120]}")
            print(f"截图：{shot.relative_to(ROOT)}")
            print(f"时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
            return 0
        except Exception as exc:  # noqa: BLE001
            SHOTS.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(SHOTS / "17-email-failed.png"))
            print(f"界面真实发送失败：{exc}")
            print(f"截图：docs/screenshots/17-email-failed.png")
            return 1
        finally:
            browser.close()


if __name__ == "__main__":
    sys.exit(main())
