"""工作流测试夹具：真实 SQLite（业务库 + 编排库 + Checkpoint），LLM/SMTP 用确定性 Fake。

**为什么不用 Mock 掉 SQLite**：本次要验证的正是"持久化 + 重启恢复"，
把存储换成内存对象就等于把被测对象删掉了。所以：

* `brain.sqlite3`（业务）→ 真实 `BrainStore`，位于 `tmp_path`；
* `workflow.sqlite3`（编排/Trace/幂等）→ 真实 `WorkflowStore`；
* `checkpoints.sqlite3` → 真实 `SqliteSaver`；
* LLM / SMTP → Fake（可注入失败、可统计调用次数）。

`tmp_path` 保证每个用例独立 workspace，绝不碰正式 `.aieb/`。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from aieb.brain.meetings import MeetingAnalysis, RiskIn, TodoIn
from aieb.brain.store import BrainStore
from aieb.workflow.deps import AnalysisOutcome, DraftOutcome, SendOutcome, WorkflowDeps
from aieb.workflow.persistence import WorkflowStore
from aieb.workflow.runner import WorkflowRunner

MEETING_TEXT = (
    "项目周会纪要。参会人：张伟、李娜。张伟说：上线日期已经延期两周，"
    "原因是接口联调一直不通过，这个风险还没有解决。"
    "李娜负责在3月15日前补齐测试用例。会议决定：本周五前完成联调。"
)


def default_analysis() -> MeetingAnalysis:
    return MeetingAnalysis(
        title="项目周会",
        meeting_time="",
        location="",
        participants=["张伟", "李娜"],
        discussion=["接口联调延期"],
        decisions=["本周五前完成联调"],
        summary="上线延期两周，联调未通过。",
        risks=[
            RiskIn(
                title="上线延期两周",
                level="high",
                risk_type="进度",
                status="open",
                description="接口联调一直不通过导致上线延期",
                evidence="上线日期已经延期两周，原因是接口联调一直不通过",
                suggestion="加派人力",
            )
        ],
        todos=[
            TodoIn(content="补齐测试用例", owner="李娜", due_date="3月15日", priority="high")
        ],
    )


@dataclass
class Fakes:
    """可控的假外部世界。"""

    analysis_calls: int = 0
    draft_calls: int = 0
    send_calls: int = 0
    sent: list[dict[str, str]] = field(default_factory=list)
    analysis_failures: int = 0          # 前 N 次分析抛错
    draft_failures: int = 0
    send_raises: int = 0                # 前 N 次发送抛异常（模拟 SMTP 未知状态）
    send_fails: int = 0                 # 前 N 次发送返回 ok=False
    send_uncertain: int = 0             # 前 N 次发送返回 uncertain
    analysis: MeetingAnalysis | None = None

    def reset_counters(self) -> None:
        self.analysis_calls = 0
        self.draft_calls = 0
        self.send_calls = 0


class FakeWorld:
    def __init__(self, fakes: Fakes) -> None:
        self.fakes = fakes

    async def analyzer(self, name: str, text: str) -> AnalysisOutcome:
        self.fakes.analysis_calls += 1
        if self.fakes.analysis_failures > 0:
            self.fakes.analysis_failures -= 1
            raise RuntimeError("LLM 超时（测试注入）")
        return AnalysisOutcome(
            analysis=self.fakes.analysis or default_analysis(),
            input_tokens=120,
            output_tokens=60,
            model="fake-model",
        )

    async def drafter(self, meeting: dict[str, Any], to_addr: str, extra: str) -> DraftOutcome:
        self.fakes.draft_calls += 1
        if self.fakes.draft_failures > 0:
            self.fakes.draft_failures -= 1
            raise RuntimeError("草稿生成失败（测试注入）")
        title = meeting.get("title") or "会议"
        return DraftOutcome(
            subject=f"【会议提醒】{title}",
            body=f"风险 {len(meeting.get('risks') or [])} 条，待办 {len(meeting.get('todos') or [])} 条。",
            input_tokens=90,
            output_tokens=45,
            model="fake-model",
        )

    def sender(self, to_addr: str, subject: str, body: str) -> SendOutcome:
        self.fakes.send_calls += 1
        if self.fakes.send_raises > 0:
            self.fakes.send_raises -= 1
            raise TimeoutError("SMTP 读超时（测试注入）")
        if self.fakes.send_fails > 0:
            self.fakes.send_fails -= 1
            return SendOutcome(ok=False, message="550 mailbox unavailable", error_code="smtp_error")
        if self.fakes.send_uncertain > 0:
            self.fakes.send_uncertain -= 1
            return SendOutcome(ok=False, message="连接在提交后被重置", uncertain=True)
        self.fakes.sent.append({"to": to_addr, "subject": subject, "body": body})
        return SendOutcome(ok=True, message="accepted")


@dataclass
class Harness:
    workspace: Path
    store: BrainStore
    wf: WorkflowStore
    deps: WorkflowDeps
    runner: WorkflowRunner
    fakes: Fakes
    world: FakeWorld

    def reopen_runner(self) -> WorkflowRunner:
        """模拟"服务重启"：关掉当前 Runner，用同样的磁盘重开一个。"""
        self.runner.close()
        self.runner = WorkflowRunner(deps=self.deps, workspace=self.workspace)
        return self.runner

    def close(self) -> None:
        self.runner.close()


def make_harness(tmp_path: Path):
    """创建一套独立工作区（真实 SQLite + Fake 外部世界），用 yield 交回夹具。"""
    workspace = tmp_path / "ws"
    workspace.mkdir(parents=True, exist_ok=True)
    store = BrainStore(workspace)
    wf = WorkflowStore(workspace / ".aieb" / "workflow" / "workflow.sqlite3")
    fakes = Fakes()
    world = FakeWorld(fakes)
    deps = WorkflowDeps(
        store=store,
        wf=wf,
        workspace=workspace,
        analyzer=world.analyzer,
        drafter=world.drafter,
        sender=world.sender,
        model_name="fake-model",
        activity=store.add_activity,
    )
    runner = WorkflowRunner(deps=deps, workspace=workspace)
    h = Harness(workspace, store, wf, deps, runner, fakes, world)
    try:
        yield h
    finally:
        h.close()
        store.close()
        wf.close()


def dump(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)
