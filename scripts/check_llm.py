"""检查真实 LLM 配置并做一次真实调用（不打印任何 API Key）。

用法::

    python scripts/check_llm.py                 # 报告配置 + 用当前 provider 真实调用一次
    python scripts/check_llm.py --no-call       # 只报告配置
    python scripts/check_llm.py --probe-qwen    # 额外探测百炼(通义/Qwen)兼容端点是否可用

输出内容只包含：provider 名称、协议、base_url、model ID、密钥是否就绪（布尔）、
请求状态、耗时、token 用量、回答前若干字符。**任何密钥值都不会被打印。**
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from aieb.client import create_client  # noqa: E402
from aieb.config import load_config, resolve_env_vars  # noqa: E402
from aieb.conversation import ConversationManager, Message  # noqa: E402
from aieb.tools.base import StreamEnd, TextDelta  # noqa: E402

_PLACEHOLDER = re.compile(r"\$\{([^}]+)\}")


def key_source(provider) -> str:
    """只说密钥来自哪个环境变量，不说值。"""
    raw = provider.api_key or ""
    match = _PLACEHOLDER.search(raw)
    if match:
        name = match.group(1)
        resolved = resolve_env_vars(raw)
        ok = bool(resolved) and "${" not in resolved
        return f"${{{name}}} → {'已设置' if ok else '未设置'}"
    if raw:
        return "配置文件内直接写入（不推荐）"
    env_name = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY",
                "openai-compat": "OPENAI_API_KEY"}.get(provider.protocol, "")
    if env_name and os.environ.get(env_name):
        return f"环境变量 {env_name}（协议默认回退）→ 已设置"
    return "未配置"


def report_config() -> bool:
    cwd = Path.cwd()
    home = Path.home()
    print("== 配置文件层级（后加载的覆盖前面的）==")
    for path in (home / ".aieb" / "config.yaml", cwd / ".aieb" / "config.yaml",
                 cwd / ".aieb" / "config.local.yaml"):
        print(f"  {'存在' if path.exists() else '缺失'}  {path}")

    config = load_config()
    print(f"\n== providers（共 {len(config.providers)} 个，第一个为当前实际使用）==")
    for index, provider in enumerate(config.providers):
        print(f"  [{index}] name={provider.name}")
        print(f"      protocol   = {provider.protocol}")
        print(f"      base_url   = {provider.base_url}")
        print(f"      model      = {provider.model}")
        print(f"      display    = {provider.display_name or '—'}")
        print(f"      api_key    = {key_source(provider)}")
        print(f"      ctx_window = {provider.get_context_window()}")
    if not config.providers:
        print("  （无可用 provider）")
        return False

    print("\n== 其他相关环境变量（只判断是否设置）==")
    for name in ("DEEPSEEK_API_KEY", "DASHSCOPE_API_KEY", "ALIYUN_BAILIAN_API_KEY",
                 "DASHSCOPE_BASE_URL", "OPENAI_API_KEY", "ANTHROPIC_API_KEY",
                 "TAVILY_API_KEY"):
        print(f"  {name}: {'已设置' if os.environ.get(name) else '未设置'}")

    print("\n== 联网搜索配置 ==")
    ws = config.web_search
    print(f"  enabled={ws.enabled} provider={ws.provider} "
          f"native_enabled={ws.native_enabled} native_provider={ws.native_provider or '—'}")
    return True


async def real_call() -> bool:
    config = load_config()
    provider = config.providers[0]
    print(f"\n== 真实调用 ==\n  provider={provider.name} model={provider.model} "
          f"protocol={provider.protocol}")
    client = create_client(provider)
    conv = ConversationManager()
    conv.history = [Message(role="user",
                            content="请只回复一行：模型连通性测试通过。")]
    started = time.monotonic()
    parts: list[str] = []
    usage = None
    try:
        async for event in client.stream(conv, system="你是一个严谨的连通性测试助手。"):
            if isinstance(event, TextDelta):
                parts.append(event.text)
            elif isinstance(event, StreamEnd):
                usage = event
                break
    except Exception as exc:  # noqa: BLE001 - 失败必须如实报告
        print(f"  状态：FAILED\n  错误：{type(exc).__name__}: {str(exc)[:300]}")
        return False

    elapsed = time.monotonic() - started
    text = "".join(parts).strip()
    print(f"  状态：OK（HTTP 层已由 SDK 正常返回）")
    print(f"  耗时：{elapsed:.2f}s")
    if usage is not None:
        print(f"  tokens：input={usage.input_tokens} output={usage.output_tokens} "
              f"stop_reason={usage.stop_reason}")
    print(f"  响应（前 80 字）：{text[:80]}")
    return bool(text)


def _post_chat(base: str, model: str, key: str, timeout: float = 60.0):
    """向 OpenAI 兼容端点发一次最小请求，返回 (ok, info)。"""
    payload = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": "只回复：Qwen 连通性通过"}],
        "max_tokens": 32,
        "temperature": 0,
    }).encode("utf-8")
    request = urllib.request.Request(
        base.rstrip("/") + "/chat/completions",
        data=payload,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        method="POST",
    )
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        detail = exc.read(400).decode("utf-8", "replace")
        code = ""
        try:
            code = (json.loads(detail).get("error") or {}).get("code", "")
        except Exception:  # noqa: BLE001
            pass
        return False, f"HTTP {exc.code} {code} {detail[:180]}"
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {str(exc)[:180]}"
    elapsed = time.monotonic() - started
    choice = (body.get("choices") or [{}])[0]
    content = (choice.get("message") or {}).get("content", "")
    return bool(content), (f"HTTP=200 耗时={elapsed:.2f}s model={body.get('model')} "
                           f"usage={body.get('usage')} 响应={str(content)[:60]}")


def probe_qwen(base_override: str = "", model_override: str = "") -> bool:
    """探测通义千问（百炼）是否可真正调用。

    同时探测两个入口，因为它们的 Key 与模型 ID 不同：
      1. DashScope 公共兼容端点（Key: DASHSCOPE_API_KEY，模型 qwen-plus）
      2. 百炼 Token Plan 端点（Key: ALIYUN_BAILIAN_API_KEY，模型 qwen3.8-max，
         与原项目配置一致）
    """
    bailian_key = os.environ.get("ALIYUN_BAILIAN_API_KEY", "")
    dash_key = os.environ.get("DASHSCOPE_API_KEY", "")
    candidates: list[tuple[str, str, str, str]] = []
    if base_override:
        candidates.append(("override", base_override, model_override or "qwen-plus",
                           dash_key or bailian_key))
    else:
        candidates.append((
            "dashscope 公共端点",
            os.environ.get("DASHSCOPE_BASE_URL",
                           "https://dashscope.aliyuncs.com/compatible-mode/v1"),
            "qwen-plus", dash_key or bailian_key,
        ))
        candidates.append((
            "百炼 Token Plan 端点（原项目配置）",
            "https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
            "qwen3.8-max", bailian_key,
        ))

    print("\n== 通义千问（Qwen）可用性探测 ==")
    any_ok = False
    for label, base, model, key in candidates:
        print(f"\n  [{label}]\n    base_url={base}\n    model={model}")
        if not key:
            print("    状态：SKIP（对应环境变量未设置）")
            continue
        ok, info = _post_chat(base, model, key)
        print(f"    状态：{'OK' if ok else 'FAILED'}\n    {info}")
        any_ok = any_ok or ok
    return any_ok


def raw_active_call() -> bool:
    """用一次原始 HTTP 调用确认服务端**实际返回**的 model 字段（而非仅配置文件里的值）。"""
    config = load_config()
    provider = config.providers[0]
    key = provider.resolve_api_key()
    print(f"\n== 原始 HTTP 调用（确认实际返回的 model） ==")
    print(f"  provider={provider.name} 请求 model={provider.model}")
    if not key:
        print("  状态：SKIP（密钥未就绪）")
        return False
    payload = json.dumps({
        "model": provider.model,
        "messages": [{"role": "user", "content": "只回复：raw ok"}],
        "max_tokens": 16,
        "temperature": 0,
    }).encode("utf-8")
    request = urllib.request.Request(
        provider.base_url.rstrip("/") + "/chat/completions",
        data=payload,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            body = json.loads(response.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        print(f"  状态：HTTP {exc.code}\n  {exc.read(300).decode('utf-8', 'replace')}")
        return False
    except Exception as exc:  # noqa: BLE001
        print(f"  状态：FAILED  {type(exc).__name__}: {str(exc)[:200]}")
        return False
    choice = (body.get("choices") or [{}])[0]
    content = (choice.get("message") or {}).get("content", "")
    print(f"  状态：HTTP 200")
    print(f"  响应 model 字段 = {body.get('model')}")
    print(f"  response id = {body.get('id')}")
    print(f"  usage = {body.get('usage')}")
    print(f"  回答（前 40 字）= {str(content)[:40]}")
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-call", action="store_true")
    parser.add_argument("--probe-qwen", action="store_true")
    parser.add_argument("--qwen-base", default="", help="自定义 Qwen 兼容端点")
    parser.add_argument("--qwen-model", default="", help="自定义 Qwen 模型 ID")
    parser.add_argument("--raw", action="store_true",
                        help="额外用原始 HTTP 调用确认服务端实际返回的 model 字段")
    args = parser.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

    if not report_config():
        print("\n没有可用 provider，无法调用。")
        return 1
    ok = True
    if not args.no_call:
        ok = asyncio.run(real_call()) and ok
    if args.probe_qwen:
        probe_qwen(args.qwen_base, args.qwen_model)  # 探测结果不影响主流程退出码
    if args.raw:
        raw_active_call()
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
