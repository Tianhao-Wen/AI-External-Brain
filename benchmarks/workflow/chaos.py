"""故障注入用的确定性依赖（跨进程可用）。

与 `tests/workflow_harness.py` 的区别：
* 这里的 fake **不依赖 pytest**，可以被独立子进程使用（真实进程崩溃/重启实验）；
* 发送副作用写到工作区文件 `sends.log`，跨进程可计数；
* 故障行为由环境变量控制：
  - `AIEB_CHAOS_CRASH_AT=<node>`   进入该节点前直接 `os._exit(9)`（真崩溃，不走清理逻辑）
  - `AIEB_CHAOS_ANALYZE_FAILS=<n>` 前 n 次分析抛 RuntimeError（模拟 LLM 超时）
  - `AIEB_CHAOS_ANALYZE_INVALID=1` 分析返回非法结构（模拟模型输出不合法 JSON）
  - `AIEB_CHAOS_PERSIST_FAILS=<n>` 前 n 次业务落库抛异常（模拟 SQLite 写入失败）
  - `AIEB_CHAOS_SEND=uncertain|raise|fail|ok`
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from aieb.brain.meetings import MeetingAnalysis, RiskIn, TodoIn
from aieb.workflow.deps import AnalysisOutcome, DraftOutcome, SendOutcome, WorkflowDeps


def analysis() -> MeetingAnalysis:
    return MeetingAnalysis(
        title="项目周会",
        summary="确认交付节奏与风险跟进人。",
        decisions=["按两班制排产"],
        risks=[
            RiskIn(
                title="供应商交付延期",
                level="high",
                risk_type="进度",
                status="open",
                description="关键物料交付延期两周，影响联调窗口。",
                evidence="关键物料交付延期两周",
                suggestion="启动备选供应商",
            )
        ],
        todos=[
            TodoIn(
                content="给出备选供应商清单",
                owner="李娜",
                due_date="10月15日",
                priority="high",
            )
        ],
    )


class ChaosWorld:
    def __init__(self, workspace: Path) -> None:
        self.workspace = Path(workspace)
        self.sends_log = self.workspace / "sends.log"
        self.counter = self.workspace / "chaos_counter.json"
        self.workspace.mkdir(parents=True, exist_ok=True)

    # -- 环境变量 ------------------------------------------------------
    @staticmethod
    def _env_int(name: str, default: int = 0) -> int:
        try:
            return int(os.environ.get(name, str(default)) or default)
        except ValueError:
            return default

    def _bump(self, key: str) -> int:
        data: dict[str, int] = {}
        if self.counter.is_file():
            try:
                data = json.loads(self.counter.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                data = {}
        data[key] = int(data.get(key, 0)) + 1
        self.counter.write_text(json.dumps(data), encoding="utf-8")
        return data[key]

    def fault(self, node: str) -> None:
        if os.environ.get("AIEB_CHAOS_CRASH_AT", "").strip() == node:
            # 真崩溃：不写审计、不关连接，模拟 kill -9 / 断电
            os._exit(9)

    # -- 依赖 ----------------------------------------------------------
    async def analyzer(self, name: str, text: str) -> AnalysisOutcome:
        attempt = self._bump("analyze")
        fails = self._env_int("AIEB_CHAOS_ANALYZE_FAILS")
        if attempt <= fails:
            raise RuntimeError("LLM 调用超时（故障注入）")
        if os.environ.get("AIEB_CHAOS_ANALYZE_INVALID") == "1":
            raise ValueError("模型返回的结构化结果不是合法 JSON（故障注入）")
        return AnalysisOutcome(
            analysis=analysis(), input_tokens=120, output_tokens=60, model="chaos-fake"
        )

    async def drafter(
        self, meeting: dict[str, Any], to_addr: str, extra: str
    ) -> DraftOutcome:
        self._bump("draft")
        return DraftOutcome(
            subject="【会议提醒】项目周会 风险与待办",
            body="各位好：请关注供应商交付延期风险，并给出备选供应商清单。",
            input_tokens=80,
            output_tokens=40,
            model="chaos-fake",
        )

    def sender(self, to_addr: str, subject: str, body: str) -> SendOutcome:
        self._bump("send")
        mode = os.environ.get("AIEB_CHAOS_SEND", "ok")
        if mode == "raise":
            # 模拟"信可能已发出，但连接被切断" → 结果不确定
            raise TimeoutError("SMTP 连接超时（故障注入）")
        if mode == "uncertain":
            return SendOutcome(ok=False, uncertain=True, message="SMTP 结果不确定（故障注入）")
        if mode == "fail":
            return SendOutcome(ok=False, message="SMTP 550 拒绝（故障注入）", error_code="send_failed")
        with self.sends_log.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"to": to_addr, "subject": subject}, ensure_ascii=False) + "\n")
        return SendOutcome(ok=True, message="fake SMTP 已接受")

    def sent_count(self) -> int:
        if not self.sends_log.is_file():
            return 0
        return len([line for line in self.sends_log.read_text(encoding="utf-8").splitlines() if line])


def make_deps(store: Any, wf: Any, workspace: Path) -> WorkflowDeps:
    world = ChaosWorld(workspace)
    return WorkflowDeps(
        store=store,
        wf=wf,
        workspace=Path(workspace),
        analyzer=world.analyzer,
        drafter=world.drafter,
        sender=world.sender,
        model_name="chaos-fake",
        retry_attempts=2,
        fault=world.fault,
        activity=None,
    )


def install_store_faults(store: Any) -> None:
    """让业务落库按环境变量失败 n 次（模拟 SQLite 写入失败）。"""
    fails = ChaosWorld._env_int("AIEB_CHAOS_PERSIST_FAILS")
    if fails <= 0:
        return
    original = store.add_meeting
    state = {"left": fails}

    def guarded(*args: Any, **kwargs: Any):
        if state["left"] > 0:
            state["left"] -= 1
            raise RuntimeError("database is locked（故障注入）")
        return original(*args, **kwargs)

    store.add_meeting = guarded  # type: ignore[assignment]
