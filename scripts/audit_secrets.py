"""发布前密钥扫描：在准备公开仓库/分享包之前运行。

用法:
    python scripts/audit_secrets.py            # 扫描当前仓库（跳过 ignore 目录）
    python scripts/audit_secrets.py --all      # 连被 .gitignore 覆盖的目录也扫
    python scripts/audit_secrets.py --json out.json

输出只包含 file / line / type / masked，绝不会打印完整 secret。
发现高置信度密钥时以退出码 1 结束，便于挂进 pre-release checklist / CI。
"""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# 默认跳过的目录：依赖、构建产物、缓存
SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    "env",
    ".conda",
    "conda-meta",
    "pkgs",
    "Lib",
    "Scripts",
    "site-packages",
    ".mypy_cache",
    ".ruff_cache",
    ".pytest_cache",
    "dist",
    "build",
}

# 这些后缀是二进制/大文件，逐行扫描没有意义
SKIP_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf", ".zip", ".gz",
    ".tar", ".whl", ".exe", ".dll", ".so", ".pyd", ".pyc", ".woff", ".woff2",
    ".ttf", ".mp4", ".mp3", ".db", ".sqlite",
}

MAX_FILE_BYTES = 2 * 1024 * 1024

# 出现这些字样时，该行视为“已知的安全占位值”
PLACEHOLDER_HINTS = (
    "${",
    "<redacted>",
    "your-api-key",
    "your_api_key",
    "example",
    "placeholder",
    "changeme",
    "xxx",
    "dummy",
    "audit:ignore",
    "pragma: allowlist secret",
)

# 扫描器自身包含这些模式的样例字符串，避免自命中
SELF_PATH = Path(__file__).resolve()


@dataclass(frozen=True)
class Rule:
    name: str
    pattern: re.Pattern[str]
    confidence: str  # "high" | "medium"


RULES: tuple[Rule, ...] = (
    Rule("openai/anthropic-style key", re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}"), "high"),
    Rule("github token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}"), "high"),
    Rule("github fine-grained pat", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"), "high"),
    Rule("aws access key id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), "high"),
    Rule("slack token", re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}"), "high"),
    Rule("google api key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"), "high"),
    Rule(
        "private key block",
        re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY-----"),
        "high",
    ),
    Rule(
        "bearer token",
        re.compile(r"(?i)\bbearer\s+([A-Za-z0-9_\-\.=]{20,})"),
        "medium",
    ),
    Rule(
        "credential assignment",
        re.compile(
            r"""(?ix)
            (api[_-]?key|secret|token|password|passwd|access[_-]?key|client[_-]?secret)
            \s*[:=]\s*
            ['"]?([A-Za-z0-9_\-\./+=]{24,})['"]?
            """
        ),
        "medium",
    ),
)


def mask(secret: str) -> str:
    """把 secret 脱敏成 sk-abcd…wxyz 形式（保留首尾各 4 位）。"""
    s = secret.strip().strip("'\"")
    if len(s) <= 8:
        return s[:2] + "…" + s[-1:] if len(s) > 2 else "…"
    return f"{s[:4]}…{s[-4:]}"


def _iter_files(root: Path, include_ignored: bool) -> list[Path]:
    files: list[Path] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel_parts = set(path.relative_to(root).parts)
        if rel_parts & SKIP_DIRS:
            continue
        if path.suffix.lower() in SKIP_SUFFIXES:
            continue
        if not include_ignored and _is_gitignored(path, root):
            continue
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                continue
        except OSError:
            continue
        files.append(path)
    return files


def _load_gitignore_patterns(root: Path) -> list[str]:
    gitignore = root / ".gitignore"
    if not gitignore.exists():
        return []
    patterns: list[str] = []
    for line in gitignore.read_text(encoding="utf-8", errors="replace").splitlines():
        text = line.strip()
        if not text or text.startswith("#") or text.startswith("!"):
            continue
        patterns.append(text)
    return patterns


def _is_gitignored(path: Path, root: Path) -> bool:
    """极简 .gitignore 匹配：够用于“发布前不要扫用户运行数据”这一目的。"""
    patterns = _load_gitignore_patterns(root)
    if not patterns:
        return False
    rel = path.relative_to(root).as_posix()
    for pattern in patterns:
        p = pattern.lstrip("/")
        if p.endswith("/"):
            if rel == p[:-1] or rel.startswith(p):
                return True
            continue
        if "/" in p:
            if rel == p or rel.startswith(p.rstrip("/") + "/"):
                return True
        else:
            if rel == p or rel.endswith("/" + p) or Path(rel).name == p:
                return True
            if re.fullmatch(re.escape(p).replace(r"\*", ".*"), Path(rel).name):
                return True
    return False


def scan_file(path: Path) -> list[dict]:
    findings: list[dict] = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return findings
    for lineno, line in enumerate(text.splitlines(), start=1):
        lowered = line.lower()
        if any(hint in lowered for hint in PLACEHOLDER_HINTS):
            continue
        for rule in RULES:
            match = rule.pattern.search(line)
            if not match:
                continue
            raw = match.group(match.lastindex or 0)
            findings.append(
                {
                    "file": str(path),
                    "line": lineno,
                    "type": rule.name,
                    "confidence": rule.confidence,
                    "masked": mask(raw),
                }
            )
    return findings


def scan_repo(root: Path, include_ignored: bool = False) -> list[dict]:
    findings: list[dict] = []
    for path in _iter_files(root, include_ignored):
        if path.resolve() == SELF_PATH:
            continue
        findings.extend(scan_file(path))
    return findings


def main() -> None:
    parser = argparse.ArgumentParser(description="Scan repository for leaked secrets")
    parser.add_argument("--root", default=str(REPO_ROOT))
    parser.add_argument("--all", action="store_true", help="also scan gitignored paths")
    parser.add_argument("--json", default=None, help="write findings as JSON")
    args = parser.parse_args()

    root = Path(args.root).resolve()
    findings = scan_repo(root, include_ignored=args.all)

    high = [f for f in findings if f["confidence"] == "high"]
    medium = [f for f in findings if f["confidence"] == "medium"]

    print(f"scanned root: {root}")
    print(f"high-confidence findings: {len(high)} | medium: {len(medium)}")
    for item in findings:
        rel = Path(item["file"]).resolve()
        try:
            shown = rel.relative_to(root).as_posix()
        except ValueError:
            shown = str(rel)
        print(f"  [{item['confidence']:6s}] {shown}:{item['line']}  {item['type']}  {item['masked']}")

    if args.json:
        Path(args.json).write_text(json.dumps(findings, indent=2), encoding="utf-8")
        print(f"written -> {args.json}")

    if high:
        print("\nRESULT: potential real secrets found — do not publish until resolved.")
        raise SystemExit(1)
    print("\nRESULT: no high-confidence secrets detected.")


if __name__ == "__main__":
    main()
