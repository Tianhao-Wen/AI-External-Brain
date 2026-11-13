"""导出一次真实端到端工作流的 Trace（脱敏），并校验可观测性字段是否齐全。

默认走真实 DeepSeek（1 次分析 + 1 次草稿 = 2 次 LLM 调用）；若没有 API Key 则退化到
确定性 fake，并在输出里明确标注 `mode`，**不会把 fake 结果说成真实 LLM 结果**。

    python scripts/export_trace_sample.py

产出：
* `docs/samples/workflow_trace_sample.json`（脱敏后）
* `docs/LANGGRAPH_TRACE_SAMPLE.md`（人读版）
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from aieb.brain.store import BrainStore  # noqa: E402
from aieb.config import ProviderConfig  # noqa: E402
from aieb.workflow.deps import build_real_deps  # noqa: E402
from aieb.workflow.persistence import WorkflowStore  # noqa: E402
from aieb.workflow.runner import WorkflowRunner  # noqa: E402
from aieb.workflow.state import ResumeCommand  # noqa: E402
from benchmarks.workflow.chaos import make_deps  # noqa: E402
from tests.fake_smtp import FakeSMTPServer  # noqa: E402

WORK = ROOT / "benchmarks" / ".work" / "trace_sample"
OUT_JSON = ROOT / "docs" / "samples" / "workflow_trace_sample.json"
OUT_MD = ROOT / "docs" / "LANGGRAPH_TRACE_SAMPLE.md"

TEXT = (
    "客户支持周会（2026年11月10日）\n"
    "参会人：陈晨、刘洋、周宇\n"
    "一、上周工单412张，首次响应全部在24小时内，SLA达标。\n"
    "二、知识库更新：决定本周内把新版退换货政策写入知识库，由陈晨负责，11月12日前完成。\n"
    "三、值班安排：下周值班由刘洋负责。\n"
    "附注：工单系统在上周三出现过一次约20分钟的不可用，已恢复，尚未定位原因。\n"
)

#: Trace 里必须存在的字段（对照任务书的可观测性清单）
REQUIRED_EVENT_FIELDS = (
    "workflow_id",
    "node",
    "event",
    "status",
    "started_at",
    "ended_at",
    "duration_ms",
    "model",
    "input_tokens",
    "output_tokens",
    "retry",
    "error_type",
    "checkpoint_id",
    "detail",
)

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
KEY_RE = re.compile(r"sk-[A-Za-z0-9]{10,}")


def redact(value):
    if isinstance(value, dict):
        return {k: redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, str):
        return KEY_RE.sub("sk-***REDACTED***", EMAIL_RE.sub("***@example.com", value))
    return value


def live_service(provider: ProviderConfig, store: BrainStore):
    class _Service:
        def __init__(self) -> None:
            self.provider = provider
            self.store = store

    return _Service()


def main() -> int:
    shutil.rmtree(WORK, ignore_errors=True)
    WORK.mkdir(parents=True, exist_ok=True)
    store = BrainStore(WORK)
    wf = WorkflowStore(WORK / ".aieb" / "workflow" / "workflow.sqlite3")

    key = os.environ.get("DEEPSEEK_API_KEY", "")
    if key:
        provider = ProviderConfig(
            name="deepseek-live",
            protocol="openai-compat",
            base_url=os.environ.get("AIEB_LIVE_BASE_URL", "https://api.deepseek.com"),
            model=os.environ.get("AIEB_LIVE_MODEL", "deepseek-chat"),
            api_key=key,
        )
        deps = build_real_deps(
            service=live_service(provider, store), wf_store=wf, workspace=WORK
        )
        mode = f"live:{provider.model}"
    else:
        deps = make_deps(store, wf, WORK)
        mode = "fixture（未检测到 DEEPSEEK_API_KEY）"

    runner = WorkflowRunner(deps=deps, workspace=WORK)
    smtp = FakeSMTPServer()
    smtp.start()
    saved_env = {k: os.environ.get(k) for k in smtp.env()}
    os.environ.update(smtp.env())
    os.environ.pop("AIEB_EMAIL_DRY_RUN", None)
    try:
        snap = runner.start_sync(
            name="客户支持周会.txt", text=TEXT, to_addr="ops@example.com", session_id="trace-demo"
        )
        pending = runner.pending_approval_sync(snap.workflow_id)
        if pending is not None:
            snap = runner.resume_sync(
                snap.workflow_id,
                ResumeCommand(
                    decision="approve",
                    version=pending.version,
                    draft_hash=pending.draft_hash,
                    note="trace 导出演示",
                    decided_by="trace-demo",
                ),
            )
        history = runner.history_sync(snap.workflow_id) if hasattr(runner, "history_sync") else runner.history(snap.workflow_id)
    finally:
        runner.close()
        smtp.stop()
        for key, value in saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    events = history["events"]
    run = history["run"]
    missing = [
        field for field in REQUIRED_EVENT_FIELDS
        if events and (field not in events[0] or field not in {"workflow_id", "detail"})
    ]
    # workflow_id 在 events 行里是列，detail 是 JSON；上面统一按列名检查
    missing = [f for f in REQUIRED_EVENT_FIELDS if f not in events[0]] if events else list(REQUIRED_EVENT_FIELDS)
    payload = {
        "mode": mode,
        "workflow_id": snap.workflow_id,
        "thread_id": run.get("thread_id"),
        "status": run.get("status"),
        "token_totals": history["tokens"],
        "effects": [
            {k: e[k] for k in ("action", "status", "object_id", "version", "attempts")}
            for e in history["effects"]
        ],
        "events": events,
        "slowest_node": max(events, key=lambda e: e["duration_ms"])["node"] if events else "",
        "failed_nodes": [e["node"] for e in events if e["status"] == "error"],
        "checkpoints": sorted({e["checkpoint_id"] for e in events if e["checkpoint_id"]}),
        "redaction": {
            "emails": "替换为 ***@example.com",
            "api_keys": "形如 sk-... 的字符串全量替换为 ***REDACTED***",
            "meeting_text": "Trace 里不含会议原文（原文只在上传请求与业务库 raw_text 中）",
            "external_upload": "全部写本地 SQLite，未接入 LangSmith 或任何外部观测平台",
        },
    }
    redacted = redact(payload)
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(redacted, ensure_ascii=False, indent=2), encoding="utf-8")

    # 脱敏自检：导出的文件里不允许出现真实邮箱或 key
    text = OUT_JSON.read_text(encoding="utf-8")
    leak = KEY_RE.findall(text) + [m for m in EMAIL_RE.findall(text) if "example.com" not in m]

    lines = [
        "# 一次真实工作流的 Trace 样例（已脱敏）",
        "",
        f"- 执行模式：`{mode}`",
        f"- workflow_id：`{snap.workflow_id}`　thread_id：`{run.get('thread_id')}`",
        f"- 终态：`{run.get('status')}`　Token 合计：in {history['tokens']['input_tokens']} / "
        f"out {history['tokens']['output_tokens']}",
        f"- 最慢节点：`{payload['slowest_node']}`",
        f"- 失败节点：{payload['failed_nodes'] or '无'}",
        f"- Fake SMTP 投递成功邮件数：{len(smtp.messages)}（未真实发信）",
        "",
        "## 节点级事件",
        "",
        "| seq | node | event | status | duration_ms | model | in/out | retry | error_type | checkpoint_id |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for e in events:
        lines.append(
            f"| {e['seq']} | {e['node']} | {e['event']} | {e['status']} | {e['duration_ms']} | "
            f"{e['model'] or '-'} | {e['input_tokens']}/{e['output_tokens']} | {e['retry']} | "
            f"{e['error_type'] or '-'} | {(e['checkpoint_id'] or '-')[:12]} |"
        )
    lines += [
        "",
        "## 幂等键（副作用账本）",
        "",
        "| action | status | object_id | version | attempts |",
        "| --- | --- | --- | --- | --- |",
    ]
    for e in payload["effects"]:
        lines.append(
            f"| {e['action']} | {e['status']} | {e['object_id']} | {e['version']} | {e['attempts']} |"
        )
    lines += [
        "",
        "## 脱敏说明",
        "",
        "- 邮箱地址、API Key 在导出时被替换；会议原文不进入 Trace。",
        "- Trace 只写本地 SQLite（`workflow_events` 表），默认不上传任何外部平台。",
        "",
        f"字段完整性检查：`{'通过' if not missing else '缺失 ' + str(missing)}`；"
        f"脱敏自检：`{'通过' if not leak else '发现疑似泄露 ' + str(leak)}`",
    ]
    OUT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"mode={mode}")
    print(f"事件数={len(events)} 最慢节点={payload['slowest_node']} "
          f"checkpoint 数={len(payload['checkpoints'])}")
    print(f"字段缺失={missing or '无'}")
    print(f"脱敏疑似泄露={leak or '无'}")
    print(f"→ {OUT_JSON}")
    print(f"→ {OUT_MD}")
    store.close()
    wf.close()
    return 0 if not missing and not leak else 1


if __name__ == "__main__":
    raise SystemExit(main())
