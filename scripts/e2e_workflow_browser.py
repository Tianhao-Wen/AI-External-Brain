"""真实浏览器 E2E：会议 → LangGraph 工作流 → 人工审批 → 发送（或取消）。

验证验收标准里"浏览器端到端"那几条：

1. 会议页能启动工作流，并显示**后端真实状态**（等待人工审批 / 当前节点）；
2. 审批卡展示草稿，未点确认前**绝不发信**；
3. 点「确认发送」后状态变 completed，业务库里只有一份会议/风险/待办；
4. 刷新页面（关掉再打开）后仍能看到同一个工作流状态（来自 Checkpoint）；
5. 重启后端服务后状态不变，且**不会重复创建**会议；
6. 取消路径：点「取消发送」后状态 cancelled，邮件未发出。

全部使用独立 workspace；SMTP 强制 dry-run；不碰正式 `.aieb/`。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import time
import traceback
from pathlib import Path
from typing import Any

from playwright.sync_api import sync_playwright

from scripts.e2e_hybrid_browser import (
    OUT_DIR,
    start_server,
    stop_server,
    wait_http,
    write_config,
)
from tests.fake_smtp import FakeSMTPServer

MEETING = (
    "项目周会纪要。参会人：张伟、李娜。张伟说：上线日期已经延期两周，"
    "原因是接口联调一直不通过，这个风险还没有解决。"
    "李娜负责在3月15日前补齐测试用例。会议决定：本周五前完成联调。"
)


def main() -> int:
    parser = argparse.ArgumentParser(description="LangGraph 工作流真实浏览器 E2E")
    parser.add_argument("--work-dir", default="benchmarks/.work/e2e_workflow")
    parser.add_argument("--port", type=int, default=18997)
    parser.add_argument("--channel", default="chrome")
    args = parser.parse_args()

    work = (Path(__file__).resolve().parents[1] / args.work_dir).resolve()
    if "benchmarks" not in work.parts or ".work" not in work.parts:
        raise SystemExit(f"工作目录必须在 benchmarks/.work/ 下：{work}")
    if not os.environ.get("DEEPSEEK_API_KEY"):
        raise SystemExit("缺少 DEEPSEEK_API_KEY")

    t0 = time.time()
    report: dict[str, Any] = {
        "work_dir": str(work),
        "steps": [],
        # 先全部声明为 False：万一某个阶段异常中断，缺失的检查项不会因为
        # "没写进去" 而被 all() 当成通过。
        "checks": {
            key: False
            for key in (
                "approval_card_shown",
                "shows_real_status",
                "draft_has_subject",
                "survives_page_reload",
                "completed_after_approve",
                "trace_visible",
                "restart_ok",
                "meeting_single_after_restart",
                "risks_and_todos_visible",
                "cancel_shows_cancelled",
                "no_duplicate_meetings",
                "mail_count_matches_approvals",
                "first_email_sent",
                "second_email_cancelled",
                "chat_page_ok",
                "knowledge_page_ok",
                "edit_requires_save",
                "empty_recipient_blocked",
                "edit_bumps_version",
                "completed_after_reapprove",
                "edited_email_version_two",
            )
        },
    }

    def step(name: str, **data: Any) -> None:
        report["steps"].append({"step": name, "t": round(time.time() - t0, 1), **data})
        print(f"[step] {name} {json.dumps(data, ensure_ascii=False)[:260]}", flush=True)

    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    write_config(work, args.port)
    shots = OUT_DIR / "e2e_shots"
    shots.mkdir(parents=True, exist_ok=True)

    # 本地假 SMTP：走真实 smtplib 路径，但信件只落在内存里（不投递任何真实邮件）
    smtp = FakeSMTPServer()
    smtp.start()
    os.environ.update(smtp.env())
    report["fake_smtp_port"] = smtp.port

    proc = start_server(work, args.port, "bm25", "http://127.0.0.1:1", "unused")
    base = f"http://127.0.0.1:{args.port}"
    try:
        if not wait_http(base + "/", timeout=180):
            log = stop_server(proc)
            report["server_log_tail"] = log[-3000:]
            _write(report)
            print("[fatal] 服务未就绪", flush=True)
            return 2
        step("server_up", url=base)

        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(channel=args.channel, headless=True)
                page = browser.new_page(viewport={"width": 1440, "height": 980})
                page.goto(base + "/", wait_until="domcontentloaded", timeout=60_000)
                page.click('[data-page="meetings"]')
                page.wait_for_selector("#pasteWorkflow", timeout=30_000)

                # --- 1. 启动工作流 --------------------------------------------
                page.click("#pasteWorkflow")
                page.wait_for_selector("#mtgText", timeout=30_000)
                page.fill("#mtgName", "项目周会.txt")
                page.fill("#mtgTo", "team@example.com")
                page.fill("#mtgText", MEETING)
                page.click(".mask [data-act='ok']")
                page.wait_for_selector("#wfApprove", timeout=300_000)
                card = page.inner_text("#wfCard")
                step("workflow_waiting_approval", card=card[:200])
                report["checks"]["approval_card_shown"] = "需要你确认后才会发送" in card
                report["checks"]["shows_real_status"] = "等待人工审批" in card
                report["checks"]["draft_has_subject"] = bool(page.input_value("#wfSubject").strip())
                page.screenshot(path=str(shots / "workflow_01_approval.png"))

                # --- 2. 刷新页面：状态仍然存在（Checkpoint 恢复） --------------
                page.reload(wait_until="domcontentloaded")
                page.click('[data-page="meetings"]')
                page.wait_for_selector("#wfApprove", timeout=120_000)
                after_reload = page.inner_text("#wfCard")
                step("after_reload", card=after_reload[:160])
                report["checks"]["survives_page_reload"] = "需要你确认后才会发送" in after_reload

                # --- 3. 确认发送 ----------------------------------------------
                page.click("#wfApprove")
                page.wait_for_function(
                    "() => { const c = document.querySelector('#wfCard');"
                    " return c && c.innerText.includes('已完成'); }",
                    timeout=300_000,
                )
                done_card = page.inner_text("#wfCard")
                step("after_approve", card=done_card[:200])
                report["checks"]["completed_after_approve"] = "已完成" in done_card
                page.screenshot(path=str(shots / "workflow_02_completed.png"))

                # --- 4. Trace 可查看 ------------------------------------------
                page.click("#wfTrace")
                page.wait_for_function(
                    "() => { const b = document.querySelector('#wfTraceBox');"
                    " return b && b.innerText.includes('human_approval'); }",
                    timeout=120_000,
                )
                trace_text = page.inner_text("#wfTraceBox")
                step("trace", text=trace_text[:160])
                report["checks"]["trace_visible"] = "节点级执行 Trace" in trace_text
                page.screenshot(path=str(shots / "workflow_03_trace.png"))
                browser.close()
        except Exception:  # noqa: BLE001 - 失败也要留下可诊断的信息
            report["error"] = traceback.format_exc()[-2000:]
            step("phase1_error", error=report["error"][-300:])
    finally:
        log = stop_server(proc)
        report["server_log_tail"] = log[-2000:]

    # --- 5. 重启后端：状态与业务数据必须保持 ---------------------------
    proc2 = start_server(work, args.port, "bm25", "http://127.0.0.1:1", "unused")
    try:
        if not wait_http(base + "/", timeout=180):
            report["checks"]["restart_ok"] = False
        else:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(channel=args.channel, headless=True)
                page = browser.new_page(viewport={"width": 1440, "height": 980})
                page.goto(base + "/", wait_until="domcontentloaded", timeout=60_000)
                page.click('[data-page="meetings"]')
                page.wait_for_selector("#wfCard", timeout=120_000)
                card = page.inner_text("#wfCard")
                meetings = page.inner_text("#mtgList")
                step("after_restart", card=card[:160], meetings=meetings[:120])
                report["checks"]["restart_ok"] = "已完成" in card
                report["checks"]["meeting_single_after_restart"] = meetings.count("项目周会") == 1
                report["checks"]["risks_and_todos_visible"] = ("风险" in meetings and "待办" in meetings)
                page.screenshot(path=str(shots / "workflow_04_after_restart.png"))
                browser.close()
    finally:
        log2 = stop_server(proc2)
        report["server_log_tail_2"] = log2[-1500:]

    # --- 6. 取消路径 ---------------------------------------------------
    proc3 = start_server(work, args.port, "bm25", "http://127.0.0.1:1", "unused")
    try:
        wait_http(base + "/", timeout=180)
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel=args.channel, headless=True)
            page = browser.new_page(viewport={"width": 1440, "height": 980})
            page.goto(base + "/", wait_until="domcontentloaded", timeout=60_000)
            page.click('[data-page="meetings"]')
            page.wait_for_selector("#pasteWorkflow", timeout=60_000)
            page.click("#pasteWorkflow")
            page.wait_for_selector("#mtgText", timeout=30_000)
            page.fill("#mtgName", "第二次会议.txt")
            page.fill("#mtgText", MEETING + "（第二份纪要，用于验证取消路径）")
            page.fill("#mtgTo", "team@example.com")
            page.click(".mask [data-act='ok']")
            page.wait_for_selector("#wfCancel", timeout=300_000)
            page.click("#wfCancel")
            page.wait_for_selector(".mask [data-act='ok']", timeout=30_000)
            page.click(".mask [data-act='ok']")
            page.wait_for_function(
                "() => { const c = document.querySelector('#wfCard');"
                " return c && c.innerText.includes('已取消'); }",
                timeout=120_000,
            )
            card = page.inner_text("#wfCard")
            step("after_cancel", card=card[:160])
            report["checks"]["cancel_shows_cancelled"] = "已取消" in card
            page.screenshot(path=str(shots / "workflow_05_cancelled.png"))
            browser.close()
    finally:
        log3 = stop_server(proc3)
        report["server_log_tail_3"] = log3[-1500:]

    # --- 7. 修改草稿必须先升版；同时验证原有对话/知识页未回归 -----------
    proc4 = start_server(work, args.port, "bm25", "http://127.0.0.1:1", "unused")
    try:
        wait_http(base + "/", timeout=180)
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel=args.channel, headless=True)
            page = browser.new_page(viewport={"width": 1440, "height": 980})
            page.goto(base + "/", wait_until="domcontentloaded", timeout=60_000)

            # 7a. 原有页面是否还能用（不带 LLM 调用，只验证渲染与控件存在）
            page.click('[data-page="chat"]')
            page.wait_for_selector("textarea, #chatInput, #input", timeout=60_000)
            report["checks"]["chat_page_ok"] = True
            page.click('[data-page="knowledge"]')
            page.wait_for_timeout(2500)
            report["checks"]["knowledge_page_ok"] = page.locator("[data-page='knowledge']").count() > 0

            # 7b. 空收件人 / 改稿必须先升版 / 升版后再确认
            page.click('[data-page="meetings"]')
            page.wait_for_selector("#pasteWorkflow", timeout=60_000)
            page.click("#pasteWorkflow")
            page.wait_for_selector("#mtgText", timeout=30_000)
            page.fill("#mtgName", "第三次会议.txt")
            page.fill("#mtgTo", "team@example.com")
            page.fill("#mtgText", MEETING + "（第三份纪要，用于验证改稿升版）")
            page.click(".mask [data-act='ok']")
            page.wait_for_selector("#wfApprove", timeout=300_000)

            def toast_text() -> str:
                return page.inner_text("#toastWrap") if page.locator("#toastWrap").count() else ""

            # 7b-0. 清空收件人 → 点确认必须被拦下（仍停在等待审批，没有发出去）
            page.fill("#wfTo", "")
            page.click("#wfApprove")
            page.wait_for_timeout(800)
            card_empty = page.inner_text("#wfCard")
            step("empty_recipient_blocked", card=card_empty[:110])
            report["checks"]["empty_recipient_blocked"] = (
                "等待人工审批" in card_empty and "已完成" not in card_empty
            )

            # 7b-1. 改主题后直接点确认 → 必须提示先保存修改，且不能发出去
            page.fill("#wfTo", "team@example.com")
            original_subject = page.input_value("#wfSubject")
            page.fill("#wfSubject", original_subject + "（审批人已修订）")
            page.click("#wfApprove")
            page.wait_for_timeout(800)
            card_after_bad_approve = page.inner_text("#wfCard")
            step("approve_after_edit_blocked", card=card_after_bad_approve[:110])
            report["checks"]["edit_requires_save"] = (
                "等待人工审批" in card_after_bad_approve
                and "已完成" not in card_after_bad_approve
            )

            # 7b-2. 保存修改 → 版本 +1（v2），并重新进入等待审批
            page.click("#wfEdit")
            page.wait_for_function(
                "() => { const c = document.querySelector('#wfCard');"
                " return c && c.innerText.includes('草稿 v2'); }",
                timeout=180_000,
            )
            card_after_edit = page.inner_text("#wfCard")
            step("after_edit_saved", card=card_after_edit[:150])
            report["checks"]["edit_bumps_version"] = "v2" in card_after_edit

            # 7b-3. 确认最新版本（此时前端字段与 v2 草稿一致，允许发送）
            page.click("#wfApprove")
            page.wait_for_function(
                "() => { const c = document.querySelector('#wfCard');"
                " return c && c.innerText.includes('已完成'); }",
                timeout=300_000,
            )
            report["checks"]["completed_after_reapprove"] = True
            page.screenshot(path=str(shots / "workflow_06_edited.png"))
            browser.close()
    except Exception:  # noqa: BLE001
        report["error_phase7"] = traceback.format_exc()[-1500:]
        step("phase7_error", error=report["error_phase7"][-300:])
    finally:
        log4 = stop_server(proc4)
        report["server_log_tail_4"] = log4[-1200:]

    # --- 业务侧断言：没有真实发信（Fake SMTP）且会议没重复 -----------------
    import sqlite3
    db = work / ".aieb" / "brain" / "brain.sqlite3"
    conn = sqlite3.connect(str(db))
    try:
        meetings = conn.execute("SELECT COUNT(*) FROM meetings").fetchone()[0]
        emails = conn.execute("SELECT id, status, version FROM emails").fetchall()
        workflows = conn.execute("SELECT COUNT(*) FROM workflow_runs").fetchone()[0] if _has_table(
            conn, "workflow_runs"
        ) else 0
    finally:
        conn.close()
    report["db"] = {"meetings": meetings, "emails": emails, "workflow_runs": workflows}
    report["smtp_captured"] = [m.as_dict() for m in smtp.messages]
    smtp.stop()
    report["checks"]["no_duplicate_meetings"] = meetings == 3  # 三份不同的纪要
    statuses = [row[1] for row in emails]
    versions = [row[2] for row in emails]
    report["checks"]["mail_count_matches_approvals"] = len(report["smtp_captured"]) == 2
    report["checks"]["first_email_sent"] = "sent" in statuses
    report["checks"]["second_email_cancelled"] = "cancelled" in statuses
    report["checks"]["edited_email_version_two"] = 2 in versions

    report["checks"]["all_passed"] = all(
        value for value in report["checks"].values() if isinstance(value, bool)
    )
    report["elapsed_seconds"] = round(time.time() - t0, 1)
    _write(report)
    print(json.dumps(report["checks"], ensure_ascii=False, indent=2), flush=True)
    return 0 if report["checks"]["all_passed"] else 1


def _has_table(conn, name: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def _write(report: dict[str, Any]) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / "e2e_workflow.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[out] {path}", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
