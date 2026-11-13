"""Fake SMTP 接线测试：证明"发送成功"这条路是真的走通了，而不是被 mock 掉。

这些用例跑的是生产同一套 `mailer.send_email` + 同一个 `send_email` 节点，
只是把 SMTP 换成本地假服务器。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

pytest.importorskip("langgraph")

from aieb.brain import mailer  # noqa: E402
from tests.fake_smtp import FakeSMTPServer  # noqa: E402


@pytest.fixture()
def fake_smtp(monkeypatch):
    server = FakeSMTPServer()
    server.start()
    for key, value in server.env().items():
        monkeypatch.setenv(key, value)
    try:
        yield server
    finally:
        server.stop()


def test_mailer_sends_through_fake_smtp(fake_smtp: FakeSMTPServer) -> None:
    result = mailer.send_email(
        to_addr="alice@example.com, bob@example.com",
        subject="会议提醒",
        body="请关注风险与待办。",
    )
    assert result["ok"] is True, result
    assert len(fake_smtp.messages) == 1
    message = fake_smtp.messages[0]
    assert message.subject == "会议提醒"
    assert message.rcpt_to == ["alice@example.com", "bob@example.com"]
    assert "风险与待办" in message.body


def test_dry_run_blocks_without_faking_success(monkeypatch, fake_smtp: FakeSMTPServer) -> None:
    monkeypatch.setenv("AIEB_EMAIL_DRY_RUN", "1")
    result = mailer.send_email(to_addr="a@example.com", subject="x", body="y")
    assert result["ok"] is False
    assert result["error_code"] == "send_blocked"
    assert fake_smtp.messages == [], "保护模式绝不允许真的投递"


def test_workflow_send_uses_real_smtp_path(fake_smtp: FakeSMTPServer, tmp_path: Path) -> None:
    """工作流的 send_email 节点走真实 smtplib → 假服务器收到 1 封，邮件状态变 sent。"""
    from tests.workflow_harness import Fakes, FakeWorld, MEETING_TEXT
    from aieb.brain.store import BrainStore
    from aieb.workflow.deps import build_real_deps
    from aieb.workflow.persistence import WorkflowStore
    from aieb.workflow.runner import WorkflowRunner
    from aieb.workflow.state import ResumeCommand

    store = BrainStore(tmp_path)
    wf = WorkflowStore(tmp_path / ".aieb" / "workflow" / "workflow.sqlite3")
    fakes = Fakes()
    world = FakeWorld(fakes)

    class _Service:
        def __init__(self) -> None:
            self.store = store
            self.provider = None

    deps = build_real_deps(service=_Service(), wf_store=wf, workspace=tmp_path)
    # 只替换分析/草稿（不花钱），**发送保留真实实现**（走 SMTP 到假服务器）
    deps.analyzer = world.analyzer
    deps.drafter = world.drafter
    runner = WorkflowRunner(deps=deps, workspace=tmp_path)
    try:
        snapshot = runner.start_sync(
            name="周会.txt", text=MEETING_TEXT, to_addr="team@example.com"
        )
        assert snapshot.approval is not None
        done = runner.resume_sync(
            snapshot.workflow_id,
            ResumeCommand(
                decision="approve",
                version=snapshot.approval.version,
                draft_hash=snapshot.approval.draft_hash,
            ),
        )
        assert done.execution_status == "completed"
        assert len(fake_smtp.messages) == 1
        assert fake_smtp.messages[0].rcpt_to == ["team@example.com"]
        assert store.get_email(done.email_draft_id)["status"] == "sent"
    finally:
        runner.close()
        store.close()
        wf.close()
