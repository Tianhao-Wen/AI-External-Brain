"""真实浏览器 Hybrid RAG 端到端验证（独立数据目录，绝不触碰正式 .aieb/）。

验证内容：
1. 起一个**独立工作目录**的服务（自带 .aieb/，配置里只写 `${DEEPSEEK_API_KEY}` 占位符，不落盘任何密钥）；
2. 用 Playwright 驱动**真实 Chrome** 打开工作台 → 知识库 → 上传 PDF；
3. 问三类问题：单页事实、相似编号陷阱、跨页多证据，核对**答案**与**页码引用**；
4. 重复上传同一份文件 → 确认**不重复生成向量记录**；
5. 上传修订版 → 确认检索结果随内容更新；
6. 删除文档 → 确认 SQLite、BM25、Qdrant 三处同步清除；
7. 全程截图 + 结构化结果落盘。

用法：
    python scripts/e2e_hybrid_browser.py --work-dir benchmarks/.work/e2e_hybrid --port 18988 --mode hybrid
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

OUT_DIR = REPO / "benchmarks" / "results" / "hybrid_rag"
FONT = r"C:\Windows\Fonts\msyh.ttc"

CONFIG_TEMPLATE = """\
providers:
  - name: deepseek-flash
    protocol: openai-compat
    base_url: https://api.deepseek.com
    model: deepseek-chat
    api_key: ${{DEEPSEEK_API_KEY}}
    display_name: DeepSeek

permission_mode: default

web_search:
  enabled: false
  provider: auto
  max_results: 5

remote:
  host: 127.0.0.1
  port: {port}
