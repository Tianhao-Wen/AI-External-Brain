"""在**没有 Docker**的环境里验证"容器运行契约"。

为什么需要它：Docker Desktop 不可用时，`docker compose up` 无法执行，但容器真正依赖的
契约（监听 0.0.0.0、token 鉴权、数据目录落在 cwd/.aieb、Qdrant 走服务名地址、
默认 dry-run 发信）完全可以在宿主机上按**同一套环境变量**跑起来验证。

它做的事与 `docker/entrypoint.sh` + compose 完全一致：
  1. 在独立数据目录里准备 .aieb/config.yaml（复制 docker/config.docker.yaml）
  2. 以 0.0.0.0 + AIEB_REMOTE_TOKEN 启动 `python -m aieb.brain.cli`
  3. 等 /healthz 就绪，再调用 scripts/docker_smoke_test.py 做端到端检查
  4. 关闭服务并汇总

    python scripts/verify_container_contract.py --port 18899 --qdrant-url http://127.0.0.1:6399
    python scripts/verify_container_contract.py --port 18899 --qdrant-url http://127.0.0.1:6333 --rag
"""

from __future__ import annotations

import argparse
import asyncio
import os
import secrets
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "docker" / "config.docker.yaml"
SMOKE = ROOT / "scripts" / "docker_smoke_test.py"
WORK = ROOT / "benchmarks" / ".work" / "docker_contract"

sys.path.insert(0, str(ROOT))
from scripts.docker_smoke_test import _rpc  # noqa: E402  （复用同一套 WebSocket 协议实现）


def wait_health(url: str, timeout: float = 120.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url + "/healthz", timeout=3) as resp:
                if resp.status == 200:
                    return True
        except (urllib.error.URLError, OSError, TimeoutError):
            pass
        time.sleep(1.0)
    return False


def _launch(env: dict[str, str], data_dir: Path, port: int, log_path: Path) -> subprocess.Popen:
    handle = log_path.open("a", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "-m", "aieb.brain.cli", "--host", "0.0.0.0", "--port", str(port)],
        cwd=str(data_dir),
        env=env,
        stdout=handle,
        stderr=subprocess.STDOUT,
    )
    proc._aieb_log = handle  # type: ignore[attr-defined]
    return proc


def _shutdown(proc: subprocess.Popen) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=20)
    except subprocess.TimeoutExpired:
        proc.kill()
    handle = getattr(proc, "_aieb_log", None)
    if handle is not None:
        handle.close()


