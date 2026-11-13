"""Qdrant 不可用时的受控降级与错误提示（真实浏览器 + 真实服务）。

做法：把服务的 `AIEB_QDRANT_URL` 指向一个**没有监听的端口**，其余完全一样。
要验证的是四件事：

1. 上传不能被向量库故障阻塞（业务可用性优先）；
2. 提问仍然能给出基于关键词检索的回答（受控降级，而不是整页报错）；
3. 界面上必须**看得见**"检索已降级"的提示（不能静默降级）；
4. 失败原因要能读懂（不是裸堆栈）。

用法：
    python scripts/e2e_hybrid_degraded.py --work-dir benchmarks/.work/e2e_degraded --port 18993
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from scripts.e2e_hybrid_browser import (  # noqa: E402
    OUT_DIR,
    build_pdf,
    doc_count,
    start_server,
    stop_server,
    wait_http,
    write_config,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Qdrant 故障下的受控降级验证")
    parser.add_argument("--work-dir", default="benchmarks/.work/e2e_degraded")
    parser.add_argument("--port", type=int, default=18993)
    parser.add_argument("--dead-url", default="http://127.0.0.1:6399")
    parser.add_argument("--channel", default="chrome")
    args = parser.parse_args()

    work = (REPO / args.work_dir).resolve()
    if "benchmarks" not in work.parts or ".work" not in work.parts:
        raise SystemExit(f"工作目录必须在 benchmarks/.work/ 下：{work}")
    if not os.environ.get("DEEPSEEK_API_KEY"):
        raise SystemExit("缺少 DEEPSEEK_API_KEY")

    t0 = time.time()
    report: dict[str, Any] = {"mode": "hybrid", "dead_qdrant_url": args.dead_url,
                              "work_dir": str(work), "checks": {}, "steps": []}

    def step(name: str, **data: Any) -> None:
        report["steps"].append({"step": name, "t": round(time.time() - t0, 1), **data})
        print(f"[step] {name} {json.dumps(data, ensure_ascii=False)[:280]}", flush=True)

    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    write_config(work, args.port)
    pdf = build_pdf(work / "docs" / "采购管理制度.pdf", cap_wan=95)

    proc = start_server(work, args.port, "hybrid", args.dead_url, "e2e_degraded")
    base = f"http://127.0.0.1:{args.port}"
    shots = OUT_DIR / "e2e_shots"
    shots.mkdir(parents=True, exist_ok=True)
    try:
        if not wait_http(base + "/", timeout=120):
            log = stop_server(proc)
            report["server_log_tail"] = log[-3000:]
            _write(report)
            print("[fatal] 服务未就绪", flush=True)
            return 2

        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel=args.channel, headless=True)
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            page.goto(base + "/", wait_until="domcontentloaded", timeout=60_000)
            page.click('[data-page="knowledge"]')
            page.wait_for_selector("#docDrop", timeout=30_000)

            page.set_input_files("input[type=file]", str(pdf))
            page.wait_for_function(
                "() => document.querySelectorAll('#docList [data-doc]').length >= 1",
                timeout=180_000,
            )
            detail = page.evaluate("() => document.querySelector('#docDetail').innerText")
            step("upload_with_dead_qdrant", documents=doc_count(page), detail=detail[:160])
            report["upload_detail"] = detail
            report["checks"]["upload_not_blocked"] = (
                doc_count(page) == 1 and "3 页" in detail and "failed" not in detail.lower()
            )

            page.fill("#docQ", "单一份合同的采购金额超过多少万元需要两人共同签批？")
            page.click("#docAsk")
            page.wait_for_function(
                "() => { const b = document.querySelector('#docAnswer');"
                " return b && (b.querySelector('.banner.warn') || b.querySelector('.banner.err')"
                " || b.innerText.includes('引用来源')); }",
                timeout=180_000,
            )
            page.wait_for_timeout(800)
            view = page.evaluate(
                """() => {
                    const box = document.querySelector('#docAnswer');
                    const warn = box.querySelector('.banner.warn');
                    const err = box.querySelector('.banner.err');
                    return { text: box.innerText.trim(),
                             warn: warn ? warn.innerText.trim() : '',
                             err: err ? err.innerText.trim() : '' };
                }"""
            )
            page.screenshot(path=str(shots / "degraded_qdrant_down.png"))
            step("ask_with_dead_qdrant", warn=view["warn"][:120], err=view["err"][:120],
                 answered="引用来源" in view["text"])
            report["answer_view"] = view
            report["checks"]["controlled_degradation"] = (
                "95" in view["text"] and "引用来源" in view["text"]
            )
            report["checks"]["degradation_visible"] = bool(view["warn"]) and (
                "降级" in view["warn"]
            )
            report["checks"]["reason_readable"] = (
                bool(view["warn"]) and "Traceback" not in view["warn"]
            )
            browser.close()
    finally:
        log = stop_server(proc)
        report["server_log_tail"] = log[-2500:]

    report["checks"]["all_passed"] = all(
        v for v in report["checks"].values() if isinstance(v, bool)
    )
    report["elapsed_seconds"] = round(time.time() - t0, 1)
    _write(report)
    print(json.dumps(report["checks"], ensure_ascii=False, indent=2), flush=True)
    return 0 if report["checks"]["all_passed"] else 1


def _write(report: dict[str, Any]) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / "e2e_browser_degraded.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[out] {path}", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
