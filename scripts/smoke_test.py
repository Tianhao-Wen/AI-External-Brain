"""端到端验收脚本：对着真实服务端跑六项功能（真实模型 / 真实文件）。

用法::

    # 1) 先启动服务（另一个终端）
    aieb --port 18889
    # 2) 再跑验收
    python scripts/smoke_test.py --port 18889

说明
----
* 每一项都会打印 PASS / FAIL / SKIP 与证据（回答摘要、文件大小、来源链接等）；
* 不访问网络的项目（纯本地存储、邮件未配置的失败路径）与真实 API 项目分开标注；
* 失败不会中断后续项目，最终给出汇总，绝不把失败说成成功。
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import time
from pathlib import Path

import httpx
import websockets

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "samples"

results: list[dict[str, object]] = []


def record(name: str, ok: bool, evidence: str, kind: str = "real") -> None:
    results.append({"name": name, "ok": ok, "evidence": evidence, "kind": kind})
    flag = "PASS" if ok else "FAIL"
    print(f"[{flag}] {name} ({kind}) — {evidence}", flush=True)


class Client:
    def __init__(self, url: str) -> None:
        self.url = url
        self.ws = None
        self.seq = 0
        self.pending: dict[str, asyncio.Future] = {}
        self.events: asyncio.Queue = asyncio.Queue()

    async def __aenter__(self) -> "Client":
        self.ws = await websockets.connect(self.url, subprotocols=["aieb.v1"], open_timeout=20)
        self._reader = asyncio.create_task(self._read())
        return self

    async def __aexit__(self, *exc) -> None:
        self._reader.cancel()
        await self.ws.close()

    async def _read(self) -> None:
        async for raw in self.ws:
            msg = json.loads(raw)
            data = msg.get("data") or {}
            if msg.get("type") == "brain_response":
                fut = self.pending.pop(data.get("req_id", ""), None)
                if fut and not fut.done():
                    fut.set_result(data)
            else:
                await self.events.put(msg)

    async def rpc(self, op: str, payload: dict | None = None, timeout: float = 240.0) -> dict:
        self.seq += 1
        req_id = f"smoke{self.seq}"
        fut = asyncio.get_running_loop().create_future()
        self.pending[req_id] = fut
        await self.ws.send(json.dumps({
            "type": "brain_request",
            "data": dict({"op": op, "req_id": req_id}, **(payload or {})),
        }))
        data = await asyncio.wait_for(fut, timeout=timeout)
        if not data.get("ok"):
            raise RuntimeError(data.get("error") or "操作失败")
        return data.get("data") or {}

    async def chat(self, prompt: str, timeout: float = 180.0) -> tuple[str, list[str]]:
        await self.ws.send(json.dumps({"type": "user_message", "data": {"content": prompt}}))
        answer, artifacts = "", []
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("等待模型回复超时")
            msg = await asyncio.wait_for(self.events.get(), timeout=remaining)
            kind = msg.get("type")
            data = msg.get("data") or {}
            if kind == "stream_text":
                answer += data.get("text", "")
            elif kind == "artifacts":
                artifacts.extend(data.get("files") or [])
            elif kind == "error":
                raise RuntimeError(data.get("message") or "模型报错")
            elif kind == "loop_complete":
                return answer, artifacts


async def wait_for_server(port: int, timeout: float = 60.0) -> bool:
    """等待服务就绪。

    服务刚启动时（尤其首次初始化数据库、拉取模型上下文窗口）端口可能还没开始监听，
    这时直接连 WS 会得到 ConnectionRefusedError，看起来像“服务挂了”。这里轮询首页，
    直到 HTTP 200 或超时，让冒烟结果只反映产品真实状态。
    """
    deadline = time.monotonic() + timeout
    last: str = "未尝试"
    while time.monotonic() < deadline:
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(f"http://127.0.0.1:{port}/")
            if resp.status_code == 200:
                return True
            last = f"HTTP {resp.status_code}"
        except Exception as exc:  # noqa: BLE001 - 未就绪时任何异常都继续等待
            last = f"{type(exc).__name__}: {exc}"
        await asyncio.sleep(0.5)
    print(f"服务未就绪（127.0.0.1:{port}，等待 {timeout:.0f}s）：{last}", flush=True)
    return False


async def check_page(port: int) -> None:
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(f"http://127.0.0.1:{port}/")
        html = resp.text
        ok = resp.status_code == 200 and "AI 外脑 Demo" in html and "知识库" in html
        record("页面加载", ok, f"HTTP {resp.status_code} / {len(html)} 字节", kind="local")
    except Exception as exc:  # noqa: BLE001
        record("页面加载", False, f"{exc}", kind="local")


async def download(url: str, port: int) -> bytes:
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(f"http://127.0.0.1:{port}{url.split(str(port))[-1]}")
    return resp.content


async def run(port: int, skip_chat: bool, send_real_email: bool = False,
              email_to: str = "") -> None:
    base = f"ws://127.0.0.1:{port}"
    if not await wait_for_server(port):
        record("服务就绪", False, f"127.0.0.1:{port} 未在超时内启动", kind="local")
        raise SystemExit(1)
    await check_page(port)

    async with Client(base + "/ws") as c:
        # 1) 普通 AI 对话（真实模型）
        if skip_chat:
            record("测试1 · AI 对话", False, "已跳过（--skip-chat）", kind="skipped")
        else:
            try:
                answer, _ = await c.chat("用一句话说明你在企业场景里能帮我做什么？")
                record("测试1 · AI 对话", bool(answer.strip()),
                       answer.strip().replace("\n", " ")[:80] or "空回答")
            except Exception as exc:  # noqa: BLE001
                record("测试1 · AI 对话", False, f"{exc}")

        # 2) PDF 知识背景
        pdf = SAMPLES / "产品需求说明书.pdf"
        try:
            payload = base64.b64encode(pdf.read_bytes()).decode("ascii")
            up = await c.rpc("documents.upload", {"name": pdf.name, "data": payload})
            doc = up["document"]
            record("测试2a · PDF 上传解析", doc["status"] == "ready",
                   f"{doc['name']} / {doc['pages']} 页 / {doc['chars']} 字符", kind="local")

            q = await c.rpc("knowledge.query", {
                "question": "验收条款 C 对回答引用有什么要求？",
                "doc_ids": [doc["id"]],
            }, timeout=300)
            cites = q.get("citations") or []
            page_ok = any(int(ci.get("page") or 0) == 2 for ci in cites)
            ok = bool(q.get("answer")) and page_ok
            record("测试2b · PDF 问答与页码引用", ok,
                   f"引用页={[ci.get('page') for ci in cites]} / 回答："
                   f"{str(q.get('answer'))[:70]}".replace("\n", " "))
        except Exception as exc:  # noqa: BLE001
            record("测试2 · PDF 知识背景", False, f"{exc}")

        # 3) 会议分析 + 邮件
        meeting_id = ""
        try:
            text = (SAMPLES / "会议记录_有风险.txt").read_text(encoding="utf-8")
            res = await c.rpc("meetings.analyze", {"name": "会议记录_有风险.txt", "text": text},
                              timeout=300)
            meeting = res["meeting"]
            meeting_id = meeting["id"]
            risks, todos = meeting["risks"], meeting["todos"]
            grounded = all(
                (t["owner"] == "" or t["owner"] in text) and
                (t["due_date"] == "" or t["due_date"].replace(" ", "") in text.replace(" ", ""))
                for t in todos
            )
            ok = bool(meeting.get("title")) and bool(risks) and bool(todos) and grounded
            record("测试3a · TXT 会议分析（风险+待办）", ok,
                   f"标题={meeting.get('title')} / 风险{len(risks)} / 待办{len(todos)} / "
                   f"负责人截止时间均可溯源={grounded}")

            draft = await c.rpc("email.draft", {"meeting_id": meeting_id, "to": "demo@example.com"},
                                timeout=300)
            email = draft["email"]
            record("测试3b · 邮件草稿生成", email["status"] == "draft",
                   f"主题={email['subject'][:40]}")

            mail_cfg = (await c.rpc("email.config", {})).get("mail") or {}
            configured = bool(mail_cfg.get("configured"))
            dry_run = bool(mail_cfg.get("dry_run"))
            if not configured:
                # 未配置：必须如实失败，不能假装成功
                try:
                    await c.rpc("email.send", {"id": email["id"], "confirm": True}, timeout=120)
                    record("测试3c · 未配置 SMTP 时必须失败", False,
                           "服务端返回成功——这会虚假宣称邮件已发出")
                except Exception as exc:  # noqa: BLE001
                    ok = "SMTP" in str(exc)
                    record("测试3c · 未配置 SMTP 时如实失败", ok, str(exc)[:90], kind="local")
            elif dry_run:
                # 服务端开了发送保护：必须被拦截，而不是静默投递
                try:
                    await c.rpc("email.send", {"id": email["id"], "confirm": True}, timeout=120)
                    record("测试3c · 保护模式拦截真实发送", False,
                           "保护模式已开启，但仍返回成功——真实投递没有被拦住")
                except Exception as exc:  # noqa: BLE001
                    ok = "保护模式" in str(exc) or "AIEB_EMAIL_DRY_RUN" in str(exc)
                    record("测试3c · 保护模式拦截真实发送", ok, str(exc)[:110], kind="local")
            elif send_real_email and email_to:
                # 只有显式指定开关与收件人时才真实投递
                try:
                    sent = await c.rpc("email.send", {
                        "id": email["id"], "confirm": True, "to_addr": email_to,
                    }, timeout=180)
                    record("测试3c · 邮件真实发送（显式开关）", True,
                           f"{sent.get('message')} / 状态={(sent.get('email') or {}).get('status')}")
                except Exception as exc:  # noqa: BLE001
                    record("测试3c · 邮件真实发送（显式开关）", False, str(exc)[:120])
            else:
                # 默认：只验证草稿，绝不投递（避免自动化测试误发真实邮件）
                record("测试3c · 邮件草稿已生成、未投递（默认保护）", True,
                       "SMTP 已配置但本次未请求投递；如需真发请加 "
                       "--send-real-email --email-to <你的邮箱>", kind="local")
        except Exception as exc:  # noqa: BLE001
            record("测试3 · 会议分析/邮件", False, f"{exc}")

        # 4) 长期记忆（跨会话 + 服务重启后仍在）
        try:
            mem = await c.rpc("memory.list")
            items = mem.get("memories") or []
            facts = [m for m in items if m.get("kind") == "fact"]
            ok = bool(facts) and any("供应商" in m.get("content", "") for m in facts)
            record("测试4 · 会议要素进入长期记忆", ok,
                   f"{len(facts)} 条结构化记忆，示例：{facts[0]['content'][:40] if facts else '无'}",
                   kind="local")
        except Exception as exc:  # noqa: BLE001
            record("测试4 · 长期记忆", False, f"{exc}")

        # 5) 联网搜索（真实网络）
        if skip_chat:
            record("测试5 · 联网搜索", False, "已跳过（--skip-chat）", kind="skipped")
        else:
            try:
                answer, _ = await c.chat(
                    "请联网搜索最近关于人工智能的一条新闻，用一句话总结并附上来源链接。",
                    timeout=240,
                )
                ok = "http" in answer
                record("测试5 · 联网搜索与来源", ok,
                       answer.replace("\n", " ")[:110] or "空回答")
            except Exception as exc:  # noqa: BLE001
                record("测试5 · 联网搜索", False, f"{exc}")

        # 6) PPT 生成（真实 .pptx + 下载）
        try:
            ppt = await c.rpc("ppt.generate", {
                "topic": "供应商风险复盘汇报", "slides": 6,
                "audience": "管理层", "focus": "风险与整改行动",
            }, timeout=420)
            file_info = ppt.get("file") or {}
            ok = bool(file_info.get("url"))
            evidence = f"{ppt.get('filename')} / {ppt.get('slide_count')} 页"
            if ok:
                data = await download(file_info["url"], port)
                is_zip = data[:2] == b"PK"
                record("测试6 · PPT 生成与下载", is_zip and len(data) > 20000,
                       f"{evidence} / 下载 {len(data)} 字节 / pptx 容器={is_zip}")
            else:
                record("测试6 · PPT 生成与下载", False, f"{evidence} / 无可下载产物")
        except Exception as exc:  # noqa: BLE001
            record("测试6 · PPT", False, f"{exc}")

    print("\n==== 汇总 ====")
    for r in results:
        print(f"{'PASS' if r['ok'] else 'FAIL'}\t{r['kind']}\t{r['name']}\t{r['evidence']}")
    failed = [r for r in results if not r["ok"] and r["kind"] != "skipped"]
    print(f"\n共 {len(results)} 项，失败 {len(failed)} 项。")


async def run_persistence(port: int) -> None:
    """服务重启后检查：会议、待办、结构化记忆仍然可读。"""
    base = f"ws://127.0.0.1:{port}"
    if not await wait_for_server(port):
        print(f"服务未就绪（127.0.0.1:{port}）", flush=True)
        raise SystemExit(1)
    async with Client(base + "/ws") as c:
        dash = await c.rpc("dashboard")
        stats = dash.get("stats") or {}
        mem = await c.rpc("memory.list")
        facts = [m for m in (mem.get("memories") or []) if m.get("kind") == "fact"]
        todos = await c.rpc("todos.list", {"status": "all"})
        ok = (stats.get("meetings", 0) > 0 and stats.get("documents", 0) > 0
              and len(facts) > 0 and len(todos.get("todos") or []) > 0)
        record("测试4b · 服务重启后数据仍在", ok,
               f"会议={stats.get('meetings')} / 文档={stats.get('documents')} / "
               f"待办={len(todos.get('todos') or [])} / 结构化记忆={len(facts)}",
               kind="local")
    for r in results:
        print(f"{'PASS' if r['ok'] else 'FAIL'}\t{r['kind']}\t{r['name']}\t{r['evidence']}")
    if not all(r["ok"] for r in results):
        raise SystemExit(1)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=18889)
    parser.add_argument("--skip-chat", action="store_true", help="跳过需要真实模型的项目")
    parser.add_argument("--persistence-only", action="store_true",
                        help="只检查服务重启后的数据持久化")
    parser.add_argument("--send-real-email", action="store_true",
                        help="确需验证真实投递时才加；默认只验证草稿，不投递")
    parser.add_argument("--email-to", default="",
                        help="与 --send-real-email 配套的真实收件地址")
    args = parser.parse_args()
    if args.send_real_email and not args.email_to:
        parser.error("--send-real-email 必须同时指定 --email-to，避免误发到占位地址")
    if args.persistence_only:
        asyncio.run(run_persistence(args.port))
        return
    asyncio.run(run(args.port, args.skip_chat, args.send_real_email, args.email_to))


if __name__ == "__main__":
    main()
