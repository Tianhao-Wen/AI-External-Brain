"""Benchmark 公共基座：隔离工作区、模型解析、调用预算、Git/环境元信息。

这里刻意不引入任何第三方依赖（除项目自身的 ``aieb`` / ``pymupdf`` / ``python-pptx``），
以保证 ``python -m benchmarks.run`` 在干净环境里可直接跑起来。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

BENCH_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = BENCH_ROOT.parent
DATASETS_DIR = BENCH_ROOT / "datasets"
RESULTS_DIR = BENCH_ROOT / "results"
REPORTS_DIR = BENCH_ROOT / "reports"
WORK_ROOT = BENCH_ROOT / ".work"

# 正式业务运行目录（Demo 录屏用）。Benchmark 全程只读、绝不写入。
PRODUCTION_RUNTIME_DIR = PROJECT_ROOT / ".aieb"

DATASET_VERSION = "rag-v1 / meeting-v1 / agent-v1"


# ---------------------------------------------------------------------------
# 环境隔离
# ---------------------------------------------------------------------------


def neutralise_env() -> list[str]:
    """强制邮件隔离：清掉进程内的 SMTP 凭据并打开 dry-run。

    Benchmark 里所有发信测试都走 Fake SMTP（替换 ``aieb.brain.mailer.send_email``
    这一传输边界）。这里再做一层保险：即使代码某处绕过 Fake 走到真实 smtplib，
    缺凭据 + dry-run 也会让发送失败，而不会真的把邮件发出去。
    """
    removed: list[str] = []
    for key in list(os.environ):
        upper = key.upper()
        if "SMTP" in upper and (upper.startswith("AIEB_") or upper.startswith("WENDREAM_")):
            os.environ.pop(key, None)
            removed.append(key)
    for key in ("AIEB_SMTP_HOST", "AIEB_SMTP_PORT", "AIEB_SMTP_USER", "AIEB_SMTP_PASSWORD"):
        os.environ.pop(key, None)
    os.environ["AIEB_EMAIL_DRY_RUN"] = "1"
    # 联网渲染 / 搜索在 fixture 模式下必须彻底关掉，避免任何外部请求。
    os.environ.setdefault("AIEB_WEB_RENDER", "0")
    return removed


def load_dotenv(path: Path | None = None) -> int:
    """读取项目根 ``.env``（与 ``aieb.brain.cli`` 行为一致），只补缺失的键。"""
    path = path or (PROJECT_ROOT / ".env")
    if not path.is_file():
        return 0
    loaded = 0
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return 0
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value
            loaded += 1
    return loaded


# ---------------------------------------------------------------------------
# Git / 运行元信息
# ---------------------------------------------------------------------------


def _git(*args: str) -> str:
    try:
        out = subprocess.run(
            ["git", *args],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def git_info() -> dict[str, Any]:
    return {
        "commit": _git("rev-parse", "HEAD"),
        "commit_short": _git("rev-parse", "--short", "HEAD"),
        "branch": _git("rev-parse", "--abbrev-ref", "HEAD"),
        "dirty": bool(_git("status", "--porcelain")),
    }


def fingerprint_dir(path: Path) -> dict[str, str]:
    """目录指纹：用于证明评测过程没有改动正式 ``.aieb/`` 演示数据。

    只看文件名/大小/mtime，避免把业务内容读进内存（也别把用户数据写进报告）。
    """
    result: dict[str, str] = {}
    if not path.exists():
        return result
    for file in sorted(path.rglob("*")):
        if not file.is_file():
            continue
        try:
            stat = file.stat()
        except OSError:
            continue
        key = str(file.relative_to(path)).replace("\\", "/")
        result[key] = f"{stat.st_size}:{stat.st_mtime_ns}"
    return result


def digest(mapping: dict[str, str]) -> str:
    payload = json.dumps(mapping, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]


def demo_env_guard() -> dict[str, Any]:
    before = fingerprint_dir(PRODUCTION_RUNTIME_DIR)
    return {"files": len(before), "digest": digest(before), "_before": before}


def demo_env_unchanged(guard: dict[str, Any]) -> bool:
    after = fingerprint_dir(PRODUCTION_RUNTIME_DIR)
    return digest(after) == guard["digest"]


# ---------------------------------------------------------------------------
# 脱敏
# ---------------------------------------------------------------------------


_SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b"),
    re.compile(r"(?i)(authorization|api[_\-]?key|token|password|passwd)\s*[:=]\s*\S+"),
)


def redact(text: str) -> str:
    """把可能出现的密钥 / 邮箱在落盘前抹掉。"""
    if not text:
        return ""
    out = text
    for pattern in _SECRET_PATTERNS:
        out = pattern.sub("[REDACTED]", out)
    return out


# ---------------------------------------------------------------------------
# 模型提供方
# ---------------------------------------------------------------------------


@dataclass
class ModelRef:
    """对外报告的模型标识：**绝不含 api_key**。"""

    name: str
    protocol: str
    model: str
    endpoint_host: str
    context_window: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.name,
            "protocol": self.protocol,
            "model": self.model,
            "endpoint_host": self.endpoint_host,
            "context_window": self.context_window,
        }


def resolve_provider(offline: bool = False):
    """返回 (ProviderConfig, ModelRef)。

    ``offline=True``（fixture 模式）返回一个永远不联网的占位 provider，
    任何真实 LLM 调用都会在预算/网络层失败——这样 fixture 结果不会被误当成
    模型能力。
    """
    from aieb.config import ProviderConfig, load_config

    if offline:
        cfg = ProviderConfig(
            name="offline-fixture",
            protocol="openai-compat",
            base_url="http://127.0.0.1:9/offline",
            model="offline-fixture (no LLM)",
            api_key="offline",
        )
        return cfg, ModelRef(
            name=cfg.name,
            protocol=cfg.protocol,
            model=cfg.model,
            endpoint_host="127.0.0.1 (offline)",
            context_window=0,
        )

    load_dotenv()
    app = load_config()
    provider = app.providers[0]
    if not provider.resolve_api_key():
        raise RuntimeError(
            "未配置模型 API Key：请在项目 .env 中设置 DEEPSEEK_API_KEY，"
            "或用 --mode fixture 运行不依赖模型的检索评测。"
        )
    host = re.sub(r"^https?://", "", provider.base_url).split("/")[0]
    return provider, ModelRef(
        name=provider.name,
        protocol=provider.protocol,
        model=provider.model,
        endpoint_host=host,
        context_window=provider.get_context_window(),
    )


# ---------------------------------------------------------------------------
# 调用预算
# ---------------------------------------------------------------------------


class BudgetExceeded(RuntimeError):
    """LLM 调用次数超过 ``--max-calls``。"""


@dataclass
class CallBudget:
    max_calls: int = 0  # 0 = 不限
    used: int = 0
    failures: int = 0

    def consume(self, label: str = "") -> None:
        if self.max_calls and self.used + 1 > self.max_calls:
            raise BudgetExceeded(
                f"已达到最大 LLM 调用次数 {self.max_calls}（最后一次尝试：{label}）"
            )
        self.used += 1

    def as_dict(self) -> dict[str, Any]:
        return {"max_calls": self.max_calls, "used": self.used, "failures": self.failures}


@contextmanager
def patch_llm_counter(budget: CallBudget) -> Iterator[None]:
    """给 ``aieb.brain.llm.complete`` 套一层计数 + 预算拦截。

    RAG 回答、会议分析、邮件草稿、PPT 大纲全部走这个函数，
    因此这里是"一条调用 = 一次计费"的唯一入口。
    """
    from aieb.brain import llm

    original = llm.complete

    async def counted(*args: Any, **kwargs: Any):
        budget.consume("llm.complete")
        try:
            return await original(*args, **kwargs)
        except Exception:
            budget.failures += 1
            raise

    llm.complete = counted  # type: ignore[assignment]
    try:
        yield
    finally:
        llm.complete = original  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# 运行上下文
# ---------------------------------------------------------------------------


def _timestamp() -> str:
    return time.strftime("%Y%m%d_%H%M%S")


@dataclass
class RunContext:
    suite: str
    mode: str
    run_id: str
    run_dir: Path
    provider: Any
    model: ModelRef
    budget: CallBudget
    git: dict[str, Any]
    demo_guard: dict[str, Any]
    created_at: str
    notes: list[str] = field(default_factory=list)

    def workspace(self, name: str) -> Path:
        """新建一个绝对隔离的 workspace（``BrainService(work_dir=...)`` 的根）。"""
        path = self.run_dir / "workspaces" / name
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def meta(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "suite": self.suite,
            "mode": self.mode,
            "created_at": self.created_at,
            "dataset_version": DATASET_VERSION,
            "git": self.git,
            "model": self.model.as_dict(),
            "budget": self.budget.as_dict(),
            "demo_env": {k: v for k, v in self.demo_guard.items() if not k.startswith("_")},
            "python": _python_version(),
            "platform": os.name,
            "notes": self.notes,
        }


def _python_version() -> str:
    import platform

    return platform.python_version()


def prepare_run(suite: str, mode: str, max_calls: int = 0, run_id: str = "") -> RunContext:
    neutralise_env()
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    WORK_ROOT.mkdir(parents=True, exist_ok=True)

    run_id = run_id or f"{_timestamp()}_{suite}_{mode}"
    run_dir = WORK_ROOT / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    provider, model = resolve_provider(offline=(mode == "fixture"))
    return RunContext(
        suite=suite,
        mode=mode,
        run_id=run_id,
        run_dir=run_dir,
        provider=provider,
        model=model,
        budget=CallBudget(max_calls=max_calls),
        git=git_info(),
        demo_guard=demo_env_guard(),
        created_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
    )


# ---------------------------------------------------------------------------
# 文件输出
# ---------------------------------------------------------------------------


def write_json(path: Path, payload: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    return path


def write_csv(path: Path, rows: list[dict[str, Any]], columns: list[str]) -> Path:
    import csv

    path.parent.mkdir(parents=True, exist_ok=True)
    # utf-8-sig：Excel 直接双击打开不乱码
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: _csv_cell(row.get(k)) for k in columns})
    return path


def _csv_cell(value: Any) -> Any:
    if isinstance(value, (list, tuple)):
        return " | ".join(str(v) for v in value)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, bool):
        return "true" if value else "false"
    return value


def write_markdown(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("//"):
            items.append(json.loads(line))
    return items


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "\n".join(json.dumps(r, ensure_ascii=False) for r in rows)
    path.write_text(text + "\n", encoding="utf-8")
    return path


def percentile(values: list[float], pct: float) -> float:
    """线性插值分位数（与 numpy 默认一致，避免依赖 numpy）。"""
    if not values:
        return 0.0
    data = sorted(values)
    if len(data) == 1:
        return float(data[0])
    rank = (len(data) - 1) * (pct / 100.0)
    low = int(rank)
    high = min(low + 1, len(data) - 1)
    weight = rank - low
    return float(data[low] * (1 - weight) + data[high] * weight)