def _run_restart_scenario(
    env: dict[str, str], data_dir: Path, base: str, token: str, port: int, log_path: Path
) -> int:
    """审批中断 → 停进程 → 重启 → 按原 workflow_id 查到并取消（对应任务书测试 C）。"""
    text = (
        "容器恢复验证会议纪要（虚构数据）。参会人：王五、赵六。"
        "测试环境资源不足，压测排队严重，此风险尚未解决。"
        "赵六负责在12月10日前扩容两套测试环境。"
    )
    results: list[tuple[str, bool, str]] = []

    def record(name: str, ok: bool, detail: str) -> None:
        results.append((name, ok, detail))
        print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}", flush=True)

    # ---- 阶段 1：跑到“等待审批”，然后关掉进程 ----
    proc = _launch(env, data_dir, port, log_path)
    workflow_id = ""
    status1 = ""
    try:
        if not wait_health(base):
            record("restart.server_stage1_up", False, "第一阶启动超时")
            return 1
        started = asyncio.run(
            _rpc(
                base,
                token,
                "workflow.start",
                {"name": "restart-check.txt", "text": text, "to": "restart@example.com"},
            )
        )
        wf = started.get("workflow") or {}
        workflow_id = wf.get("workflow_id", "")
        status1 = str(wf.get("execution_status"))
        record(
            "restart.workflow_waiting_approval",
            bool(workflow_id) and status1 == "waiting_approval",
            f"id={workflow_id} status={status1}",
        )
    finally:
        _shutdown(proc)
    print("[stage] 后端进程已终止（相当于 docker compose restart aieb）", flush=True)
    if not workflow_id:
        return 1

    # ---- 阶段 2：新进程，按原 ID 查状态 → 取消 ----
    proc = _launch(env, data_dir, port, log_path)
    try:
        if not wait_health(base):
            record("restart.server_stage2_up", False, "重启后启动超时")
            return 1
        status = asyncio.run(_rpc(base, token, "workflow.status", {"id": workflow_id}))
        snap = status.get("workflow") or {}
        record(
            "restart.checkpoint_recovered",
            snap.get("execution_status") == "waiting_approval" and snap.get("resumable") is True,
            f"status={snap.get('execution_status')} resumable={snap.get('resumable')} "
            f"next={snap.get('next_nodes')}",
        )
        listed = asyncio.run(_rpc(base, token, "workflow.list", {"limit": 20}))
        ids = [w.get("workflow_id") for w in (listed.get("workflows") or [])]
        record("restart.workflow_findable_by_id", workflow_id in ids, f"list 中包含原 id={workflow_id}")
        cancelled = asyncio.run(_rpc(base, token, "workflow.cancel", {"id": workflow_id}))
        final = (cancelled.get("workflow") or {}).get("execution_status")
        record("restart.resume_after_restart", final == "cancelled", f"cancel → {final}")
        history = asyncio.run(_rpc(base, token, "workflow.history", {"id": workflow_id}))
        events = history.get("events") or []
        record(
            "restart.trace_available_after_restart",
            len(events) > 0,
            f"Trace 事件数={len(events)}（重启后仍可读取）",
        )
        emails = asyncio.run(_rpc(base, token, "email.list"))
        sent = [e for e in (emails.get("emails") or []) if e.get("status") == "sent"]
        record("restart.no_duplicate_send", not sent, f"已发送邮件数={len(sent)}（取消后应为 0）")
    finally:
        _shutdown(proc)

    passed = sum(1 for _, ok, _ in results if ok)
    print(f"\n跨进程恢复：{passed}/{len(results)} 通过")
    return 0 if passed == len(results) else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="容器运行契约验证（无需 Docker）")
    parser.add_argument("--port", type=int, default=18899)
    parser.add_argument("--data-dir", default=str(WORK))
    parser.add_argument("--qdrant-url", default="http://127.0.0.1:6399")
    parser.add_argument("--mode", default="hybrid")
    parser.add_argument("--workflow", action="store_true", help="额外验证 LangGraph 工作流（会产生 LLM 调用）")
    parser.add_argument("--rag", action="store_true", help="额外验证知识库列表/检索")
    parser.add_argument(
        "--rag-upload",
        action="store_true",
        help="额外：上传合成 PDF + 真实 Hybrid 检索（消耗 1 次 LLM 调用）",
    )
    parser.add_argument("--min-docs", type=int, default=0, help="知识库至少应有几份文档")
    parser.add_argument("--keep", action="store_true", help="保留数据目录（默认清空重建）")
    parser.add_argument(
        "--restart-workflow",
        action="store_true",
        help="验证审批中断跨进程恢复（启动→停进程→重启→按原 ID 查到→取消；消耗 2 次 LLM 调用）",
    )
    args = parser.parse_args()

    data_dir = Path(args.data_dir).resolve()
    if not args.keep:
        shutil.rmtree(data_dir, ignore_errors=True)
    (data_dir / ".aieb").mkdir(parents=True, exist_ok=True)

    config = data_dir / ".aieb" / "config.yaml"
    if not config.is_file():
        shutil.copyfile(TEMPLATE, config)
        print(f"[setup] 已按模板生成 {config}")

    token = secrets.token_urlsafe(18)
    env = dict(os.environ)
    env.update(
        {
            "PYTHONPATH": str(ROOT),
            "AIEB_HOST": "0.0.0.0",
            "AIEB_PORT": str(args.port),
            "AIEB_REMOTE_TOKEN": token,
            "AIEB_QDRANT_URL": args.qdrant_url,
            "AIEB_RETRIEVAL_MODE": args.mode,
            "AIEB_EMAIL_DRY_RUN": "1",
            "AIEB_EMBEDDING_CACHE_DIR": str(data_dir / ".cache" / "fastembed"),
        }
    )
    env.pop("AIEB_ALLOW_UNAUTHENTICATED_REMOTE", None)

    base = f"http://127.0.0.1:{args.port}"
    log_path = data_dir / "server.log"
    print(f"[run ] 启动后端：0.0.0.0:{args.port}  cwd={data_dir}")
    print(f"       AIEB_QDRANT_URL={args.qdrant_url} mode={args.mode}（与 compose 注入一致）")

    if args.restart_workflow:
        return _run_restart_scenario(env, data_dir, base, token, args.port, log_path)

    proc = _launch(env, data_dir, args.port, log_path)
    try:
        ok = wait_health(base)
        print(f"[wait] /healthz {'就绪' if ok else '超时'}")
        if not ok:
            print(log_path.read_text(encoding="utf-8", errors="replace")[-2000:])
            return 2

        cmd = [
            sys.executable,
            str(SMOKE),
            "--url",
            base,
            "--token",
            token,
            "--json-out",
            str(data_dir / "smoke.json"),
        ]
        if args.rag:
            cmd.append("--rag")
        if args.rag_upload:
            cmd.extend(["--rag-upload", "--qdrant-url", args.qdrant_url])
        if args.min_docs:
            cmd.extend(["--min-docs", str(args.min_docs)])
        if args.workflow:
            cmd.append("--workflow")
        return subprocess.run(cmd, cwd=str(ROOT), env=env).returncode
    finally:
        _shutdown(proc)
        print(f"[stop] 服务已关闭（日志：{log_path}）")


if __name__ == "__main__":
    raise SystemExit(main())
