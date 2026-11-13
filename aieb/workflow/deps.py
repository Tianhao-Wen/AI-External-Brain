"""依赖注入层：把"外部世界"（LLM / SMTP / 业务库）从图结构里剥出来。

节点只依赖本文件里的四个可调用对象，因此：

* 生产：`RealDeps.build(...)` 接现有 `meetings` / `mailer` / `BrainStore`（**不重写业务逻辑**）；
* 测试：注入确定性 fake（不花钱、不发信、可注入故障），从而能真实测"恢复/幂等/重试"；
* 基准：同一套 fake 可以驱动"原生流程"和"LangGraph 流程"，保证对比公平。

会议原文不进入 Graph State，而是暂存在工作区 inbox 文件里（`stage_text` / `read_staged_text`），
分析完成后即删除 —— Checkpoint 里只留 `document_id` 与 `text_hash`。
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from aieb.brain import mailer, meetings
from aieb.brain.chunkkey import content_hash
from aieb.brain.store import BrainStore
from aieb.config import ProviderConfig
from aieb.workflow.persistence import WorkflowStore


# ---------------------------------------------------------------------------
# 节点返回的结果对象
# ---------------------------------------------------------------------------


@dataclass
class AnalysisOutcome:
    analysis: meetings.MeetingAnalysis
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = ""
    retries: int = 0


@dataclass
class DraftOutcome:
    subject: str = ""
    body: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = ""
    fallback: bool = False


@dataclass
class SendOutcome:
    ok: bool = False
    message: str = ""
    uncertain: bool = False
    error_code: str = ""
    detail: dict[str, Any] = field(default_factory=dict)


Analyzer = Callable[[str, str], Awaitable[AnalysisOutcome]]
Drafter = Callable[[dict[str, Any], str, str], Awaitable[DraftOutcome]]
Sender = Callable[[str, str, str], SendOutcome]


# ---------------------------------------------------------------------------
# 原文暂存（避免把大段原文写进 Checkpoint）
# ---------------------------------------------------------------------------


def inbox_dir(workspace: Path) -> Path:
    path = Path(workspace) / ".aieb" / "workflow" / "inbox"
    path.mkdir(parents=True, exist_ok=True)
    return path


def stage_text(workspace: Path, workflow_id: str, text: str) -> str:
    """把会议原文写到 inbox，返回 `document_id`（同时就是文件名）。"""
    doc_id = f"wftext_{content_hash(text)[:16]}_{workflow_id[-6:]}"
    path = inbox_dir(workspace) / f"{doc_id}.txt"
    path.write_text(text, encoding="utf-8")
    return doc_id


def read_staged_text(workspace: Path, document_id: str) -> str:
    path = inbox_dir(workspace) / f"{document_id}.txt"
    if not path.is_file():
        raise FileNotFoundError(f"暂存的会议原文不存在：{document_id}")
    return path.read_text(encoding="utf-8")


def drop_staged_text(workspace: Path, document_id: str) -> None:
    path = inbox_dir(workspace) / f"{document_id}.txt"
    try:
        path.unlink(missing_ok=True)
    except OSError:  # pragma: no cover - Windows 偶发占用
        pass


# ---------------------------------------------------------------------------
# 依赖容器
# ---------------------------------------------------------------------------


@dataclass
class WorkflowDeps:
    store: BrainStore
    wf: WorkflowStore
    workspace: Path
    analyzer: Analyzer
    drafter: Drafter
    sender: Sender
    model_name: str = "offline"
    #: 节点内显式重试次数（只用于纯读/纯 LLM 节点；发送与落库节点绝不自动重试）
    retry_attempts: int = 2
    #: **故障注入钩子**：每个节点开始时调用一次（参数为节点名）。
    #: 生产环境为 None；故障注入实验用它把"进程崩溃"精确地打在某个节点边界上
    #: （例如在 `persist_business_state` 之前 `os._exit()`），从而验证 Checkpoint 恢复。
    fault: Callable[[str], None] | None = None
    #: (kind, title, detail) → 写 activities；测试里可关闭
    activity: Callable[[str, str, str], None] | None = None

    # -- 原文 ----------------------------------------------------------
    def stage(self, workflow_id: str, text: str) -> str:
        return stage_text(self.workspace, workflow_id, text)

    def read_text(self, document_id: str) -> str:
        return read_staged_text(self.workspace, document_id)

    def drop_text(self, document_id: str) -> None:
        drop_staged_text(self.workspace, document_id)

    # -- 草稿 ----------------------------------------------------------
    def draft_hash(self, to_addr: str, subject: str, body: str) -> str:
        """草稿指纹：审批与发送都核对它，防止"审的是 A、发的是 B"。"""
        return content_hash(json.dumps([to_addr, subject, body], ensure_ascii=False))

    def get_email(self, email_id: str) -> dict[str, Any] | None:
        return self.store.get_email(email_id)

    def save_draft(
        self,
        email_id: str,
        *,
        to_addr: str,
        subject: str,
        body: str,
        bump_version: bool,
    ) -> dict[str, Any]:
        current = self.store.get_email(email_id) or {}
        version = int(current.get("version") or 1) + (1 if bump_version else 0)
        updated = self.store.update_email(
            email_id,
            to_addr=to_addr,
            subject=subject,
            body=body,
            version=version,
            draft_hash=self.draft_hash(to_addr, subject, body),
        )
        return updated or {}

    def record_activity(self, title: str, detail: str, kind: str = "workflow") -> None:
        if self.activity is None:
            return
        try:
            self.activity(kind, title, detail)
        except Exception:  # noqa: BLE001 - 审计失败不应影响主流程
            pass


# ---------------------------------------------------------------------------
# 生产实现
# ---------------------------------------------------------------------------


def build_real_deps(
    *,
    service: Any,
    wf_store: WorkflowStore,
    workspace: Path,
    provider: ProviderConfig | None = None,
    sender: Sender | None = None,
    use_langchain: bool | None = None,
    retry_attempts: int = 1,
) -> WorkflowDeps:
    """把现有 `BrainService` 包装成节点依赖。

    analyzer / drafter 默认走**原封不动**的 `meetings.analyze_async` / `meetings.draft_email`，
    只额外把 provider 回传的 token 用量带出来。

    `use_langchain=True`（或环境变量 `AIEB_WORKFLOW_LLM=langchain`）时改走 LangChain 适配层：
    同一个 prompt、同一个 provider、同样记录 token，用于验证“标准化接口可用”；
    默认不开启，避免多引入一条没必要的调用链。
    """
    import os

    prov = provider or service.provider
    if use_langchain is None:
        use_langchain = (os.environ.get("AIEB_WORKFLOW_LLM") or "").strip().lower() == "langchain"

    async def analyzer(filename: str, text: str) -> AnalysisOutcome:
        usage: dict[str, int] = {}
        if use_langchain:
            from aieb.workflow.langchain_adapter import build_chat_model, structured_output

            model = build_chat_model(prov)
            analysis = await structured_output(
                model,
                schema=meetings.MeetingAnalysis,
                system=meetings.ANALYSIS_SYSTEM,
                prompt=meetings.build_analysis_prompt(text, filename),
            )
            last = getattr(model, "last_usage", {}) or {}
            return AnalysisOutcome(
                analysis=analysis,  # type: ignore[arg-type]
                input_tokens=int(last.get("input_tokens", 0)),
                output_tokens=int(last.get("output_tokens", 0)),
                model=getattr(prov, "model", ""),
            )
        analysis = await meetings.analyze_async(
            prov, text, source_name=filename, usage_out=usage
        )
        return AnalysisOutcome(
            analysis=analysis,
            input_tokens=int(usage.get("input_tokens", 0)),
            output_tokens=int(usage.get("output_tokens", 0)),
            model=getattr(prov, "model", ""),
        )

    async def drafter(meeting: dict[str, Any], to_addr: str, extra: str) -> DraftOutcome:
        usage: dict[str, int] = {}
        draft = await meetings.draft_email(
            prov, meeting, to_addr=to_addr, extra_instruction=extra, usage_out=usage
        )
        return DraftOutcome(
            subject=str(draft.get("subject") or ""),
            body=str(draft.get("body") or ""),
            input_tokens=int(usage.get("input_tokens", 0)),
            output_tokens=int(usage.get("output_tokens", 0)),
            model=getattr(prov, "model", ""),
        )

    def default_sender(to_addr: str, subject: str, body: str) -> SendOutcome:
        result = mailer.send_email(to_addr=to_addr, subject=subject, body=body)
        ok = bool(result.get("ok"))
        return SendOutcome(
            ok=ok,
            message=str(result.get("message") or ""),
            uncertain=bool(result.get("uncertain")),
            error_code=str(result.get("error_code") or ""),
            detail={k: v for k, v in result.items() if k not in {"ok", "message"}},
        )

    return WorkflowDeps(
        store=service.store,
        wf=wf_store,
        workspace=Path(workspace),
        analyzer=analyzer,
        drafter=drafter,
        sender=sender or default_sender,
        model_name=getattr(prov, "model", "") or "offline",
        # 生产路径**不叠加**节点级重试：`meetings.analyze_async` 内部已经重试 2 次
        # （LLMError / ValidationError），再叠一层会让一次失败最多打 4 次模型调用。
        # 测试/基准用 fake 时保持默认 2，以便验证“重试最终成功/最终失败”两种路径。
        retry_attempts=max(1, int(retry_attempts)),
        activity=service.store.add_activity,
    )


def reset_inbox(workspace: Path) -> None:
    """清空暂存目录（测试/运维用；不影响业务库）。"""
    shutil.rmtree(inbox_dir(workspace), ignore_errors=True)
