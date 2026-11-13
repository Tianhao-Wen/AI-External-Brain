"""故障注入实验的子进程入口：一次只做一件事，然后退出（用于真实"进程崩溃/重启"）。

父进程（`fault_injection.py`）用 `subprocess` 调它，因此：
* Checkpoint 与业务库都是**真实文件**，跨进程仍然有效；
* `AIEB_CHAOS_CRASH_AT` 触发 `os._exit(9)` 时是**真正**的进程死亡（没有 finally 清理）。

命令：
    python -m benchmarks.workflow.chaos_child start    --workspace W --name N --text-file F
    python -m benchmarks.workflow.chaos_child status   --workspace W --id WF
    python -m benchmarks.workflow.chaos_child approve  --workspace W --id WF
    python -m benchmarks.workflow.chaos_child reject   --workspace W --id WF
    python -m benchmarks.workflow.chaos_child edit     --workspace W --id WF
    python -m benchmarks.workflow.chaos_child continue --workspace W --id WF
    python -m benchmarks.workflow.chaos_child retry    --workspace W --id WF

输出：最后一行是 JSON（父进程解析）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from aieb.brain.store import BrainStore
from aieb.workflow.persistence import WorkflowStore
from aieb.workflow.runner import WorkflowError, WorkflowRunner
from aieb.workflow.state import ResumeCommand
from benchmarks.workflow.chaos import ChaosWorld, install_store_faults, make_deps


def _emit(payload: dict) -> None:
    print("__RESULT__" + json.dumps(payload, ensure_ascii=False))


def _counts(store: BrainStore, world: ChaosWorld) -> dict[str, int]:
    return {
        "meetings": len(store.list_meetings()),
        "risks": len(store.list_risks()),
        "todos": len(store.list_todos()),
        "emails": len(store.list_emails()),
        "sends": world.sent_count(),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action")
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--id", default="")
    parser.add_argument("--name", default="会议纪要.txt")
    parser.add_argument("--text-file", default="")
    parser.add_argument("--to", default="team@example.com")
    args = parser.parse_args(argv)

    workspace = Path(args.workspace)
    store = BrainStore(workspace)
    install_store_faults(store)
    wf = WorkflowStore(workspace / ".aieb" / "workflow" / "workflow.sqlite3")
    world = ChaosWorld(workspace)
    deps = make_deps(store, wf, workspace)
    runner = WorkflowRunner(deps=deps, workspace=workspace)

    payload: dict = {"action": args.action, "ok": True, "error": ""}
    try:
        if args.action == "start":
            text = Path(args.text_file).read_text(encoding="utf-8")
            snap = runner.start_sync(name=args.name, text=text, to_addr=args.to)
            payload["workflow_id"] = snap.workflow_id
            payload["status"] = snap.execution_status
        else:
            workflow_id = args.id
            if args.action == "status":
                snap = runner.status_sync(workflow_id)
                payload["status"] = snap.execution_status
                payload["resumable"] = snap.resumable
                payload["current_step"] = snap.current_step
            elif args.action in {"approve", "reject", "edit"}:
                pending = runner.pending_approval_sync(workflow_id)
                if pending is None:
                    payload.update({"ok": False, "error": "没有待审批的中断"})
                else:
                    decision = "reject" if args.action == "reject" else args.action
                    edit = (
                        {
                            "edit_subject": pending.subject + "（已修订）",
                            "edit_body": pending.body + "\n[审批意见] 请确认。",
                        }
                        if decision == "edit"
                        else {}
                    )
                    snap = runner.resume_sync(
                        workflow_id,
                        ResumeCommand(
                            decision=decision,  # type: ignore[arg-type]
                            version=pending.version,
                            draft_hash=pending.draft_hash,
                            note="chaos-controller",
                            decided_by="chaos-controller",
                        ),
                        edit=edit,
                    )
                    payload["status"] = snap.execution_status
                    if snap.resumable:
                        # edit 之后还要再确认一次（版本已 +1）
                        again = runner.pending_approval_sync(workflow_id)
                        if again is not None:
                            snap = runner.resume_sync(
                                workflow_id,
                                ResumeCommand(
                                    decision="approve",
                                    version=again.version,
                                    draft_hash=again.draft_hash,
                                    note="修改后确认",
                                    decided_by="chaos-controller",
                                ),
                            )
                            payload["status"] = snap.execution_status
                            payload["email_version"] = snap.email_version
            elif args.action == "continue":
                snap = runner.continue_run_sync(workflow_id)
                payload["status"] = snap.execution_status
            elif args.action == "retry":
                snap = runner.retry_send_sync(workflow_id)
                payload["status"] = snap.execution_status
            else:
                payload.update({"ok": False, "error": f"未知动作 {args.action}"})
        run = wf.get_run(payload.get("workflow_id") or args.id) or {}
        payload["run_status"] = run.get("status", "")
        payload["counts"] = _counts(store, world)
        payload["effects"] = {
            e["action"]: e["status"] for e in wf.list_effects(payload.get("workflow_id") or args.id)
        }
        payload["events"] = [
            {
                "node": e["node"],
                "event": e["event"],
                "status": e["status"],
                "duration_ms": e["duration_ms"],
            }
            for e in wf.list_events(payload.get("workflow_id") or args.id)
        ]
    except WorkflowError as exc:
        payload.update({"ok": False, "error": str(exc)})
        payload["counts"] = _counts(store, world)
    except Exception as exc:  # noqa: BLE001
        payload.update({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
        payload["counts"] = _counts(store, world)
    finally:
        runner.close()
        store.close()
        wf.close()

    _emit(payload)
    return 0 if payload.get("ok") else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
