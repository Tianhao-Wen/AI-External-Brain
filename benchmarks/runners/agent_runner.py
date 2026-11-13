"""Agent 端到端套件运行器。

与 RAG / 会议套件的关键区别：这里评的是**端到端任务成功率**，
也就是"用户用一句自然语言提需求，系统能不能把事办成"，而不只是某个函数算得对不对。

隔离与安全
----------
* 每个任务一个独立 workspace（独立 SQLite / Session / Memory / 上传 / 汇报材料目录）；
* 邮件传输边界 ``aieb.brain.mailer.send_email`` 被替换为 Fake SMTP 记录器，
  就算代码走到真实发送分支也只会被记录下来，不会联外网；
* 联网搜索被替换为固定 fixture 工具，保证结果可复现，并明确标注为 **fixture**，
  不代表真实网络搜索质量；
* 所有模型调用都经过 ``common.patch_llm_counter``，受 ``--max-calls`` 硬约束。
"""

from __future__ import annotations

import asyncio
import json
import shutil
import time
from pathlib import Path
from typing import Any

from benchmarks import common
from benchmarks.datasets.agent import tasks as task_set
from benchmarks.evaluators.metrics import mean, percent

MEET_SRC = common.DATASETS_DIR / "meetings" / "meetings"

# 固定的假搜索结果：让"会不会调用搜索、会不会用搜索结果"可以稳定复现。
WEB_FIXTURES: dict[str, str] = {
    "lpr": "2026年10月20日贷款市场报价利率(LPR)：1年期 LPR 为 3.00%，5年期以上 LPR 为 3.50%。",
    "上证": "上证指数 2026-10-20 收盘 3286.45 点，涨 0.62%，成交额 4120 亿元。",
}
WEB_DEFAULT = "未找到与该查询相关的搜索结果（0 条）。"


# ---------------------------------------------------------------------------
# Fake SMTP
# ---------------------------------------------------------------------------


