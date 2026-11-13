"""Checkpoint 故障注入与并发实验（真实进程崩溃 / 真实 SQLite / 无 mock 恢复）。

与单元测试的区别：**R 组用独立子进程执行**，崩溃是 `os._exit(9)`（没有 finally 清理），
恢复是另一个进程重新打开同一个 Checkpoint 文件 —— 而不是在同一进程里重建对象。

    python -m benchmarks.workflow.fault_injection

产出：
* `benchmarks/results/workflow/fault_injection.json`（每个场景的注入点/预期/实际/DB 终态）
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from aieb.brain.store import BrainStore
from aieb.workflow.persistence import WorkflowStore
from aieb.workflow.runner import WorkflowError, WorkflowRunner
from aieb.workflow.state import ResumeCommand
from benchmarks import common
from benchmarks.workflow.chaos import ChaosWorld, install_store_faults, make_deps

ROOT = Path(__file__).resolve().parents[2]
WORK = common.WORK_ROOT / "fault"
OUT = common.RESULTS_DIR / "workflow" / "fault_injection.json"

TEXT = (
    "项目周会纪要（2026年11月10日）\n"
    "参会人：张伟、李娜、王强\n"
    "一、关键物料交付延期两周，影响联调窗口，目前尚未恢复。\n"
    "二、决定启动备选供应商，由李娜负责在10月15日前给出备选供应商清单。\n"
)

rows: list[dict[str, Any]] = []


# ---------------------------------------------------------------------------
# 通用
# ---------------------------------------------------------------------------


def _reset(name: str) -> Path:
    workspace = WORK / name
    shutil.rmtree(workspace, ignore_errors=True)
    workspace.mkdir(parents=True, exist_ok=True)
    return workspace


def _record(
    *,
    scenario: str,
    inject_at: str,
    expected: str,
    actual: str,
    recovered: str,
    counts: dict[str, Any],
    duplicate: int,
    ok: bool,
    extra: dict[str, Any] | None = None,
) -> None:
    row = {
        "scenario": scenario,
        "inject_at": inject_at,
        "expected": expected,
        "actual": actual,
        "recovered": recovered,
        "final_db": counts,
        "duplicate_side_effects": duplicate,
        "ok": ok,
    }
    if extra:
        row.update(extra)
    rows.append(row)
    print(
        f"[{'OK ' if ok else 'x  '}] {scenario:<34} {actual[:46]:<46} dup={duplicate}",
        flush=True,
    )


def _child(workspace: Path, *args: str, extra_env: dict[str, str] | None = None) -> dict[str, Any]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT)
    env.pop("AIEB_EMAIL_DRY_RUN", None)
    env.update(extra_env or {})
    cmd = [sys.executable, "-m", "benchmarks.workflow.chaos_child", *args,
           "--workspace", str(workspace)]
    proc = subprocess.run(cmd, capture_output=True, text=True, env=env, cwd=str(ROOT), timeout=300)
    for line in reversed(proc.stdout.splitlines()):
        if line.startswith("__RESULT__"):
            payload = json.loads(line[len("__RESULT__"):])
            payload["returncode"] = proc.returncode
            return payload
    return {
        "ok": False,
        "returncode": proc.returncode,
        "error": (proc.stderr or proc.stdout or "子进程无输出")[-400:],
    }


def _text_file(workspace: Path) -> str:
    path = workspace / "meeting.txt"
    path.write_text(TEXT, encoding="utf-8")
    return str(path)


# ---------------------------------------------------------------------------
# R 组：真实进程崩溃 + 重启恢复
# ---------------------------------------------------------------------------


def run_restart_scenarios() -> None:
    # R1：分析完成后崩溃（validate_risks 前）
    ws = _reset("R1_crash_after_analyze")
    text_file = _text_file(ws)
    first = _child(ws, "start", "--name", "周会.txt", "--text-file", text_file,
                   extra_env={"AIEB_CHAOS_CRASH_AT": "validate_risks"})
    killed = first.get("returncode") == 9
    again = _child(ws, "start", "--name", "周会.txt", "--text-file", text_file)
    workflow_id = again.get("workflow_id", "")
    resumed = _child(ws, "continue", "--id", workflow_id)
    approve = _child(ws, "approve", "--id", workflow_id)
    counts = approve.get("counts") or {}
    _record(
        scenario="R1 分析后崩溃 → 重启续跑",
        inject_at="validate_risks 前 os._exit(9)",
        expected="崩溃不落库；重启后 continue 完成流程；会议只 1 条",
        actual=f"crash={killed} status={approve.get('status')}",
        recovered=f"continue={resumed.get('status')} → approve={approve.get('status')}",
        counts=counts,
        duplicate=max(0, counts.get("meetings", 0) - 1),
        ok=killed and approve.get("status") == "completed" and counts.get("meetings") == 1,
    )

    # R2：业务落库后、草稿前崩溃
    ws = _reset("R2_crash_after_persist")
    text_file = _text_file(ws)
    first = _child(ws, "start", "--name", "周会.txt", "--text-file", text_file,
                   extra_env={"AIEB_CHAOS_CRASH_AT": "generate_email_draft"})
    killed = first.get("returncode") == 9
    again = _child(ws, "start", "--name", "周会.txt", "--text-file", text_file)
    workflow_id = again.get("workflow_id", "")
    resumed = _child(ws, "continue", "--id", workflow_id)
    approve = _child(ws, "approve", "--id", workflow_id)
    counts = approve.get("counts") or {}
    _record(
        scenario="R2 落库后崩溃 → 重启续跑",
        inject_at="generate_email_draft 前 os._exit(9)",
        expected="会议/风险/待办各 1 条，不因续跑翻倍；最终 completed 且发 1 封",
        actual=f"crash={killed} status={approve.get('status')}",
        recovered=f"continue={resumed.get('status')}",
        counts=counts,
        duplicate=max(0, counts.get("meetings", 0) - 1),
        ok=killed and counts.get("meetings") == 1 and counts.get("risks") == 1
        and counts.get("todos") == 1 and approve.get("status") == "completed"
        and counts.get("sends") == 1,
    )

    # R3：草稿落库后（审批节点前）崩溃
    ws = _reset("R3_crash_before_interrupt")
    text_file = _text_file(ws)
    first = _child(ws, "start", "--name", "周会.txt", "--text-file", text_file,
                   extra_env={"AIEB_CHAOS_CRASH_AT": "human_approval"})
    killed = first.get("returncode") == 9
    again = _child(ws, "start", "--name", "周会.txt", "--text-file", text_file)
    workflow_id = again.get("workflow_id", "")
    resumed = _child(ws, "continue", "--id", workflow_id)
    approve = _child(ws, "approve", "--id", workflow_id)
    counts = approve.get("counts") or {}
    _record(
        scenario="R3 草稿后崩溃 → 重启继续到审批",
        inject_at="human_approval 前 os._exit(9)",
        expected="重启后 continue 进入等待审批，再 approve 发 1 封",
        actual=f"crash={killed} continue={resumed.get('status')} approve={approve.get('status')}",
        recovered=resumed.get("status", ""),
        counts=counts,
        duplicate=max(0, counts.get("emails", 0) - 1),
        ok=killed and resumed.get("status") == "waiting_approval"
        and approve.get("status") == "completed" and counts.get("sends") == 1,
    )

    # R4：等待审批时进程退出（无崩溃） → 新进程能重新拿到审批载荷
    ws = _reset("R4_wait_then_new_process")
    text_file = _text_file(ws)
    started = _child(ws, "start", "--name", "周会.txt", "--text-file", text_file)
    workflow_id = started.get("workflow_id", "")
    status = _child(ws, "status", "--id", workflow_id)
    approve = _child(ws, "approve", "--id", workflow_id)
    counts = approve.get("counts") or {}
    _record(
        scenario="R4 等待审批 → 新进程恢复",
        inject_at="进程正常退出（审批期间）",
        expected="新进程 status 仍为 waiting_approval 且 resumable；approve 成功",
        actual=f"status={status.get('status')} resumable={status.get('resumable')} "
               f"approve={approve.get('status')}",
        recovered="新进程 approve",
        counts=counts,
        duplicate=max(0, counts.get("sends", 0) - 1),
        ok=started.get("status") == "waiting_approval"
        and status.get("status") == "waiting_approval"
        and status.get("resumable") is True
        and approve.get("status") == "completed"
        and counts.get("sends") == 1,
    )

    # R5：等待审批 → 重启后取消
    ws = _reset("R5_restart_then_cancel")
    text_file = _text_file(ws)
    started = _child(ws, "start", "--name", "周会.txt", "--text-file", text_file)
    workflow_id = started.get("workflow_id", "")
    reject = _child(ws, "reject", "--id", workflow_id)
    counts = reject.get("counts") or {}
    _record(
        scenario="R5 等待审批 → 新进程取消",
        inject_at="进程正常退出（审批期间）",
        expected="取消后不发信，邮件状态 cancelled",
        actual=f"status={reject.get('status')}",
        recovered="新进程 reject",
        counts=counts,
        duplicate=0,
        ok=reject.get("status") == "cancelled" and counts.get("sends") == 0,
    )

    # R6：已完成后再次 resume
    ws = _reset("R6_resume_after_completed")
    text_file = _text_file(ws)
    started = _child(ws, "start", "--name", "周会.txt", "--text-file", text_file)
    workflow_id = started.get("workflow_id", "")
    _child(ws, "approve", "--id", workflow_id)
    again = _child(ws, "approve", "--id", workflow_id)
    counts = again.get("counts") or {}
    _record(
        scenario="R6 已完成后再次确认",
        inject_at="重复 approve（终态）",
        expected="不重复发信，状态仍为 completed",
        actual=f"ok={again.get('ok')} status={again.get('status')} error={again.get('error')[:40]}",
        recovered="返回当前快照",
        counts=counts,
        duplicate=max(0, counts.get("sends", 0) - 1),
        ok=counts.get("sends") == 1,
    )


# ---------------------------------------------------------------------------
# C 组：并发 / 重复请求 / 故障（同进程，真实 SQLite）
# ---------------------------------------------------------------------------


def _runner(workspace: Path) -> tuple[WorkflowRunner, BrainStore, WorkflowStore, ChaosWorld]:
    store = BrainStore(workspace)
    install_store_faults(store)
    wf = WorkflowStore(workspace / ".aieb" / "workflow" / "workflow.sqlite3")
    world = ChaosWorld(workspace)
    deps = make_deps(store, wf, workspace)
    return WorkflowRunner(deps=deps, workspace=workspace), store, wf, world


def _with_env(overrides: dict[str, str]):
    class _Ctx:
        def __enter__(self):
            self.saved = {k: os.environ.get(k) for k in overrides}
            os.environ.update(overrides)
            return self

        def __exit__(self, *exc):  # noqa: ANN002
            for key, value in self.saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            return False

    return _Ctx()


def run_concurrency_scenarios() -> None:
    # C1：连续两次 resume
    ws = _reset("C1_double_resume")
    runner, store, wf, world = _runner(ws)
    try:
        snap = runner.start_sync(name="周会.txt", text=TEXT, to_addr="team@example.com")
        pending = runner.pending_approval_sync(snap.workflow_id)
        command = ResumeCommand(
            decision="approve", version=pending.version, draft_hash=pending.draft_hash
        )
        first = runner.resume_sync(snap.workflow_id, command)
        second = runner.resume_sync(snap.workflow_id, command)
        counts = {"meetings": len(store.list_meetings()), "sends": world.sent_count()}
        _record(
            scenario="C1 同一 thread 连续两次 approve",
            inject_at="第二次 resume（终态/无挂起中断）",
            expected="只发 1 封，第二次返回当前快照",
            actual=f"{first.execution_status}/{second.execution_status}",
            recovered="幂等返回",
            counts=counts,
            duplicate=max(0, counts["sends"] - 1),
            ok=counts["sends"] == 1 and second.execution_status == "completed",
        )
    finally:
        runner.close()
        store.close()
        wf.close()

    # C2：并发两次 resume
    ws = _reset("C2_concurrent_resume")
    runner, store, wf, world = _runner(ws)
    try:
        snap = runner.start_sync(name="周会.txt", text=TEXT, to_addr="team@example.com")
        pending = runner.pending_approval_sync(snap.workflow_id)
        command = ResumeCommand(
            decision="approve", version=pending.version, draft_hash=pending.draft_hash
        )
        results: list[str] = []
        lock = threading.Lock()

        def worker() -> None:
            try:
                out = runner.resume_sync(snap.workflow_id, command)
                with lock:
                    results.append(out.execution_status)
            except Exception as exc:  # noqa: BLE001
                with lock:
                    results.append(f"error:{type(exc).__name__}")

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=120)
        counts = {"meetings": len(store.list_meetings()), "sends": world.sent_count()}
        _record(
            scenario="C2 两个并发 approve",
            inject_at="两个线程同时 resume",
            expected="只有一个执行发送，另一个拿到幂等结果；总数 1 封",
            actual=f"results={results}",
            recovered="图在同一 loop 内串行；幂等键兜底",
            counts=counts,
            duplicate=max(0, counts["sends"] - 1),
            ok=counts["sends"] == 1,
        )
    finally:
        runner.close()
        store.close()
        wf.close()

    # C3：approve 与 cancel 竞争
    ws = _reset("C3_approve_vs_cancel")
    runner, store, wf, world = _runner(ws)
    try:
        snap = runner.start_sync(name="周会.txt", text=TEXT, to_addr="team@example.com")
        pending = runner.pending_approval_sync(snap.workflow_id)
        command = ResumeCommand(
            decision="approve", version=pending.version, draft_hash=pending.draft_hash
        )
        out: list[str] = []

        def do_approve() -> None:
            try:
                out.append(runner.resume_sync(snap.workflow_id, command).execution_status)
            except Exception as exc:  # noqa: BLE001
                out.append(f"error:{type(exc).__name__}")

        def do_cancel() -> None:
            try:
                out.append(runner.cancel_sync(snap.workflow_id, note="race").execution_status)
            except Exception as exc:  # noqa: BLE001
                out.append(f"error:{type(exc).__name__}")

        threads = [threading.Thread(target=do_approve), threading.Thread(target=do_cancel)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=120)
        counts = {"meetings": len(store.list_meetings()), "sends": world.sent_count()}
        _record(
            scenario="C3 approve 与 cancel 同时到达",
            inject_at="两个线程分别 approve / cancel",
            expected="最终状态唯一；发送次数 ≤ 1",
            actual=f"results={out}",
            recovered="以先完成者为终态，另一个走幂等分支",
            counts=counts,
            duplicate=max(0, counts["sends"] - 1),
            ok=counts["sends"] <= 1,
        )
    finally:
        runner.close()
        store.close()
        wf.close()

    # C4：同一份会议文本重复 start
    ws = _reset("C4_duplicate_start")
    runner, store, wf, world = _runner(ws)
    try:
        a = runner.start_sync(name="周会.txt", text=TEXT, to_addr="team@example.com")
        b = runner.start_sync(name="周会.txt", text=TEXT, to_addr="team@example.com")
        counts = {"meetings": len(store.list_meetings()), "workflows": len(wf.list_runs())}
        _record(
            scenario="C4 同一文本重复 start",
            inject_at="第二次 start，allow_reuse 默认开启",
            expected="复用同一工作流；业务数据只落一次（会议 1 条、run 1 条）",
            actual=f"{a.workflow_id} vs {b.workflow_id} meetings={counts['meetings']}",
            recovered="按 text_hash 复用未完成 run",
            counts=counts,
            duplicate=max(0, counts["meetings"] - 1),
            ok=a.workflow_id == b.workflow_id and counts["meetings"] == 1
            and counts["workflows"] == 1,
        )
    finally:
        runner.close()
        store.close()
        wf.close()

    # C5：多个不同工作流并发
    ws = _reset("C5_multi_workflow")
    runner, store, wf, world = _runner(ws)
    try:
        errors: list[str] = []
        snaps: list[str] = []

        def one(index: int) -> None:
            try:
                text = f"{TEXT}\n附注：第{index}场并行会议记录。"
                snap = runner.start_sync(
                    name=f"周会{index}.txt", text=text, to_addr="team@example.com"
                )
                pending = runner.pending_approval_sync(snap.workflow_id)
                runner.resume_sync(
                    snap.workflow_id,
                    ResumeCommand(
                        decision="approve",
                        version=pending.version,
                        draft_hash=pending.draft_hash,
                    ),
                )
                snaps.append(snap.workflow_id)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{type(exc).__name__}: {exc}")

        threads = [threading.Thread(target=one, args=(i,)) for i in range(1, 4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=180)
        counts = {
            "meetings": len(store.list_meetings()),
            "workflows": len(wf.list_runs()),
            "sends": world.sent_count(),
        }
        _record(
            scenario="C5 三个不同工作流并发",
            inject_at="3 线程各自 start+approve",
            expected="3 条会议、3 个工作流、3 封邮件，互不串扰",
            actual=f"workflows={counts['workflows']} errors={errors}",
            recovered="各自独立 thread_id",
            counts=counts,
            duplicate=0,
            ok=counts["meetings"] == 3 and counts["sends"] == 3 and not errors,
        )
    finally:
        runner.close()
        store.close()
        wf.close()


def run_fault_scenarios() -> None:
    # F1：LLM 持续超时
    ws = _reset("F1_llm_timeout")
    with _with_env({"AIEB_CHAOS_ANALYZE_FAILS": "5"}):
        runner, store, wf, world = _runner(ws)
        try:
            from aieb.workflow.runner import WorkflowError as WErr

            try:
                runner.start_sync(name="周会.txt", text=TEXT, to_addr="team@example.com")
                failed = False
            except WErr:
                failed = True
            events = wf.list_events((wf.list_runs() or [{}])[0].get("workflow_id", ""))
            retries = [e for e in events if e["event"] == "retry"]
            attempts = 0
            counter = ws / "chaos_counter.json"
            if counter.is_file():
                attempts = int(json.loads(counter.read_text(encoding="utf-8")).get("analyze", 0))
            counts = {"meetings": len(store.list_meetings()), "retries": len(retries),
                      "analyze_attempts": attempts}
            _record(
                scenario="F1 LLM 连续超时",
                inject_at="analyzer 前 5 次抛 RuntimeError",
                expected="只尝试 2 次（1 次原始 + 1 次重试）后失败；不落业务数据；每次失败都有 retry 记录",
                actual=f"failed={failed} attempts={attempts} retry_events={len(retries)}",
                recovered="人工重跑（continue_run）",
                counts=counts,
                duplicate=0,
                ok=failed and counts["meetings"] == 0 and attempts == 2 and len(retries) == 2,
            )
        finally:
            runner.close()
            store.close()
            wf.close()

    # F2：LLM 返回非法 JSON
    ws = _reset("F2_llm_invalid_json")
    with _with_env({"AIEB_CHAOS_ANALYZE_INVALID": "1"}):
        runner, store, wf, world = _runner(ws)
        try:
            from aieb.workflow.runner import WorkflowError as WErr

            try:
                runner.start_sync(name="周会.txt", text=TEXT, to_addr="team@example.com")
                failed = False
            except WErr:
                failed = True
            counts = {"meetings": len(store.list_meetings())}
            _record(
                scenario="F2 LLM 返回非法 JSON",
                inject_at="analyzer 抛 ValueError",
                expected="失败并如实上报；不写业务数据",
                actual=f"failed={failed}",
                recovered="需人工检查 prompt/模型",
                counts=counts,
                duplicate=0,
                ok=failed and counts["meetings"] == 0,
            )
        finally:
            runner.close()
            store.close()
            wf.close()

    # F3：业务落库失败（SQLite 写入失败）
    ws = _reset("F3_persist_failure")
    with _with_env({"AIEB_CHAOS_PERSIST_FAILS": "1"}):
        runner, store, wf, world = _runner(ws)
        try:
            from aieb.workflow.runner import WorkflowError as WErr

            try:
                snap = runner.start_sync(name="周会.txt", text=TEXT, to_addr="team@example.com")
                failed = False
                workflow_id = snap.workflow_id
            except WErr:
                failed = True
                workflow_id = (wf.list_runs() or [{}])[0].get("workflow_id", "")
            effects = {e["action"]: e["status"] for e in wf.list_effects(workflow_id)}
            # 再续跑一次（故障只注入一次）
            recovered = ""
            if workflow_id:
                try:
                    recovered = runner.continue_run_sync(workflow_id).execution_status
                except Exception as exc:  # noqa: BLE001
                    recovered = f"error:{type(exc).__name__}"
            counts = {"meetings": len(store.list_meetings())}
            _record(
                scenario="F3 业务落库失败（SQLite 写入）",
                inject_at="store.add_meeting 抛 database is locked",
                expected="幂等键记为 failed；续跑后成功且只 1 条会议",
                actual=f"failed={failed} effect={effects.get('persist_meeting')}",
                recovered=f"continue→{recovered}",
                counts=counts,
                duplicate=max(0, counts["meetings"] - 1),
                ok=failed and recovered == "waiting_approval" and counts["meetings"] == 1,
            )
        finally:
            runner.close()
            store.close()
            wf.close()

    # F4：业务库被外部锁住（真实 sqlite 锁）
    ws = _reset("F4_sqlite_locked")
    runner, store, wf, world = _runner(ws)
    blocker = sqlite3.connect(str(store.path))
    try:
        blocker.execute("BEGIN EXCLUSIVE")
        started = time.perf_counter()
        from aieb.workflow.runner import WorkflowError as WErr

        try:
            runner.start_sync(name="周会.txt", text=TEXT, to_addr="team@example.com")
            locked_error = ""
        except WErr as exc:
            locked_error = str(exc)[:80]
        elapsed = round((time.perf_counter() - started) * 1000, 1)
        blocker.rollback()
        counts = {"meetings": len(store.list_meetings())}
        _record(
            scenario="F4 业务库被悲观锁占用",
            inject_at="另一连接 BEGIN EXCLUSIVE 持有写锁",
            expected="给出明确失败（不挂死、不静默成功），无重复数据",
            actual=f"error={locked_error or '无'} 耗时 {elapsed}ms",
            recovered="释放锁后可重跑（见 F3 的 continue 路径）",
            counts=counts,
            duplicate=0,
            ok=bool(locked_error) and counts["meetings"] == 0,
        )
    finally:
        blocker.close()
        runner.close()
        store.close()
        wf.close()

    # F5：Checkpoint 不可用（把真正的 checkpoint 文件写成非 sqlite 内容）
    ws = _reset("F5_checkpoint_unavailable")
    broken = ws / ".aieb" / "workflow" / "checkpoints.sqlite3"
    broken.parent.mkdir(parents=True, exist_ok=True)
    broken.write_text("not a sqlite file", encoding="utf-8")
    ok = False
    error = ""
    try:
        runner, store, wf, world = _runner(ws)
        try:
            runner.start_sync(name="周会.txt", text=TEXT, to_addr="team@example.com")
            error = "未报错"
        except Exception as exc:  # noqa: BLE001
            error = f"{type(exc).__name__}: {str(exc)[:60]}"
            ok = True
        finally:
            runner.close()
            store.close()
            wf.close()
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {str(exc)[:60]}"
        ok = True
    meetings = 0
    try:
        probe = BrainStore(ws)
        meetings = len(probe.list_meetings())
        probe.close()
    except Exception:  # noqa: BLE001
        meetings = -1
    _record(
        scenario="F5 Checkpoint 库损坏",
        inject_at="checkpoint 文件写成非 sqlite 内容",
        expected="启动/首次调用即报错，不产生业务数据",
        actual=error,
        recovered="需修复/重建 checkpoint（业务库不受影响）",
        counts={"meetings": meetings},
        duplicate=0,
        ok=ok and meetings == 0,
    )

    # F6：SMTP 结果不确定（抛异常）
    ws = _reset("F6_smtp_exception")
    with _with_env({"AIEB_CHAOS_SEND": "raise"}):
        runner, store, wf, world = _runner(ws)
        try:
            snap = runner.start_sync(name="周会.txt", text=TEXT, to_addr="team@example.com")
            pending = runner.pending_approval_sync(snap.workflow_id)
            out = runner.resume_sync(
                snap.workflow_id,
                ResumeCommand(
                    decision="approve", version=pending.version, draft_hash=pending.draft_hash
                ),
            )
            email = store.get_email((wf.get_run(snap.workflow_id) or {}).get("email_draft_id", ""))
            effects = {e["action"]: e["status"] for e in wf.list_effects(snap.workflow_id)}
            retry_error = ""
            try:
                runner.retry_send_sync(snap.workflow_id)
            except WorkflowError as exc:
                retry_error = str(exc)[:40]
            counts = {
                "meetings": len(store.list_meetings()),
                "email_status": (email or {}).get("status"),
                "effect": effects.get("send_email"),
            }
            _record(
                scenario="F6 SMTP 抛异常（结果不确定）",
                inject_at="sender 抛 TimeoutError",
                expected="邮件标记 uncertain、工作流 failed、retry_send 被拒绝",
                actual=f"status={out.execution_status} email={counts['email_status']} "
                       f"effect={counts['effect']}",
                recovered=f"人工核实；retry 拒绝={bool(retry_error)}",
                counts=counts,
                duplicate=0,
                ok=out.execution_status == "failed"
                and counts["email_status"] == "uncertain"
                and counts["effect"] == "uncertain"
                and bool(retry_error),
            )
        finally:
            runner.close()
            store.close()
            wf.close()

    # F7：SMTP 明确拒绝（可人工重试）
    ws = _reset("F7_smtp_refused")
    with _with_env({"AIEB_CHAOS_SEND": "fail"}):
        runner, store, wf, world = _runner(ws)
        try:
            snap = runner.start_sync(name="周会.txt", text=TEXT, to_addr="team@example.com")
            pending = runner.pending_approval_sync(snap.workflow_id)
            out = runner.resume_sync(
                snap.workflow_id,
                ResumeCommand(
                    decision="approve", version=pending.version, draft_hash=pending.draft_hash
                ),
            )
            effects = {e["action"]: e["status"] for e in wf.list_effects(snap.workflow_id)}
            counts = {"meetings": len(store.list_meetings()), "effect": effects.get("send_email")}
            _record(
                scenario="F7 SMTP 明确拒绝（550）",
                inject_at="sender 返回 ok=False",
                expected="邮件 failed、幂等键 failed（允许人工 retry）",
                actual=f"status={out.execution_status} effect={counts['effect']}",
                recovered="workflow.retry 可重发",
                counts=counts,
                duplicate=0,
                ok=out.execution_status == "failed" and counts["effect"] == "failed",
            )
        finally:
            runner.close()
            store.close()
            wf.close()

    # F8：审批已过期（版本不匹配）
    ws = _reset("F8_stale_approval")
    runner, store, wf, world = _runner(ws)
    try:
        snap = runner.start_sync(name="周会.txt", text=TEXT, to_addr="team@example.com")
        pending = runner.pending_approval_sync(snap.workflow_id)
        error = ""
        try:
            runner.resume_sync(
                snap.workflow_id,
                ResumeCommand(
                    decision="approve",
                    version=pending.version + 1,
                    draft_hash=pending.draft_hash,
                ),
            )
        except WorkflowError as exc:
            error = str(exc)[:60]
        counts = {"sends": world.sent_count()}
        _record(
            scenario="F8 用旧版本审批新草稿",
            inject_at="ResumeCommand.version + 1",
            expected="拒绝执行、不发信",
            actual=error or "未拒绝",
            recovered="要求刷新后重新审批",
            counts=counts,
            duplicate=0,
            ok=bool(error) and counts["sends"] == 0,
        )
    finally:
        runner.close()
        store.close()
        wf.close()


def main() -> int:
    shutil.rmtree(WORK, ignore_errors=True)
    WORK.mkdir(parents=True, exist_ok=True)
    print("== R 组：真实进程崩溃 + 重启 ==")
    run_restart_scenarios()
    print("\n== C 组：并发与重复请求 ==")
    run_concurrency_scenarios()
    print("\n== F 组：故障注入 ==")
    run_fault_scenarios()

    passed = sum(1 for r in rows if r["ok"])
    payload = {
        "total": len(rows),
        "passed": passed,
        "failed": len(rows) - passed,
        "rows": rows,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    common.write_json(OUT, payload)
    print()
    print(f"共 {len(rows)} 个场景，通过 {passed}，未通过 {len(rows) - passed}")
    for row in rows:
        if not row["ok"]:
            print(f"  x {row['scenario']} -> {row['actual']}")
    print(f"结果 → {OUT}")
    return 0 if passed == len(rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