"""


# ---------------------------------------------------------------------------
# 测试语料（刻意包含相似编号、跨页条款）
# ---------------------------------------------------------------------------


def build_pdf(path: Path, *, cap_wan: int, code_owner: str = "李强") -> Path:
    """生成 3 页测试 PDF。`cap_wan` 变化用于验证"内容更新后检索跟着变"。"""
    import fitz  # PyMuPDF

    pages = [
        [
            "华东智造集团 采购管理制度（V1）",
            "第一条 单一合同审批权限",
            f"单一份合同的采购金额超过 {cap_wan} 万元时，须由部门负责人与财务负责人共同签批；",
            f"不超过 {cap_wan} 万元的，由部门负责人单独签批即可。",
            "第二条 质保金",
            "质保金比例为合同金额的 5%，验收合格后 30 日内退还。",
            "第三条 项目编号规则",
            "项目编号格式为 PRJ-年份-三位序号，例如 PRJ-2024-123。",
            f"项目 PRJ-2024-123 的负责人为{code_owner}，预算为 120 万元。",
        ],
        [
            "附件一 项目台账（节选）",
            "项目 PRJ-2024-1230 的负责人为张明，预算为 340 万元。",
            "项目 PRJ-2024-123 的负责人为李强，预算为 120 万元。",
            "注意：PRJ-2024-123 与 PRJ-2024-1230 是两个不同项目，请勿混淆。",
        ],
        [
            "附件二 交付与违约",
            "第一阶段验收应在合同签订后第 30 日完成。",
            "若交付逾期，违约金按合同金额的 0.5%／日计算，累计封顶比例为 10%。",
            "违约金的计算基数为合同金额，与第二条的质保金条款互不抵扣。",
        ],
    ]
    doc = fitz.open()
    for index, lines in enumerate(pages, start=1):
        page = doc.new_page(width=595, height=842)
        y = 72
        for line in lines:
            page.insert_text((56, y), line, fontsize=12, fontname="china-s")
            y += 26
        page.insert_text((56, 800), f"第 {index} 页 / 共 {len(pages)} 页", fontsize=9,
                         fontname="china-s")
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(path))
    doc.close()
    return path


# ---------------------------------------------------------------------------
# Qdrant 观测（只读）
# ---------------------------------------------------------------------------


def qdrant_snapshot(url: str, collection: str) -> dict[str, Any]:
    try:
        from qdrant_client import QdrantClient

        client = QdrantClient(url=url, timeout=5)
        if not client.collection_exists(collection):
            return {"exists": False, "points": 0, "documents": {}}
        total = client.count(collection_name=collection, exact=True).count
        docs: dict[str, int] = {}
        offset = None
        while True:
            records, offset = client.scroll(
                collection_name=collection,
                limit=512,
                offset=offset,
                with_payload=["document_id", "chunk_key"],
                with_vectors=False,
            )
            for record in records:
                payload = record.payload or {}
                key = str(payload.get("document_id") or "?")
                docs[key] = docs.get(key, 0) + 1
            if offset is None:
                break
        return {"exists": True, "points": total, "documents": docs}
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}


# ---------------------------------------------------------------------------
# 服务进程
# ---------------------------------------------------------------------------


def write_config(work: Path, port: int) -> Path:
    aieb_dir = work / ".aieb"
    aieb_dir.mkdir(parents=True, exist_ok=True)
    config = aieb_dir / "config.yaml"
    config.write_text(CONFIG_TEMPLATE.format(port=port), encoding="utf-8")
    return config


def start_server(work: Path, port: int, mode: str, qdrant_url: str, collection: str) -> subprocess.Popen:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO)
    env["AIEB_RETRIEVAL_MODE"] = mode
    env["AIEB_QDRANT_URL"] = qdrant_url
    env["AIEB_QDRANT_COLLECTION"] = collection
    env["AIEB_CONTEXT_BUDGET"] = "budgeted"
    env["AIEB_CONTEXT_MAX_TOKENS"] = "1800"
    # 复用已下载的模型文件（只避免重复下载 0.09GB，不影响任何被测行为）
    shared = REPO / "benchmarks" / ".work" / "emb_cache_cold"
    if (shared / "models").exists():
        env["AIEB_EMBEDDING_CACHE_DIR"] = str(shared)
    else:
        env.pop("AIEB_EMBEDDING_CACHE_DIR", None)
    cmd = [sys.executable, "-c", "from aieb.brain.cli import main; main()", "--port", str(port)]
    print(f"[stage] 启动服务 mode={mode} port={port} cwd={work}", flush=True)
    return subprocess.Popen(
        cmd, cwd=str(work), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
        errors="replace",
    )


def wait_http(url: str, timeout: float = 90.0) -> bool:
    import urllib.request

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                if response.status == 200:
                    return True
        except Exception:  # noqa: BLE001
            time.sleep(1.0)
    return False


def stop_server(proc: subprocess.Popen) -> str:
    try:
        proc.terminate()
        proc.wait(timeout=20)
    except Exception:  # noqa: BLE001
        proc.kill()
    out = ""
    if proc.stdout is not None:
        try:
            out = proc.stdout.read() or ""
        except Exception:  # noqa: BLE001
            out = ""
    return out


# ---------------------------------------------------------------------------
# 浏览器动作
# ---------------------------------------------------------------------------


def ask(page, question: str, timeout: int = 120_000) -> dict[str, Any]:
    page.fill("#docQ", question)
    page.click("#docAsk")
    page.wait_for_function(
        "() => { const b = document.querySelector('#docAnswer');"
        " return b && (b.innerText.includes('引用来源') || b.querySelector('.banner.err')); }",
        timeout=timeout,
    )
    return page.evaluate(
        """() => {
            const box = document.querySelector('#docAnswer');
            const chips = Array.from(box.querySelectorAll('.cite, .citation, [data-cite]'))
                .map(e => e.innerText.trim());
            return { text: box.innerText.trim(), cites: chips,
                     error: (box.querySelector('.banner.err') || {}).innerText || '' };
        }"""
    )


def doc_count(page) -> int:
    return page.evaluate("() => document.querySelectorAll('#docList [data-doc]').length")


def select_doc_by_name(page, needle: str) -> bool:
    """按文件名选中左侧文档（不依赖列表顺序）。"""
    return bool(
        page.evaluate(
            """(needle) => {
                const els = [...document.querySelectorAll('#docList [data-doc]')];
                const el = els.find(e => e.innerText.includes(needle));
                if(!el) return false;
                el.click();
                return true;
            }""",
            needle,
        )
    )


def answer_body(text: str) -> str:
    """只要回答正文，去掉后面的“引用来源”预览（预览里会出现其他段落文本）。"""
    return text.split("引用来源")[0]


def main() -> int:
    parser = argparse.ArgumentParser(description="真实浏览器 Hybrid RAG E2E")
    parser.add_argument("--work-dir", default="benchmarks/.work/e2e_hybrid")
    parser.add_argument("--port", type=int, default=18988)
    parser.add_argument("--mode", default="hybrid")
    parser.add_argument("--qdrant-url", default="http://127.0.0.1:6335")
    parser.add_argument("--collection", default="e2e_hybrid")
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--channel", default="chrome")
    parser.add_argument(
        "--session-check",
        action="store_true",
        help="额外验证对话会话历史恢复（需真实 LLM，多一次模型调用）",
    )
    args = parser.parse_args()

    work = (REPO / args.work_dir).resolve()
    if "benchmarks" not in work.parts or ".work" not in work.parts:
        raise SystemExit(f"工作目录必须在 benchmarks/.work/ 下，拒绝使用 {work}")
    if not os.environ.get("DEEPSEEK_API_KEY"):
        raise SystemExit("缺少 DEEPSEEK_API_KEY 环境变量（由调用方注入，不落盘）")

    report: dict[str, Any] = {
        "mode": args.mode,
        "uses_vector": args.mode != "bm25",
        "work_dir": str(work),
        "qdrant_url": args.qdrant_url,
        "collection": args.collection,
        "steps": [],
        "checks": {},
    }

    def step(name: str, **data: Any) -> None:
        entry = {"step": name, "t": round(time.time() - t0, 2), **data}
        report["steps"].append(entry)
        print(f"[step] {name} {json.dumps(data, ensure_ascii=False)[:300]}", flush=True)

    t0 = time.time()
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    write_config(work, args.port)

    pdf_v1 = build_pdf(work / "docs" / "采购管理制度.pdf", cap_wan=95)
    pdf_v2 = build_pdf(work / "docs" / "采购管理制度_v2.pdf", cap_wan=88)
    step("prepare", pdf_v1=str(pdf_v1), pdf_v2=str(pdf_v2), bytes=pdf_v1.stat().st_size)

    proc = start_server(work, args.port, args.mode, args.qdrant_url, args.collection)
    base = f"http://127.0.0.1:{args.port}"
    shots = OUT_DIR / "e2e_shots"
    shots.mkdir(parents=True, exist_ok=True)
    try:
        if not wait_http(base + "/", timeout=120):
            tail = stop_server(proc)
            report["checks"]["server_up"] = False
            report["server_log_tail"] = tail[-4000:]
            common_write(report, args.mode)
            print("[fatal] 服务未在 120s 内就绪，见 server_log_tail", flush=True)
            return 2
        report["checks"]["server_up"] = True
        step("server_up", url=base)

        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel=args.channel, headless=not args.headed)
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            console: list[str] = []
            page.on("console", lambda msg: console.append(f"{msg.type}: {msg.text}"))
            page.goto(base + "/", wait_until="domcontentloaded", timeout=60_000)
            page.click('[data-page="knowledge"]')
            page.wait_for_selector("#docDrop", timeout=30_000)
            step("open_knowledge")
            page.screenshot(path=str(shots / f"{args.mode}_01_knowledge.png"))

            # --- 1. 上传 -------------------------------------------------
            page.set_input_files("input[type=file]", str(pdf_v1))
            page.wait_for_function(
                "() => document.querySelectorAll('#docList [data-doc]').length >= 1",
                timeout=180_000,
            )
            n1 = doc_count(page)
            detail = page.evaluate(
                "() => (document.querySelector('#docDetail') || {}).innerText || ''")
            step("upload_v1", documents=n1, detail=detail[:200])
            page.screenshot(path=str(shots / f"{args.mode}_02_uploaded.png"))
            report["checks"]["upload_ok"] = n1 == 1 and "采购管理制度" in detail
            report["checks"]["pages_parsed_3"] = "3 页" in detail

            # --- 2. 三类问题 ---------------------------------------------
            questions = [
                ("单页事实", "单一份合同的采购金额超过多少万元需要两人共同签批？", ["95"]),
                ("相似编号", "项目 PRJ-2024-1230 的预算是多少万元？", ["340"]),
                ("跨页多证据", "交付逾期违约金的累计封顶比例，以及质保金比例分别是多少？", ["10", "5"]),
            ]
            answers: dict[str, Any] = {}
            for label, question, expect in questions:
                result = ask(page, question)
                answers[label] = {
                    "question": question, "answer": result["text"], "cites": result["cites"],
                    "error": result["error"],
                    "hit": [e for e in expect if e in result["text"]],
                    "has_citation": "第" in result["text"] and "页" in result["text"],
                }
                page.screenshot(path=str(shots / f"{args.mode}_03_{label}.png"))
                step("ask", label=label, hit=answers[label]["hit"],
                     citation=answers[label]["has_citation"], error=result["error"][:120])
            report["answers"] = answers
            report["checks"]["answer_and_citation"] = all(
                v["hit"] and v["has_citation"] and not v["error"] for v in answers.values()
            )
            code_body = answer_body(answers["相似编号"]["answer"])
            report["checks"]["similar_code_disambiguated"] = (
                "340" in code_body and "120" not in code_body
            )

            # --- 3. 重复上传（同字节）→ 不应产生重复向量 -------------------
            before = qdrant_snapshot(args.qdrant_url, args.collection)
            page.set_input_files("input[type=file]", str(pdf_v1))
            page.wait_for_timeout(6000)
            after = qdrant_snapshot(args.qdrant_url, args.collection)
            n2 = doc_count(page)
            step("reupload_identical", documents=n2, points_before=before.get("points"),
                 points_after=after.get("points"))
            report["reupload"] = {"before": before, "after": after, "documents": n2}
            # bm25 模式根本没有向量：只验证"文档"不重复，不冒充"向量"不重复
            key = "no_duplicate_vectors" if report["uses_vector"] else "no_duplicate_documents"
            report["checks"][key] = (
                n2 == 1 and before.get("points") == after.get("points")
            )

            # --- 4. 上传修订版 → 检索随内容变化 ---------------------------
            page.set_input_files("input[type=file]", str(pdf_v2))
            page.wait_for_timeout(2000)
            page.wait_for_function(
                "() => document.querySelectorAll('#docList [data-doc]').length >= 2",
                timeout=180_000,
            )
            selected_v2 = select_doc_by_name(page, "v2")
            page.wait_for_selector("#docQ", timeout=30_000)
            updated = ask(page, "单一份合同的采购金额超过多少万元需要两人共同签批？")
            updated_body = answer_body(updated["text"])
            step("upload_v2", documents=doc_count(page), selected_v2=selected_v2,
                 answer=updated_body[:160])
            report["update"] = {"documents": doc_count(page), "answer": updated["text"],
                                "selected_v2": selected_v2}
            report["checks"]["update_reflected"] = selected_v2 and "88" in updated_body
            page.screenshot(path=str(shots / f"{args.mode}_04_updated.png"))

            # --- 5. 删除旧版 → 三处同步清除，且检索不再命中旧内容 ------------
            before_del = qdrant_snapshot(args.qdrant_url, args.collection)
            assert select_doc_by_name(page, "管理制度.pdf"), "没找到旧版文档"
            page.click("#delDoc")
            page.wait_for_selector(".mask [data-act='ok']", timeout=30_000)
            page.click(".mask [data-act='ok']")
            page.wait_for_function(
                "() => document.querySelectorAll('#docList [data-doc]').length === 1",
                timeout=60_000,
            )
            page.wait_for_timeout(2500)
            after_del = qdrant_snapshot(args.qdrant_url, args.collection)
            # 删掉旧版后再问：必须只剩新版的 88，不能再出现 95
            select_doc_by_name(page, "v2")
            after_delete_answer = answer_body(ask(page, "单一份合同的采购金额超过多少万元需要两人共同签批？")["text"])
            step("delete", points_before=before_del.get("points"),
                 points_after=after_del.get("points"),
                 docs_after=after_del.get("documents"), answer=after_delete_answer[:120])
            report["delete"] = {"before": before_del, "after": after_del,
                                "answer_after_delete": after_delete_answer}
            report["checks"]["delete_synced"] = (
                (not report["uses_vector"] or after_del.get("points", 0) < before_del.get("points", 0))
                and len(after_del.get("documents") or {}) < len(before_del.get("documents") or {})
                and "88" in after_delete_answer and "95" not in after_delete_answer
            ) if report["uses_vector"] else (
                doc_count(page) == 1 and "88" in after_delete_answer
                and "95" not in after_delete_answer
            )
            page.screenshot(path=str(shots / f"{args.mode}_05_deleted.png"))

            # --- 6. 会话历史恢复（刷新后当前会话应重放） --------------------
            if args.session_check:
                probe = "请只回复四个字：收到确认"
                page.click('[data-page="chat"]')
                page.wait_for_selector("#composer", timeout=60_000)
                page.fill("#composer", probe)
                page.click("#sendBtn")
                page.wait_for_function(
                    "(q) => { const s = document.querySelector('#chatScroll');"
                    " return s && s.innerText.includes(q); }",
                    arg=probe,
                    timeout=180_000,
                )
                page.wait_for_function(
                    "() => { const b = document.querySelector('#sendBtn');"
                    " const s = document.querySelector('#stopBtn');"
                    " return b && !b.disabled && (!s || s.hidden); }",
                    timeout=180_000,
                )
                before_text = page.evaluate(
                    "() => (document.querySelector('#chatScroll') || {}).innerText || ''"
                )
                page.screenshot(path=str(shots / f"{args.mode}_06_session_before_reload.png"))
                page.reload(wait_until="domcontentloaded")
                page.click('[data-page="chat"]')
                page.wait_for_selector("#chatScroll", timeout=60_000)
                try:
                    page.wait_for_function(
                        "(q) => { const s = document.querySelector('#chatScroll');"
                        " return s && s.innerText.includes(q); }",
                        arg=probe,
                        timeout=60_000,
                    )
                    restored = True
                except Exception:  # noqa: BLE001 - 恢复失败要留证据而不是崩掉
                    restored = False
                after_text = page.evaluate(
                    "() => (document.querySelector('#chatScroll') || {}).innerText || ''"
                )
                page.screenshot(path=str(shots / f"{args.mode}_07_session_after_reload.png"))
                step("session_history", restored=restored,
                     before_len=len(before_text), after_len=len(after_text))
                report["session"] = {
                    "probe": probe,
                    "restored": restored,
                    "before_excerpt": before_text[-300:],
                    "after_excerpt": after_text[-300:],
                }
                report["checks"]["session_history_restored"] = restored
            report["console"] = console[-40:]
            browser.close()
    finally:
        log = stop_server(proc)
        report["server_log_tail"] = log[-3000:]

    report["checks"]["all_passed"] = all(
        v for k, v in report["checks"].items() if isinstance(v, bool)
    )
    report["elapsed_seconds"] = round(time.time() - t0, 1)
    common_write(report, args.mode)
    print(json.dumps(report["checks"], ensure_ascii=False, indent=2), flush=True)
    return 0 if report["checks"]["all_passed"] else 1


def common_write(report: dict[str, Any], mode: str) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / f"e2e_browser_{mode}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[out] {path}", flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