class FakeSMTP:
    """替换 ``mailer.send_email`` 的传输层，只记录不投递。"""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self._original = None

    def __enter__(self) -> "FakeSMTP":
        from aieb.brain import mailer

        self._original = mailer.send_email

        def fake_send(*, to_addr: str, subject: str, body: str, cfg: Any = None) -> dict[str, Any]:
            self.sent.append(
                {
                    "to_addr": common.redact(to_addr),
                    "subject": common.redact(subject),
                    "body_chars": len(body or ""),
                }
            )
            return {
                "ok": True,
                "message": "已投递到 Fake SMTP（评测环境，未真实发送）",
                "sent_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            }

        mailer.send_email = fake_send  # type: ignore[assignment]
        return self

    def __exit__(self, *exc: Any) -> None:
        from aieb.brain import mailer

        if self._original is not None:
            mailer.send_email = self._original  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Fake WebSearch
# ---------------------------------------------------------------------------


def _register_fake_web_search(registry) -> None:
    from pydantic import BaseModel, Field

    from aieb.tools.base import Tool, ToolResult

    class FakeWebSearchParams(BaseModel):
        query: str = Field(description="搜索关键词")

    class FakeWebSearch(Tool):
        name = "WebSearch"
        description = "搜索互联网获取最新信息（评测环境使用固定数据集）。"
        params_model = FakeWebSearchParams
        category = "read"

        async def execute(self, params: FakeWebSearchParams) -> ToolResult:
            text = WEB_DEFAULT
            for key, value in WEB_FIXTURES.items():
                if key.lower() in (params.query or "").lower():
                    text = value
                    break
            return ToolResult(output=text)

    registry.register(FakeWebSearch())


# ---------------------------------------------------------------------------
# 工具调用记录
# ---------------------------------------------------------------------------


class _RecordingTool:
    """包装工具实例，记录每次调用是否成功——用于区分「调用成功率」与「任务成功率」。"""

    def __init__(self, inner: Any, sink: list[dict[str, Any]]) -> None:
        self._inner = inner
        self._sink = sink

    def __getattr__(self, item: str) -> Any:
        return getattr(self._inner, item)

    async def execute(self, params: Any):
        started = time.perf_counter()
        try:
            args = params.model_dump() if hasattr(params, "model_dump") else {}
        except Exception:  # noqa: BLE001
            args = {}
        try:
            result = await self._inner.execute(params)
        except Exception as exc:  # noqa: BLE001 - 工具异常也要计入成功率分母
            self._sink.append(
                {
                    "tool": self._inner.name,
                    "args": args,
                    "ok": False,
                    "error": f"{type(exc).__name__}: {exc}",
                    "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
                }
            )
            raise
        self._sink.append(
            {
                "tool": self._inner.name,
                "args": args,
                "ok": not getattr(result, "is_error", False),
                "error": "" if not getattr(result, "is_error", False) else str(result.output)[:200],
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
            }
        )
        return result


def _instrument(registry, sink: list[dict[str, Any]]) -> None:
    for name, tool in list(registry._tools.items()):  # noqa: SLF001 - 评测需要拿到实例
        registry._tools[name] = _RecordingTool(tool, sink)  # noqa: SLF001


class _CountingClient:
    """把 Agent 主循环的模型调用也纳入 ``CallBudget``。

    ``common.patch_llm_counter`` 只能拦截 ``aieb.brain.llm.complete``（业务层），
    Agent 走的是 ``LLMClient.stream``，必须单独拦截，否则 ``--max-calls`` 形同虚设。
    """

    def __init__(self, inner: Any, budget: common.CallBudget) -> None:
        self._inner = inner
        self._budget = budget

    def __getattr__(self, item: str) -> Any:
        return getattr(self._inner, item)

    async def stream(self, *args: Any, **kwargs: Any):
        self._budget.consume("agent.turn")
        try:
            async for event in self._inner.stream(*args, **kwargs):
                yield event
        except Exception:
            self._budget.failures += 1
            raise


# ---------------------------------------------------------------------------
# 环境准备
# ---------------------------------------------------------------------------


SEED_MEETINGS: list[dict[str, Any]] = [
    {
        "title": "星海项目周例会",
        "meeting_time": "2026年3月5日 14:00-15:30",
        "location": "3号会议室",
        "participants": ["张伟", "李娜", "王强", "赵敏", "陈晨"],
        "summary": "确认联调滞后与人力缺口两项风险，上线节点维持 4月10日。",
        "decisions": ["上线节点维持 4月10日不变", "由陈晨出面与支付网关方对接争取沙箱环境"],
        "risks": [
            {
                "title": "支付网关接口联调滞后",
                "level": "high",
                "description": "接口联调比计划落后两周，支付网关方尚未提供沙箱环境",
                "evidence": "订单服务与支付网关的接口联调比计划落后两周",
                "suggestion": "本周内启动联调",
            }
        ],
        "todos": [
            {
                "content": "与支付网关方确认沙箱环境",
                "owner": "陈晨",
                "due_date": "2026年3月7日",
                "priority": "high",
            }
        ],
    },
    {
        "title": "供应商年度评估沟通会",
        "meeting_time": "2026年3月19日 10:00-11:00",
        "location": "采购部会议室",
        "participants": ["周凯", "何斌", "赵敏"],
        "summary": "恒安电子因来料合格率不达标被降为 C 级。",
        "decisions": ["恒安电子本年度评级降为 C 级，列入重点观察名单"],
        "risks": [
            {
                "title": "来料合格率低于合同门槛",
                "level": "medium",
                "description": "来料合格率 96.2%，低于合同约定的 98% 门槛",
                "evidence": "来料合格率 96.2%，低于合同约定的 98% 门槛",
                "suggestion": "加严抽检",
            }
        ],
        "todos": [
            {
                "content": "更新抽检作业指导书",
                "owner": "赵敏",
                "due_date": "2026年3月26日",
                "priority": "medium",
            }
        ],
    },
    {
        # 未暴露测试任务 UA3 用的历史会议（来自 unseen 语料 MEET-U05）
        "title": "海川物流 TMS 接口对接会",
        "meeting_time": "2026年5月15日 09:00-10:10",
        "location": "3号会议室",
        "participants": ["马超", "彭博", "蒋伟"],
        "summary": "客户接口文档两个月未更新，字段定义反复返工。",
        "decisions": ["字段结论以邮件固化，双方确认后冻结"],
        "risks": [
            {
                "title": "客户接口文档与实际返回不一致",
                "level": "medium",
                "description": "客户方接口文档已有两个月未更新，最新一版与线上实际返回不一致",
                "evidence": "客户方的接口文档已有两个月未更新，最新一版与线上实际返回不一致",
                "suggestion": "推动客户更新接口文档",
            }
        ],
        "todos": [
            {
                "content": "整理 3 个待确认字段清单",
                "owner": "马超",
                "due_date": "2026年5月18日",
                "priority": "high",
            }
        ],
    },
]


def _seed_meetings(service) -> None:
    """用产品自身的落库函数写入固定会议，跳过 LLM 抽取。

    这里只准备"历史数据"，被测能力是"能不能回忆起来"，不是"能不能分析"。
    """
    from aieb.brain import meetings as meetings_mod
    from aieb.brain.meetings import MeetingAnalysis, RiskIn, TodoIn

    for item in SEED_MEETINGS:
        raw = (
            f"会议主题：{item['title']}\n时间：{item['meeting_time']}\n"
            f"参会人：{'、'.join(item['participants'])}\n"
            f"{item['summary']}\n"
            + "\n".join(item["decisions"])
        )
        analysis = MeetingAnalysis(
            title=item["title"],
            meeting_time=item["meeting_time"],
            location=item["location"],
            participants=item["participants"],
            discussion=[],
            decisions=item["decisions"],
            summary=item["summary"],
            risks=[RiskIn(**r) for r in item["risks"]],
            todos=[TodoIn(**t) for t in item["todos"]],
        )
        meetings_mod.save(service.store, analysis, raw, f"{item['title']}.txt")


def _prepare_workspace(workspace: Path) -> None:
    meeting_dir = workspace / "会议记录"
    meeting_dir.mkdir(parents=True, exist_ok=True)
    # 开发集与未暴露集的会议文件都放进去：未暴露任务引用 MEET-U0x 也能找到
    for src in sorted(MEET_SRC.glob("MEET-*.txt")):
        shutil.copy2(src, meeting_dir / src.name)
    unseen_dir = common.DATASETS_DIR / "meetings" / "unseen" / "meetings"
    if unseen_dir.is_dir():
        for src in sorted(unseen_dir.glob("MEET-*.txt")):
            shutil.copy2(src, meeting_dir / src.name)
    (meeting_dir / "数据导出.csv").write_text(
        "meeting_id,risk\nMEET-001,接口联调滞后\n", encoding="utf-8"
    )


def _index_documents(service) -> None:
    """开发集 4 份文档 + 未暴露集 2 份文档全部入库。"""
    from benchmarks.datasets.rag.build_documents import DOCUMENTS

    sources = [(DOCUMENTS, common.DATASETS_DIR / "rag" / "documents")]
    unseen = common.DATASETS_DIR / "rag" / "unseen"
    if (unseen / "questions.jsonl").is_file():
        from benchmarks.datasets.rag.unseen.build_unseen import DOCS as UNSEEN_DOCS

        sources.append((UNSEEN_DOCS, unseen / "documents"))
    for specs, doc_dir in sources:
        for spec in specs.values():
            path = doc_dir / spec["filename"]
            if path.is_file():
                service.upload(path.name, path.read_bytes())


# ---------------------------------------------------------------------------
# 断言
# ---------------------------------------------------------------------------


def _latest_pptx(workspace: Path) -> Path | None:
    files = sorted(
        (workspace / "汇报材料").glob("*.pptx"), key=lambda p: p.stat().st_mtime, reverse=True
    )
    return files[0] if files else None


def _check_pptx(path: Path, min_slides: int, max_slides: int) -> tuple[bool, str]:
    try:
        from pptx import Presentation
    except ImportError:
        return False, "缺少 python-pptx"
    try:
        prs = Presentation(str(path))
    except Exception as exc:  # noqa: BLE001
        return False, f"打不开 {path.name}: {exc}"
    count = len(prs.slides)
    if count < min_slides or count > max_slides:
        return False, f"页数 {count} 不在 [{min_slides}, {max_slides}]"
    empty = 0
    for slide in prs.slides:
        texts = [
            shape.text_frame.text.strip()
            for shape in slide.shapes
            if shape.has_text_frame and shape.text_frame.text.strip()
        ]
        if not texts:
            empty += 1
    if empty > max(1, count // 3):
        return False, f"{empty} 页没有任何文字内容"
    return True, f"{count} 页，全部可读"


def evaluate_checks(
    task: dict[str, Any],
    *,
    answer: str,
    tool_calls: list[dict[str, Any]],
    llm_calls: int,
    store,
    fake_smtp: FakeSMTP,
    workspace: Path,
    meetings_before: int,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    called = [c["tool"] for c in tool_calls]
    body = answer or ""

    for check in task.get("checks") or []:
        kind = check["kind"]
        ok = True
        detail = ""
        if kind == "tool_called_any":
            hit = [n for n in check["names"] if n in called]
            ok = bool(hit)
            detail = f"已调用：{hit or '无'}（实际：{sorted(set(called)) or '无'}）"
        elif kind == "tool_called_all":
            missing = [n for n in check["names"] if n not in called]
            ok = not missing
            detail = f"缺少：{missing}"
        elif kind == "tool_not_called":
            hit = [n for n in check["names"] if n in called]
            ok = not hit
            detail = f"不应调用但调用了：{hit}"
        elif kind == "answer_contains":
            missing = [v for v in check["values"] if v not in body]
            ok = not missing
            detail = f"缺少关键词：{missing}"
        elif kind == "answer_contains_any":
            hit = [v for v in check["values"] if v in body]
            ok = bool(hit)
            detail = f"命中：{hit or '无'}（要求命中任意一个）"
        elif kind == "answer_not_contains":
            hit = [v for v in check["values"] if v in body]
            ok = not hit
            detail = f"出现了不该有的内容：{hit}"
        elif kind == "no_tool_error":
            bad = [c["tool"] for c in tool_calls if not c["ok"]]
            ok = not bad
            detail = f"报错工具：{bad}"
        elif kind == "meeting_created":
            total = len(store.list_meetings(limit=200))
            created = total - meetings_before
            meetings = store.list_meetings(limit=200)
            risks = meetings[0].get("risk_count", 0) if meetings else 0
            if check.get("expect_none"):
                ok = created <= 0
                detail = f"新增会议 {created} 场（期望 0）"
            else:
                ok = created >= 1 and risks >= check.get("min_risks", 0)
                if "max_risks" in check:
                    ok = ok and risks <= check["max_risks"]
                detail = f"新增会议 {created} 场，最新会议风险 {risks} 条"
        elif kind == "todos_at_least":
            count = len(store.list_todos())
            ok = count >= check["count"]
            detail = f"待办 {count} 条（要求 ≥{check['count']}）"
        elif kind == "email_draft_exists":
            # 草稿一经确认发送状态就会变成 sent，所以这里统计“本次运行产生的会议提醒邮件”，
            # 而不是只数 status==draft，否则「草稿→确认→发送」的任务永远判失败。
            emails = store.list_emails()
            ok = len(emails) >= check.get("min", 1)
            detail = f"会议提醒邮件 {len(emails)} 封（状态 {[e['status'] for e in emails]}）"
        elif kind == "email_not_sent":
            ok = len(fake_smtp.sent) == 0
            detail = f"Fake SMTP 收到 {len(fake_smtp.sent)} 封（期望 0）"
        elif kind == "email_sent_count":
            ok = len(fake_smtp.sent) == check["count"]
            detail = f"Fake SMTP 收到 {len(fake_smtp.sent)} 封（期望 {check['count']}）"
        elif kind == "pptx_valid":
            path = _latest_pptx(workspace)
            if path is None:
                ok = False
                detail = "没有生成 .pptx"
            else:
                ok, detail = _check_pptx(path, check["min_slides"], check["max_slides"])
        elif kind == "max_llm_calls":
            ok = llm_calls <= check["value"]
            detail = f"LLM 调用 {llm_calls} 次（上限 {check['value']}）"
        else:
            ok = False
            detail = f"未知断言类型 {kind}"
        results.append({"kind": kind, "ok": ok, "detail": detail})
    return results


# ---------------------------------------------------------------------------
# 单次执行
# ---------------------------------------------------------------------------


async def _run_once(ctx, task: dict[str, Any], workspace: Path) -> dict[str, Any]:
    from aieb.agent import Agent
    from aieb.brain.service import BrainService
    from aieb.brain.tools import register_brain_tools
    from aieb.client import create_client
    from aieb.conversation import ConversationManager
    from aieb.permissions import (
        DangerousCommandDetector,
        PathSandbox,
        PermissionChecker,
        PermissionMode,
        RuleEngine,
    )
    from aieb.tools import create_default_registry

    work_dir = str(workspace)
    _prepare_workspace(workspace)

    service = BrainService(work_dir=work_dir, provider=ctx.provider)
    tool_calls: list[dict[str, Any]] = []
    fake_smtp = FakeSMTP()
    started = time.perf_counter()
    answer = ""
    error = ""
    llm_before = ctx.budget.used
    timed_out = False
    meetings_before = 0

    try:
        _index_documents(service)
        if task["type"].startswith("memory_recall"):
            _seed_meetings(service)
        meetings_before = len(service.store.list_meetings(limit=200))

        checker = PermissionChecker(
            detector=DangerousCommandDetector(),
            sandbox=PathSandbox(work_dir),
            rule_engine=RuleEngine(
                user_rules_path=workspace.parent / "no-such-user-rules.yaml",
                project_rules_path=workspace / ".aieb" / "permissions.yaml",
                local_rules_path=workspace / ".aieb" / "permissions.local.yaml",
            ),
            mode=PermissionMode.BYPASS,
        )
        registry = create_default_registry(work_dir=work_dir, restrict_bash_to_workspace=True)
        register_brain_tools(registry, service, work_dir=work_dir)
        if task["type"].startswith("web_search"):
            _register_fake_web_search(registry)
        _instrument(registry, tool_calls)

        agent = Agent(
            client=_CountingClient(create_client(ctx.provider), ctx.budget),
            registry=registry,
            protocol=ctx.provider.protocol,
            work_dir=work_dir,
            max_iterations=int(task.get("max_iterations") or 10),
            permission_checker=checker,
            context_window=ctx.provider.get_context_window(),
        )
        agent.noninteractive = True
        # 与生产端（remote._init_agent）保持一致：把知识库状态注入环境上下文。
        # 少了这一步，评测出来的“知识库不调用”其实是环境与生产不一致造成的假象。
        agent.set_env_context_extra(service.knowledge_context())
        from aieb.brain import routing as brain_routing

        agent.set_turn_hint_provider(
            lambda question: brain_routing.hint_for(
                question, has_documents=service.has_documents()
            )
        )

        conversation = ConversationManager()
        with fake_smtp:
            for index, turn in enumerate(task["turns"]):
                text = await asyncio.wait_for(
                    agent.run_to_completion(turn, conversation=conversation, event_callback=None),
                    timeout=float(task.get("timeout_s") or 240),
                )
                answer = f"{answer}\n{text}".strip() if answer else text
                print(f"      轮次 {index + 1} 完成，输出 {len(text or '')} 字", flush=True)

            if task.get("confirm_send"):
                drafts = [e for e in service.store.list_emails() if e["status"] == "draft"]
                if drafts:
                    service.send_email(drafts[-1]["id"])
    except asyncio.TimeoutError:
        timed_out = True
        error = "timeout"
    except common.BudgetExceeded as exc:
        error = f"budget: {exc}"
    except Exception as exc:  # noqa: BLE001 - 失败任务必须留在结果里
        error = f"{type(exc).__name__}: {exc}"
    finally:
        latency_ms = (time.perf_counter() - started) * 1000
        checks = evaluate_checks(
            task,
            answer=answer,
            tool_calls=tool_calls,
            llm_calls=ctx.budget.used - llm_before,
            store=service.store,
            fake_smtp=fake_smtp,
            workspace=workspace,
            meetings_before=meetings_before,
        )
        service.store.close()

    success = (not error) and all(c["ok"] for c in checks)
    return {
        "success": success,
        "checks": checks,
        "answer": common.redact(answer)[:4000],
        "tool_calls": [
            {
                "tool": c["tool"],
                "ok": c["ok"],
                "args": c.get("args") or {},
                "error": common.redact(c["error"])[:120],
            }
            for c in tool_calls
        ],
        "tool_call_count": len(tool_calls),
        "tool_calls_ok": sum(1 for c in tool_calls if c["ok"]),
        "llm_calls": ctx.budget.used - llm_before,
        "latency_ms": round(latency_ms, 1),
        "error": error,
        "timed_out": timed_out,
        "emails_captured": len(fake_smtp.sent),
    }


# ---------------------------------------------------------------------------
# 套件入口
# ---------------------------------------------------------------------------


async def run(
    ctx, *, repeat: int = 1, only: list[str] | None = None, limit: int = 0, split: str = "all"
) -> dict[str, Any]:
    tasks = task_set.select(only=only, limit=limit, split=split)
    if not tasks:
        raise RuntimeError("没有匹配的任务，请检查 --only / --limit / --split")
    if ctx.mode != "live":
        # fixture 模式下没有可用模型，跑出来的只会是一堆连接失败记录，
        # 既浪费预算又毫无信息量。直接拒绝，避免有人把 fixture 的 0% 当结论引用。
        raise RuntimeError(
            "agent 套件必须使用 --mode live：端到端任务的成功率只能由真实模型调用得出，"
            "fixture 模式没有模型可用。"
        )

    cases: list[dict[str, Any]] = []
    total = len(tasks)
    for index, task in enumerate(tasks, start=1):
        rounds = repeat if (task.get("repeat") and repeat > 1) else 1
        for round_index in range(1, rounds + 1):
            label = f"{task['task_id']}#{round_index}" if rounds > 1 else task["task_id"]
            print(f"  [{index}/{total}] {label} {task['type']:<22} …", flush=True)
            workspace = ctx.workspace(f"agent_{task['task_id']}_{round_index}")
            result = await _run_once(ctx, task, workspace)
            cases.append(
                {
                    "task_id": task["task_id"],
                    "round": round_index,
                    "label": label,
                    "type": task["type"],
                    "title": task["title"],
                    **result,
                }
            )
            failed = [c for c in result["checks"] if not c["ok"]]
            flag = "PASS" if result["success"] else "FAIL"
            print(
                f"      => {flag}  tools={result['tool_call_count']} "
                f"llm={result['llm_calls']} {result['latency_ms']:.0f}ms"
                + (f"  未通过：{[c['kind'] for c in failed]}" if failed else "")
                + (f"  错误：{result['error'][:60]}" if result["error"] else ""),
                flush=True,
            )

    aggregates = {
        "task_success_rate": mean(1.0 if c["success"] else 0.0 for c in cases),
        "task_total": len(cases),
        "task_passed": sum(1 for c in cases if c["success"]),
        "tool_call_success_rate": _tool_rate(cases),
        "tool_call_total": sum(c["tool_call_count"] for c in cases),
        "tool_success_total": sum(c["tool_calls_ok"] for c in cases),
        "avg_tool_calls": mean(c["tool_call_count"] for c in cases),
        "avg_llm_calls": mean(c["llm_calls"] for c in cases),
        "latency_p50_ms": common.percentile([c["latency_ms"] for c in cases], 50),
        "latency_p95_ms": common.percentile([c["latency_ms"] for c in cases], 95),
        "timeout_rate": mean(1.0 if c["timed_out"] else 0.0 for c in cases),
        "error_rate": mean(1.0 if c["error"] else 0.0 for c in cases),
        "duplicate_tool_calls": _duplicate_calls(cases),
        "pptx_artifact_duplicates": _artifact_duplicates(cases),
        "by_type": _by_type(cases),
        "repeat_consistency": _repeat_consistency(cases),
    }
    return {"cases": cases, "aggregates": aggregates, "mode": ctx.mode}


def _duplicate_calls(cases: list[dict[str, Any]]) -> dict[str, Any]:
    """统计"同一任务内用完全相同的参数重复调用同一工具"的次数。

    这是产品可靠性的一个硬指标：重复调用会带来重复副作用（PPT 多份文件、邮件多封）。
    """
    total = 0
    dup = 0
    for case in cases:
        seen: set[tuple[str, str]] = set()
        for call in case.get("tool_calls") or []:
            key = (call.get("tool") or "", json.dumps(call.get("args") or {}, sort_keys=True,
                                                      ensure_ascii=False))
            total += 1
            if key in seen:
                dup += 1
            else:
                seen.add(key)
    return {
        "duplicate": dup,
        "total": total,
        "rate": round(dup / total, 4) if total else 0.0,
    }


def _artifact_duplicates(cases: list[dict[str, Any]]) -> dict[str, Any]:
    """PPT 任务是否产出了多份文件（同一任务内 CreatePresentation 被调用多次）。"""
    pptx_cases = [c for c in cases if c["type"] == "pptx"]
    duplicated = 0
    for case in pptx_cases:
        calls = [t for t in (case.get("tool_calls") or []) if t.get("tool") == "CreatePresentation"]
        if len(calls) > 1:
            duplicated += 1
    return {
        "pptx_runs": len(pptx_cases),
        "runs_with_multiple_artifacts": duplicated,
    }


def _tool_rate(cases: list[dict[str, Any]]) -> float:
    total = sum(c["tool_call_count"] for c in cases)
    if not total:
        return 0.0
    return sum(c["tool_calls_ok"] for c in cases) / total


def _by_type(cases: list[dict[str, Any]]) -> dict[str, Any]:
    buckets: dict[str, list[dict[str, Any]]] = {}
    for case in cases:
        buckets.setdefault(case["type"], []).append(case)
    out: dict[str, Any] = {}
    for name, items in sorted(buckets.items()):
        out[name] = {
            "tasks": len(items),
            "passed": sum(1 for c in items if c["success"]),
            "success_rate": mean(1.0 if c["success"] else 0.0 for c in items),
            "avg_llm_calls": mean(c["llm_calls"] for c in items),
            "avg_latency_ms": mean(c["latency_ms"] for c in items),
        }
    return out


def _repeat_consistency(cases: list[dict[str, Any]]) -> dict[str, Any]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for case in cases:
        grouped.setdefault(case["task_id"], []).append(case)
    repeated = {k: v for k, v in grouped.items() if len(v) > 1}
    if not repeated:
        return {"repeated_tasks": 0}
    return {
        "repeated_tasks": len(repeated),
        "runs": sum(len(v) for v in repeated.values()),
        "all_pass_rate": mean(
            1.0 if all(c["success"] for c in v) else 0.0 for v in repeated.values()
        ),
        "flaky_tasks": [k for k, v in repeated.items() if any(c["success"] for c in v) and not all(c["success"] for c in v)],
        "per_task": {
            k: {
                "runs": len(v),
                "pass": sum(1 for c in v if c["success"]),
                "avg_latency_ms": mean(c["latency_ms"] for c in v),
                "avg_llm_calls": mean(c["llm_calls"] for c in v),
            }
            for k, v in sorted(repeated.items())
        },
    }


# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------

CSV_COLUMNS = [
    "label",
    "task_id",
    "round",
    "type",
    "title",
    "success",
    "tool_call_count",
    "tool_calls_ok",
    "llm_calls",
    "latency_ms",
    "timed_out",
    "error",
    "failed_checks",
    "check_detail",
    "answer_preview",
]


def to_csv_rows(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for case in cases:
        failed = [c for c in case["checks"] if not c["ok"]]
        rows.append(
            {
                "label": case["label"],
                "task_id": case["task_id"],
                "round": case["round"],
                "type": case["type"],
                "title": case["title"],
                "success": case["success"],
                "tool_call_count": case["tool_call_count"],
                "tool_calls_ok": case["tool_calls_ok"],
                "llm_calls": case["llm_calls"],
                "latency_ms": case["latency_ms"],
                "timed_out": case["timed_out"],
                "error": case["error"],
                "failed_checks": [c["kind"] for c in failed],
                "check_detail": [f"{c['kind']}: {c['detail']}" for c in failed],
                "answer_preview": (case["answer"] or "")[:400].replace("\n", " "),
            }
        )
    return rows


def summary_markdown(result: dict[str, Any], ctx) -> str:
    agg = result["aggregates"]
    lines = [
        "# Agent 端到端评测结果摘要",
        "",
        f"- 运行 ID：`{ctx.run_id}`",
        f"- 模式：`{ctx.mode}`",
        f"- 模型：`{ctx.model.model}`",
        f"- Git commit：`{ctx.git.get('commit_short')}`",
        f"- 任务执行次数：{agg['task_total']}（通过 {agg['task_passed']}）",
        "",
        "## 核心指标",
        "",
        f"- **任务成功率（Task Success Rate）：{percent(agg['task_success_rate'])}**"
        "（所有断言全部通过才算成功）",
        f"- **工具调用成功率（Tool Call Success Rate）：{percent(agg['tool_call_success_rate'])}**"
        f"（{agg['tool_success_total']} / {agg['tool_call_total']} 次调用无异常）",
        f"- 平均工具调用次数：{agg['avg_tool_calls']:.2f}",
        f"- 平均模型调用次数：{agg['avg_llm_calls']:.2f}",
        f"- 延迟 P50 {agg['latency_p50_ms']:.0f} ms / P95 {agg['latency_p95_ms']:.0f} ms",
        f"- 超时率：{percent(agg['timeout_rate'])}　异常率：{percent(agg['error_rate'])}",
        "",
        "> 两个成功率不是一回事：工具调用成功只说明「工具没报错」，"
        "任务成功才说明「用户想办的事真的办成了」。把工具成功率当任务成功率是最常见的注水方式。",
        "",
        "## 按任务类型",
        "",
        "| 类型 | 执行数 | 通过 | 成功率 | 平均模型调用 | 平均耗时(ms) |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for name, item in agg["by_type"].items():
        lines.append(
            f"| {name} | {item['tasks']} | {item['passed']} | {percent(item['success_rate'])} | "
            f"{item['avg_llm_calls']:.2f} | {item['avg_latency_ms']:.0f} |"
        )

    rep = agg["repeat_consistency"]
    lines += ["", "## 重复运行一致性（全部轮次均已记录，未做挑选）", ""]
    if not rep.get("repeated_tasks"):
        lines.append("本次未启用重复运行（`--repeat`）。")
    else:
        lines.append(
            f"- 重复任务 {rep['repeated_tasks']} 个，共 {rep['runs']} 次执行；"
            f"全部轮次都通过的比例：{percent(rep['all_pass_rate'])}"
        )
        if rep["flaky_tasks"]:
            lines.append(f"- 不稳定任务（有的轮次过、有的不过）：{'、'.join(rep['flaky_tasks'])}")
        else:
            lines.append("- 不稳定任务：无")
        lines += ["", "| 任务 | 轮次 | 通过 | 平均耗时(ms) | 平均模型调用 |", "| --- | --- | --- | --- | --- |"]
        for name, item in rep["per_task"].items():
            lines.append(
                f"| {name} | {item['runs']} | {item['pass']} | {item['avg_latency_ms']:.0f} | "
                f"{item['avg_llm_calls']:.2f} |"
            )

    lines += ["", "## 逐条结果", "", "| 任务 | 类型 | 结果 | 工具调用 | 模型调用 | 耗时(ms) | 说明 |",
              "| --- | --- | --- | --- | --- | --- | --- |"]
    for case in result["cases"]:
        failed = [c for c in case["checks"] if not c["ok"]]
        note = case["error"] or ("；".join(f"{c['kind']}({c['detail']})" for c in failed) or "全部断言通过")
        lines.append(
            f"| {case['label']} | {case['type']} | {'✅' if case['success'] else '❌'} | "
            f"{case['tool_calls_ok']}/{case['tool_call_count']} | {case['llm_calls']} | "
            f"{case['latency_ms']:.0f} | {note[:120]} |"
        )
    lines.append("")
    return "\n".join(lines)


def write_outputs(result: dict[str, Any], ctx) -> dict[str, Path]:
    payload = {"meta": ctx.meta(), "aggregates": result["aggregates"], "cases": result["cases"]}
    return {
        "json": common.write_json(common.RESULTS_DIR / "agent_results.json", payload),
        "csv": common.write_csv(
            common.RESULTS_DIR / "agent_cases.csv", to_csv_rows(result["cases"]), CSV_COLUMNS
        ),
        "summary": common.write_markdown(
            common.RESULTS_DIR / "agent_summary.md", summary_markdown(result, ctx)
        ),
    }
