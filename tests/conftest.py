"""pytest 全局隔离：**任何测试都不允许真的把邮件发出去**。

背景
----
项目根目录的 `.env` 会被服务启动时自动载入，开发者本机也常常导出了真实的
`AIEB_SMTP_*` 凭证。早先 `scripts/smoke_test.py` 与 `scripts/browser_e2e.py`
在这种环境下会真的投递邮件，属于"自动化测试误发真实邮件"的缺陷。

策略（两层，互不依赖）
----------------------
1. 摘掉所有 SMTP 凭证环境变量，并把 ``AIEB_EMAIL_DRY_RUN=1`` 打开 ——
   即使某个测试又设回凭证，`mailer.send_email` 也会被 dry-run 拦截；
2. 需要真实投递的验证**不放在 pytest 里**，而是显式跑
   ``scripts/verify_email.py``（必须手动给 ``--to``）。

真的要在测试进程里发信（不建议）：设置 ``AIEB_TEST_ALLOW_REAL_EMAIL=1``。
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

DRY_RUN_ENV = "AIEB_EMAIL_DRY_RUN"
OPT_IN_ENV = "AIEB_TEST_ALLOW_REAL_EMAIL"
SMTP_PREFIXES = ("AIEB_SMTP_", "WENDREAM_SMTP_")
_TRUTHY = {"1", "true", "yes", "on"}


def _truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in _TRUTHY


@pytest.fixture(scope="session", autouse=True)
def _block_real_email_sending() -> Iterator[None]:
    """整个测试会话期间禁止真实发信。"""
    if _truthy(OPT_IN_ENV):
        yield
        return

    saved: dict[str, str] = {}
    for key in [k for k in os.environ if k.startswith(SMTP_PREFIXES)]:
        saved[key] = os.environ.pop(key)
    previous_dry_run = os.environ.get(DRY_RUN_ENV)
    os.environ[DRY_RUN_ENV] = "1"
    try:
        yield
    finally:
        os.environ.pop(DRY_RUN_ENV, None)
        if previous_dry_run is not None:
            os.environ[DRY_RUN_ENV] = previous_dry_run
        os.environ.update(saved)


# 工作流测试夹具（真实 SQLite + Fake LLM/SMTP）在 tests/workflow_harness.py 里定义，
# 这里转成 pytest fixture，供 test_workflow_*.py 共用。
#
# LangGraph 是**可选依赖**：未安装时必须让基础环境仍能完整收集并运行其余测试，
# 而不是在 conftest 阶段就 ImportError。
try:  # pragma: no cover - 取决于环境是否装了 langgraph
    from tests.workflow_harness import make_harness  # noqa: E402
except ImportError as exc:  # pragma: no cover
    _WORKFLOW_IMPORT_ERROR = exc
else:
    _WORKFLOW_IMPORT_ERROR = None


@pytest.fixture()
def harness(tmp_path):
    if _WORKFLOW_IMPORT_ERROR is not None:
        pytest.skip(f"未安装 LangGraph 可选依赖：{_WORKFLOW_IMPORT_ERROR}")
    gen = make_harness(tmp_path)
    instance = next(gen)
    try:
        yield instance
    finally:
        next(gen, None)
