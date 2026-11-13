"""``aieb`` 命令行入口：以浏览器 UI 方式启动「AI 外脑 Demo」。

与原始项目的差别：
* 只启动 Web 工作台（不进入 TUI），产品默认首页是工作台而不是聊天窗口；
* 运行时数据全部落在项目内 ``.aieb/``，与其它项目互不影响；
* 默认只监听 loopback，非本机绑定必须提供 token（行为沿用原安全模型）。
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

from aieb import __version__ as framework_version
from aieb.brain import __version__ as product_version
from aieb.brain.paths import acquire_server_lock, project_runtime_dir


def _load_dotenv(path: Path) -> int:
    """把项目根目录 ``.env`` 里的 ``KEY=VALUE`` 读进环境变量。

    已存在的环境变量优先（不会被文件覆盖），所以临时 export 依旧能覆盖文件配置。
    只支持最常见的写法：一行一个 KEY=VALUE，``#`` 开头为注释，值可用引号包裹。
    返回实际写入的条目数（不打印任何值，避免密钥进入日志）。
    """
    if not path.is_file():
        return 0
    loaded = 0
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0
    for raw in content.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if not key or key in os.environ:
            continue
        os.environ[key] = value
        loaded += 1
    return loaded


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="aieb",
        description="AI 外脑 Demo —— 企业知识管理 / 会议分析 / 待办追踪的 AI 工作台",
    )
    parser.add_argument("--version", action="version",
                        version=f"AI 外脑 Demo {product_version} (agent framework {framework_version})")
    parser.add_argument("--host", default=None, help="监听地址（默认 127.0.0.1）")
    parser.add_argument("--port", type=int, default=None, help="监听端口（默认 18888）")
    parser.add_argument("--mode", default=None,
                        help="权限模式（ask / acceptEdits / bypassPermissions 等）")
    parser.add_argument("--allow-unauthenticated-remote", action="store_true", default=False,
                        help="危险：允许无 token 绑定非本机地址")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    from aieb.client import AuthenticationError
    from aieb.config import ConfigError, load_config
    from aieb.hooks import HookConfigError, HookEngine, load_hooks
    from aieb.remote import RemoteServer, resolve_remote_settings

    args = _parse_args(argv)

    # 先加载项目根目录的 .env（邮件 / 模型密钥都走这里），再读配置。
    # 这样用户不必每次启动前手动 export 一堆变量。
    dotenv_items = _load_dotenv(Path.cwd() / ".env")
    if dotenv_items:
        print(f"已从 .env 载入 {dotenv_items} 项配置", flush=True)

    runtime = project_runtime_dir(Path.cwd())
    runtime.mkdir(parents=True, exist_ok=True)

    # 同一工作区只允许一个服务实例：两个实例会互相占用 .aieb/sessions/*.jsonl，
    # 在 Windows 上表现为"删除会话失败（WinError 32 文件被占用）"。
    # 锁在进程退出时由操作系统自动释放，不会留下僵尸锁。
    instance_lock, holder = acquire_server_lock(runtime)
    if instance_lock is None:
        print(
            "启动失败：该工作区已经有一个 AI 外脑服务在运行"
            f"（PID {holder}）。\n"
            "同一目录同时跑两个实例会互相锁住会话文件，导致历史对话删不掉、"
            "写入冲突。\n"
            "请任选其一：\n"
            "  1) 先关闭已有服务（在它的终端里 Ctrl+C），再启动新的；\n"
            "  2) 关掉多余的浏览器标签页，只保留一个服务实例。\n",
            file=sys.stderr,
        )
        sys.exit(3)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(message)s",
        filename=str(runtime / "debug.log"),
        filemode="w",
    )

    try:
        config = load_config()
    except ConfigError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        print("请复制 .aieb/config.yaml.example 为 .aieb/config.yaml 并填写模型信息。",
              file=sys.stderr)
        sys.exit(1)

    try:
        hooks = load_hooks(config.raw_hooks)
    except HookConfigError as exc:
        print(f"Hook 配置错误：{exc}", file=sys.stderr)
        sys.exit(1)
    hook_engine = HookEngine(hooks) if hooks else None

    settings = resolve_remote_settings(
        config,
        host=args.host,
        port=args.port,
        allow_unauthenticated=args.allow_unauthenticated_remote,
    )
    if args.mode:
        try:
            config.permission_mode = args.mode
        except Exception:  # noqa: BLE001 - 模式字符串非法时保持配置默认值
            pass

    server = RemoteServer(
        providers=config.providers,
        mcp_servers=config.mcp_servers,
        hook_engine=hook_engine,
        addr=settings.host,
        port=settings.port,
        auth_token=settings.auth_token,
        allowed_origins=settings.allowed_origins,
        allow_unauthenticated_remote=settings.allow_unauthenticated,
        web_search_config=config.web_search,
    )

    print(f"AI 外脑 Demo 正在启动：http://{settings.host}:{settings.port}", flush=True)
    try:
        asyncio.run(server.run())
    except KeyboardInterrupt:
        print("\n已停止。", flush=True)
    except AuthenticationError as exc:
        # 全新环境下最常见的失败：填了 api_key 占位符但没设环境变量。
        # 这里给中文可操作提示，避免用户被裸堆栈劝退。
        print(f"\n启动失败：模型 API Key 无效或未配置。\n  {exc}", file=sys.stderr)
        print(
            "\n修复方式（任选其一）：\n"
            "  1) 设置环境变量后重启： PowerShell  $env:DEEPSEEK_API_KEY='sk-...'\n"
            "  2) 直接编辑 .aieb/config.yaml，把 api_key 写成真实密钥\n",
            file=sys.stderr,
        )
        sys.exit(2)


if __name__ == "__main__":
    main()
