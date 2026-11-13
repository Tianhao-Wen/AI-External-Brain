"""AI 外脑部署冒烟测试（容器内外通用，零真实邮件、默认零 LLM 调用）。

它按"容器里跑真实业务流程"的顺序做端到端检查：

1. `GET /healthz`           —— 进程存活、版本、uptime
2. `GET /readyz`            —— 就绪：是否能连上 Qdrant（未配置向量库时允许 bm25 降级）
3. `GET /`                  —— 前端页面可返回
4. `GET /ws` 鉴权           —— 带错误 token 必须 401（证明鉴权没被部署改造绕过）
5. WebSocket RPC            —— 带 token 连接、调用 `ops.*` 拿真实业务状态
6. （可选 `--workflow`）    —— 启动 LangGraph 工作流 → 读到等待审批 → 取消（不发信）
7. （可选 `--rag`）         —— 知识库统计 + 一次检索调用（需要已上传文档/向量索引）

用法：
    python scripts/docker_smoke_test.py --url http://127.0.0.1:18888 --token "$AIEB_REMOTE_TOKEN"
    python scripts/docker_smoke_test.py --url http://127.0.0.1:18888 --workflow --rag

退出码：0 = 全部通过；1 = 有检查失败；2 = 环境不可达。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import ssl
import sys
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import urlsplit

RESULTS: list[dict[str, Any]] = []


def record(name: str, ok: bool, detail: str) -> None:
    RESULTS.append({"check": name, "ok": bool(ok), "detail": detail[:300]})
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail[:300]}", flush=True)


def _http(url: str, *, method: str = "GET", headers: dict[str, str] | None = None) -> tuple[int, bytes]:
    req = urllib.request.Request(url, method=method, headers=headers or {})
    # 本地/容器内通常无 TLS；但允许 https 目标
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=10, context=ctx) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except ssl.SSLError:
        with urllib.request.urlopen(req, timeout=10, context=ssl._create_unverified_context()) as resp:
            return resp.status, resp.read()


def check_http(base: str, token: str) -> None:
    try:
        status, body = _http(base + "/healthz")
        payload = json.loads(body.decode("utf-8", "replace") or "{}")
        record(
            "healthz",
            status == 200 and payload.get("ok") is True,
            f"HTTP {status} version={payload.get('version')} uptime={payload.get('uptime_s')}s",
        )
    except Exception as exc:  # noqa: BLE001
        record("healthz", False, f"{type(exc).__name__}: {exc}")
        return

    try:
        status, body = _http(base + "/readyz")
        payload = json.loads(body.decode("utf-8", "replace") or "{}")
        qdrant = payload.get("qdrant") or {}
        # 503 只在 Qdrant 不可达时出现；此时 bm25 仍可用，所以算"降级通过"而非失败
        downgraded = status == 503 and qdrant.get("ok") is False
        record(
            "readyz",
            status in {200, 503},
            f"HTTP {status} qdrant={qdrant.get('ok')} url={qdrant.get('url')}"
            + ("（降级：向量库不可达，bm25 仍可用）" if downgraded else ""),
        )
    except Exception as exc:  # noqa: BLE001
        record("readyz", False, f"{type(exc).__name__}: {exc}")

    try:
        status, body = _http(base + "/")
        html = body.decode("utf-8", "replace")
        record(
            "index_html",
            status == 200 and "AI 外脑" in html and len(html) > 10_000,
            f"HTTP {status} bytes={len(html)}",
        )
    except Exception as exc:  # noqa: BLE001
        record("index_html", False, f"{type(exc).__name__}: {exc}")

    # 鉴权：无 token 访问 /ws 必须 401（只有握手失败才算通过）
    try:
        status, _ = _http(base + "/ws")
        record("ws_requires_token", status in {401, 403}, f"HTTP {status}（期望 401/403）")
    except Exception as exc:  # noqa: BLE001
        record("ws_requires_token", False, f"{type(exc).__name__}: {exc}")

    # 错误 Origin 必须 403
    try:
        status, _ = _http(base + "/ws", headers={"Origin": "http://evil.example.com"})
        record("ws_rejects_bad_origin", status in {401, 403}, f"HTTP {status}（期望 403）")
    except Exception as exc:  # noqa: BLE001
        record("ws_rejects_bad_origin", False, f"{type(exc).__name__}: {exc}")


async def _rpc(base: str, token: str, op: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """按项目真实的 WebSocket 协议发一次业务请求。

    协议（以 aieb/remote.py + aieb/brain/ui/app_js.py 为准）：
      子协设：["aieb.v1", "aieb-token.<token>"]
      请求：{"type": "brain_request", "data": {"op": ..., "req_id": ..., ...参数}}
      响应：{"type": "brain_response", "data": {"req_id", "ok", "data"|"error"}}
    """
    import websockets

    ws_url = base.replace("http://", "ws://").replace("https://", "wss://") + "/ws"
    protocols = ["aieb.v1"]
    if token:
        protocols.append(f"aieb-token.{token}")
    req_id = f"smoke-{op}-{int(__import__('time').time() * 1000)}"
    async with websockets.connect(
        ws_url,
        subprotocols=protocols,
        origin=base,
        open_timeout=15,
        max_size=32 * 1024 * 1024,
    ) as ws:
        request = {"op": op, "req_id": req_id}
        request.update(payload or {})
        await ws.send(json.dumps({"type": "brain_request", "data": request}, ensure_ascii=False))
        deadline = asyncio.get_event_loop().time() + 240
        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                raise TimeoutError(f"{op} 超时")
            raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
            msg = json.loads(raw)
            data = msg.get("data") or {}
            if msg.get("type") == "brain_response" and data.get("req_id") == req_id:
                if not data.get("ok"):
                    raise RuntimeError(str(data.get("error") or "未知错误"))
                return data.get("data") or {}
            # 其它事件（connected / stream 等）忽略


def check_ws(base: str, token: str) -> None:
    try:
        data = asyncio.run(_rpc(base, token, "health"))
        record(
            "ws_connect_with_token",
            data.get("ok") is True,
            f"health → documents={data.get('documents')} mail_configured={(data.get('mail') or {}).get('configured')}",
        )
    except Exception as exc:  # noqa: BLE001
        record("ws_connect_with_token", False, f"{type(exc).__name__}: {exc}")


def check_rag(
    base: str, token: str, *, upload: bool = False, qdrant_url: str = "", min_docs: int = 0
) -> None:
    try:
        data = asyncio.run(_rpc(base, token, "documents.list"))
        docs = data.get("documents") or []
        record(
            "rag_documents_list",
            isinstance(docs, list) and len(docs) >= min_docs,
            f"documents={len(docs)}（要求 ≥ {min_docs}）",
        )
    except Exception as exc:  # noqa: BLE001
        record("rag_documents_list", False, f"{type(exc).__name__}: {exc}")
        return

    if not upload:
        return

    # 生成一份合成 PDF（不包含任何真实企业/个人资料）
    try:
        import base64

        try:  # pymupdf 1.24+ 推荐模块名，fitz 会打弃用告警（污染 CI 的 stderr）
            import pymupdf as fitz
        except ImportError:  # pragma: no cover
            import fitz  # type: ignore[no-redef]

        doc = fitz.open()
        page = doc.new_page()
        page.insert_text(
            (60, 90),
            "\u90e8\u7f72\u9a8c\u6536\u6837\u4f8b\u6587\u6863\uff08\u865a\u6784\uff09\n\n"
            "1. \u5dee\u65c5\u62a5\u9500\u6807\u51c6\uff1a\u4e00\u7ebf\u57ce\u5e02\u4f4f\u5bbf\u4e0a\u9650\u6bcf\u665a 600 \u5143\u3002\n"
            "2. \u5ba1\u6279\u6d41\u7a0b\uff1a\u5355\u6b21\u62a5\u9500\u8d85\u8fc7 5000 \u5143\u9700\u90e8\u95e8\u8d1f\u8d23\u4eba\u5ba1\u6279\u3002\n"
            "3. \u53d1\u7968\u8981\u6c42\uff1a\u9700\u63d0\u4f9b\u589e\u503c\u7a0e\u4e13\u7528\u53d1\u7968\uff0c\u5f00\u7968\u65e5\u671f\u4e0d\u5f97\u65e9\u4e8e\u51fa\u5dee\u65e5\u671f\u3002\n"
            "4. \u6d41\u7a0b\u65f6\u6548\uff1a\u62a5\u9500\u5355\u9700\u5728\u51fa\u5dee\u7ed3\u675f\u540e 15 \u4e2a\u5de5\u4f5c\u65e5\u5185\u63d0\u4ea4\u3002",
            fontsize=12,
            fontname="china-s",
        )
        pdf_bytes = doc.tobytes()
        doc.close()
        name = "docker-smoke-sample.pdf"
        uploaded = asyncio.run(
            _rpc(
                base,
                token,
                "documents.upload",
                {"name": name, "data": base64.b64encode(pdf_bytes).decode("ascii")},
            )
        )
        document = uploaded.get("document") or {}
        doc_id = document.get("id", "")
        sync = document.get("vector_sync") or {}
        chunk_count = int(sync.get("chunks") or document.get("chunk_count") or 0)
        record(
            "rag_pdf_parsed",
            bool(doc_id) and chunk_count > 0,
            f"doc={doc_id} chunks={chunk_count} warning={str(uploaded.get('warning', ''))[:80]}",
        )
        written = int(sync.get("written") or sync.get("points") or sync.get("upserted") or 0)
        record(
            "rag_vector_index_written",
            bool(doc_id) and not sync.get("errors") and written > 0,
            f"written={written} embed_seconds={sync.get('embed_seconds')} errors={sync.get('errors')}",
        )
    except Exception as exc:  # noqa: BLE001
        record("rag_pdf_parsed", False, f"{type(exc).__name__}: {exc}")
        return

    # 真实 Hybrid 检索（会消耗 1 次 LLM 调用用于组织回答）
    try:
        answer = asyncio.run(
            _rpc(
                base,
                token,
                "knowledge.query",
                {"question": "\u5dee\u65c5\u4f4f\u5bbf\u4e0a\u9650\u662f\u591a\u5c11\uff1f\u62a5\u9500\u9700\u8981\u8c01\u5ba1\u6279\uff1f", "doc_ids": [doc_id]},
            )
        )
        citations = answer.get("citations") or []
        retrieval = answer.get("retrieval") or {}
        record(
            "rag_hybrid_query",
            bool(citations) and answer.get("grounded") is True,
            f"citations={len(citations)} mode={retrieval.get('mode')} "
            f"meta={str(answer.get('context_meta'))[:80]}",
        )
        record(
            "rag_answer_grounded",
            "600" in str(answer.get("answer") or ""),
            f"answer={str(answer.get('answer'))[:80]}",
        )
    except Exception as exc:  # noqa: BLE001
        record("rag_hybrid_query", False, f"{type(exc).__name__}: {exc}")

    if qdrant_url:
        try:
            with urllib.request.urlopen(qdrant_url.rstrip("/") + "/collections", timeout=10) as resp:
                collections = json.loads(resp.read().decode())
            names = [c.get("name") for c in (collections.get("result") or {}).get("collections", [])]
            record("qdrant_collection_present", bool(names), f"collections={names}")
        except Exception as exc:  # noqa: BLE001
            record("qdrant_collection_present", False, f"{type(exc).__name__}: {exc}")


def check_workflow(base: str, token: str) -> None:
    """启动一个工作流并等到“等待审批”，然后取消（绝不发信）。"""
    text = (
        "部署冒烟测试会议纪要（虚构数据）。参会人：张三、李四。"
        "供应商交付延期两周，联调窗口被压缩，此风险尚未解决。"
        "李四负责在12月1日前给出备选供应商清单。"
    )
    try:
        started = asyncio.run(
            _rpc(
                base,
                token,
                "workflow.start",
                {"name": "smoke.txt", "text": text, "to": "smoke@example.com"},
            )
        )
        wf = started.get("workflow") or {}
        workflow_id = wf.get("workflow_id", "")
        record(
            "workflow_start",
            bool(workflow_id)
            and wf.get("execution_status") in {"waiting_approval", "completed", "failed"},
            f"status={wf.get('execution_status')} id={workflow_id}",
        )
        if not workflow_id:
            return
        cancelled = asyncio.run(_rpc(base, token, "workflow.cancel", {"id": workflow_id}))
        data = cancelled.get("workflow") or {}
        record(
            "workflow_cancel",
            data.get("execution_status") in {"cancelled", "completed"},
            f"status={data.get('execution_status')}",
        )
    except Exception as exc:  # noqa: BLE001
        record("workflow_start", False, f"{type(exc).__name__}: {exc}")


def main() -> int:
    parser = argparse.ArgumentParser(description="AI 外脑部署冒烟测试")
    parser.add_argument("--url", default="http://127.0.0.1:18888")
    parser.add_argument("--token", default="")
    parser.add_argument("--workflow", action="store_true", help="额外验证 LangGraph 工作流（会产生 LLM 调用）")
    parser.add_argument("--rag", action="store_true", help="额外验证知识库/检索")
    parser.add_argument(
        "--rag-upload",
        action="store_true",
        help="额外：上传合成 PDF 并做一次真实 Hybrid 检索（消耗 1 次 LLM 调用）",
    )
    parser.add_argument("--qdrant-url", default="", help="可选：直接查 Qdrant /collections 验证索引持久化")
    parser.add_argument("--min-docs", type=int, default=0, help="知识库至少应有几份文档（重启持久化验证用）")
    parser.add_argument("--json-out", default="", help="把结果写成 JSON 文件")
    args = parser.parse_args()

    base = args.url.rstrip("/")
    token = args.token or ""

    try:
        status, _ = _http(base + "/healthz")
    except Exception as exc:  # noqa: BLE001
        print(f"[FATAL] 无法连接 {base}/healthz：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    check_http(base, token)
    if token:
        check_ws(base, token)
        if args.rag or args.rag_upload:
            check_rag(
                base,
                token,
                upload=args.rag_upload,
                qdrant_url=args.qdrant_url,
                min_docs=args.min_docs,
            )
        if args.workflow:
            check_workflow(base, token)
    else:
        record("ws_connect_with_token", False, "未提供 --token（容器模式下必填）")

    passed = sum(1 for r in RESULTS if r["ok"])
    total = len(RESULTS)
    print()
    print(f"冒烟测试：{passed}/{total} 通过（host={base}）")
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump({"base": base, "passed": passed, "total": total, "results": RESULTS}, fh, ensure_ascii=False, indent=2)
        print(f"结果 → {args.json_out}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
