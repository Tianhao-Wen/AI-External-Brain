"""静态校验 Docker 资产（Docker 不可用时也能给出真实检查结果）。

检查项全部是对**文件内容**的确定性断言，不假装跑过容器：

1. Dockerfile：基础镜像是否带明确 tag、是否先装依赖再 COPY 源码、是否非 root、
   是否有 HEALTHCHECK、是否避免 `COPY . .`（防止把密钥/数据打进镜像）；
2. .dockerignore：是否排除 .env / .aieb / 虚拟环境 / SQLite / PDF；
3. docker-compose.yml：服务齐全、端口宿主=容器（Origin 校验依赖）、
   数据卷齐备、healthcheck 与 depends_on 存在、环境变量里没有硬编码密钥；
4. .env.example / docker/config.docker.yaml：不得含真实密钥或非空口令。

    python scripts/validate_docker_assets.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str) -> None:
    RESULTS.append((name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}")


def validate_dockerfile() -> None:
    path = ROOT / "Dockerfile"
    text = path.read_text(encoding="utf-8")
    lines = [ln.strip() for ln in text.splitlines()]

    from_lines = [ln for ln in lines if ln.upper().startswith("FROM ")]
    check(
        "dockerfile.python_base_pinned",
        bool(from_lines) and all(":" in ln for ln in from_lines),
        f"FROM 指令：{from_lines}",
    )

    idx_req = next((i for i, ln in enumerate(lines) if ln.startswith("COPY requirements")), -1)
    idx_src = next((i for i, ln in enumerate(lines) if ln.startswith("COPY aieb")), -1)
    check(
        "dockerfile.layer_order",
        idx_req >= 0 and idx_src > idx_req,
        f"依赖 COPY 行 {idx_req} 在源码 COPY 行 {idx_src} 之前（依赖层可缓存）",
    )

    check("dockerfile.non_root", any(ln.upper().startswith("USER ") for ln in lines),
          next((ln for ln in lines if ln.upper().startswith("USER ")), "缺少 USER 指令"))
    check("dockerfile.healthcheck", any(ln.upper().startswith("HEALTHCHECK") for ln in lines),
          "存在 HEALTHCHECK 指令")
    check("dockerfile.no_copy_all", "COPY . ." not in text and "COPY . /app" not in text,
          "未使用整目录 COPY（避免把 .env / .aieb / PDF 打进镜像）")
    check("dockerfile.entrypoint_exec", 'ENTRYPOINT ["/usr/bin/tini"' in text,
          "使用 tini 作为 PID1 并 exec 启动（信号可传递）")
    check(
        "dockerfile.expose_matches_default_port",
        "EXPOSE 18888" in text and "AIEB_PORT=18888" in text,
        "EXPOSE 与默认监听端口一致（18888）",
    )
    # 镜像构建期不得出现密钥字面量
    leaked = re.findall(r"(sk-[A-Za-z0-9]{12,}|AIEB_REMOTE_TOKEN=\S{8,})", text)
    check("dockerfile.no_inline_secret", not leaked, f"未发现密钥字面量（命中：{leaked or '无'}）")


def validate_dockerignore() -> None:
    text = (ROOT / ".dockerignore").read_text(encoding="utf-8")
    patterns = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    required = [".env", ".aieb/", ".venv", "__pycache__", "*.sqlite3", "*.pdf", ".git"]
    missing = [item for item in required if item not in patterns]
    check("dockerignore.excludes_sensitive", not missing, f"缺失排除项：{missing or '无'}")


def validate_compose() -> None:
    path = ROOT / "docker-compose.yml"
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    services = data.get("services") or {}
    check("compose.has_services", {"aieb", "qdrant"} <= set(services), f"services={sorted(services)}")

    aieb = services.get("aieb") or {}
    qdrant = services.get("qdrant") or {}

    check("compose.aieb_build", "build" in aieb, f"build={aieb.get('build')}")
    check("compose.aieb_healthcheck", "healthcheck" in aieb, "后端配置了 healthcheck")
    check("compose.qdrant_healthcheck", "healthcheck" in qdrant, "Qdrant 配置了 healthcheck")
    check(
        "compose.depends_on",
        "qdrant" in (aieb.get("depends_on") or {}),
        f"depends_on={aieb.get('depends_on')}",
    )

    env = aieb.get("environment") or {}
    check(
        "compose.qdrant_url_uses_service_name",
        "qdrant" in str(env.get("AIEB_QDRANT_URL", "")) and "127.0.0.1" not in str(env.get("AIEB_QDRANT_URL", "")),
        f"AIEB_QDRANT_URL={env.get('AIEB_QDRANT_URL')}",
    )

    secrets_hardcoded = {
        k: v for k, v in env.items()
        if isinstance(v, str) and re.search(r"sk-[A-Za-z0-9]{12,}", v)
    }
    check("compose.no_hardcoded_secret", not secrets_hardcoded, f"硬编码密钥：{secrets_hardcoded or '无'}")

    ports = aieb.get("ports") or []
    # 端口一致性：宿主与容器必须相同，否则 Origin 校验会 403。
    # 注意 `${AIEB_PORT:-18888}` 里含有冒号，不能直接按 ":" 拆。
    def _normalize(expr: str) -> str:
        return re.sub(r"\$\{[^}:]+:-([^}]*)\}", r"\1", str(expr))

    consistent = False
    for item in ports:
        host, _, container = _normalize(item).partition(":")
        if host and host == container:
            consistent = True
    check("compose.port_host_equals_container", consistent, f"ports={ports}")

    volumes = aieb.get("volumes") or []
    check(
        "compose.data_volume_mounted",
        any("/data" in str(v) for v in volumes),
        f"volumes={volumes}",
    )
    top_volumes = data.get("volumes") or {}
    check(
        "compose.named_volumes",
        {"aieb_data", "qdrant_storage"} <= set(top_volumes),
        f"volumes={sorted(top_volumes)}",
    )
    dry_run = str(env.get("AIEB_EMAIL_DRY_RUN", ""))
    check(
        "compose.email_dry_run_default",
        dry_run in {"1", "true", "True"} or dry_run.endswith(":-1}"),
        f"AIEB_EMAIL_DRY_RUN={dry_run}（默认禁止真实发信）",
    )


def validate_env_files() -> None:
    env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
    secret_keys = {
        "DEEPSEEK_API_KEY",
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "TAVILY_API_KEY",
        "AIEB_SMTP_PASSWORD",
        "AIEB_REMOTE_TOKEN",
        "QDRANT_API_KEY",
        "AIEB_QDRANT_API_KEY",
    }
    suspicious: list[str] = []
    for line in env_example.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        if key.strip() not in secret_keys:
            continue
        value = value.strip().strip('"').strip("'")
        # 允许空值、示例值、占位符；其余视为疑似真实密钥
        if value and not re.match(r"^(example|change-me|<.*>)$", value):
            suspicious.append(key.strip())
    check("env_example.no_real_secret", not suspicious, f"可疑非空密钥行：{suspicious or '无'}")

    template = (ROOT / "docker" / "config.docker.yaml").read_text(encoding="utf-8")
    hardcoded_key = re.search(r"api_key:\s*\"(?:\$\{)?(sk-[^\"]+)\"", template)
    check(
        "config_template.no_hardcoded_key",
        hardcoded_key is None,
        "config.docker.yaml 用 ${DEEPSEEK_API_KEY} 占位，无硬编码密钥",
    )


def main() -> int:
    validate_dockerfile()
    validate_dockerignore()
    validate_compose()
    validate_env_files()
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print()
    print(f"静态校验：{passed}/{total} 通过")
    for name, ok, detail in RESULTS:
        if not ok:
            print(f"  x {name}: {detail}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
