"""Remote 安全：默认 loopback、token 认证、Origin 校验（P0-3 / P0-4 / P0-5）。"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from websockets.datastructures import Headers

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from aieb.config import RemoteConfig  # noqa: E402
from aieb.remote import (  # noqa: E402
    TOKEN_SUBPROTOCOL_PREFIX,
    RemoteAuthError,
    RemoteServer,
    is_loopback_host,
    resolve_remote_settings,
)


def _server(**kwargs) -> RemoteServer:
    defaults = dict(
        providers=[SimpleNamespace(name="p", protocol="anthropic", model="m")],
    )
    defaults.update(kwargs)
    return RemoteServer(**defaults)


def _request(headers: dict[str, str]) -> SimpleNamespace:
    return SimpleNamespace(headers=Headers(list(headers.items())))


# ---------------------------------------------------------------- 绑定地址


def test_default_bind_is_loopback():
    server = _server()
    assert server.addr == "127.0.0.1"
    assert is_loopback_host(server.addr)


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1", "127.0.0.5"])
def test_loopback_detection(host):
    assert is_loopback_host(host)


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.10", "example.com", "::"])
def test_non_loopback_detection(host):
    assert not is_loopback_host(host)


def test_resolve_settings_defaults_to_loopback():
    settings = resolve_remote_settings(None)
    assert settings.host == "127.0.0.1"
    assert settings.port == 18888
    assert settings.auth_token is None


def test_resolve_settings_reads_config_and_env(monkeypatch):
    cfg = SimpleNamespace(
        remote=RemoteConfig(host="0.0.0.0", port=9000, auth_token=None, allowed_origins=["http://x.test"])
    )
    monkeypatch.setenv("WENDREAM_REMOTE_TOKEN", "env-token-value")
    settings = resolve_remote_settings(cfg)
    assert settings.host == "0.0.0.0"
    assert settings.port == 9000
    assert settings.auth_token == "env-token-value"
    assert settings.allowed_origins == ["http://x.test"]


# ---------------------------------------------------------------- 启动防护


def test_external_bind_without_token_refuses_to_start():
    server = _server(addr="0.0.0.0")
    with pytest.raises(RemoteAuthError) as exc:
        server._validate_binding()
    assert "without authentication" in str(exc.value)


def test_external_bind_with_token_starts_with_warning(capsys):
    server = _server(addr="0.0.0.0", auth_token="t" * 32)
    server._validate_binding()  # 不抛异常
    err = capsys.readouterr().err
    assert "non-loopback" in err
    assert "t" * 32 not in err  # token 不得出现在输出


def test_external_bind_with_explicit_dangerous_flag_starts(capsys):
    server = _server(addr="0.0.0.0", allow_unauthenticated_remote=True)
    server._validate_binding()
    err = capsys.readouterr().err
    assert "WITHOUT authentication" in err


def test_loopback_without_token_is_allowed():
    _server()._validate_binding()  # 不应抛异常


# ---------------------------------------------------------------- token 认证


def test_token_required_when_configured_wrong_token_rejected():
    server = _server(auth_token="correct-token-value")
    request = _request({"Sec-WebSocket-Protocol": f"{TOKEN_SUBPROTOCOL_PREFIX}wrong"})
    ok, reason = server.authorize_request(request)
    assert (ok, reason) == (False, "token")


def test_token_accepted_via_subprotocol():
    server = _server(auth_token="correct-token-value")
    request = _request(
        {
            "Origin": f"http://127.0.0.1:{server.port}",
            "Sec-WebSocket-Protocol": f"aieb.v1, {TOKEN_SUBPROTOCOL_PREFIX}correct-token-value",
        }
    )
    ok, reason = server.authorize_request(request)
    assert ok, reason


def test_token_accepted_via_bearer_header():
    server = _server(auth_token="correct-token-value")
    request = _request({"Authorization": "Bearer correct-token-value"})
    ok, reason = server.authorize_request(request)
    assert ok, reason


def test_no_token_required_on_loopback():
    server = _server()
    ok, reason = server.authorize_request(_request({"Origin": f"http://localhost:{server.port}"}))
    assert ok, reason


def test_token_never_logged(caplog):
    token = "super-secret-remote-token"
    server = _server(addr="0.0.0.0", auth_token=token)
    with caplog.at_level("DEBUG"):
        server._validate_binding()
        server.authorize_request(_request({"Sec-WebSocket-Protocol": f"{TOKEN_SUBPROTOCOL_PREFIX}{token}"}))
    assert token not in caplog.text


# ---------------------------------------------------------------- Origin 校验


def test_valid_localhost_origin_allowed():
    server = _server()
    for origin in (
        f"http://127.0.0.1:{server.port}",
        f"http://localhost:{server.port}",
    ):
        ok, reason = server.authorize_request(_request({"Origin": origin}))
        assert ok, (origin, reason)


def test_evil_origin_rejected():
    server = _server()
    ok, reason = server.authorize_request(_request({"Origin": "http://evil.example"}))
    assert (ok, reason) == (False, "origin")


def test_custom_allowed_origin():
    server = _server(allowed_origins=["https://trusted.example"])
    ok, reason = server.authorize_request(_request({"Origin": "https://trusted.example"}))
    assert ok, reason


def test_wildcard_origin_is_not_allowed_by_default():
    server = _server()
    ok, _ = server.authorize_request(_request({"Origin": "*"}))
    assert not ok


def test_ws_path_returns_401_and_403():
    """HTTP 层：/ws 未授权时返回 401/403，而不是继续升级。"""
    server = _server(auth_token="tok")

    def _status(headers: dict[str, str]) -> int:
        response = server._process_http_request(
            None, SimpleNamespace(path="/ws", headers=Headers(list(headers.items())))
        )
        return getattr(response, "status_code", 101)

    assert _status({"Origin": "http://evil.example", "Sec-WebSocket-Protocol": "aieb-token.tok"}) == 403
    assert _status({"Origin": "http://127.0.0.1:18888", "Sec-WebSocket-Protocol": "aieb-token.bad"}) == 401
    assert _status({"Origin": "http://127.0.0.1:18888", "Sec-WebSocket-Protocol": "aieb-token.tok"}) == 101


# ---------------------------------------------------------------- 产物下载


# ------------------------------------------------ 产物下载（签名 URL）


def _file_server(tmp_path, **kwargs) -> RemoteServer:
    server = _server(**kwargs)
    server.agent = SimpleNamespace(work_dir=str(tmp_path))
    server.artifact_root = tmp_path / "_artifacts"
    return server


def _file_status(server: RemoteServer, path: str) -> int:
    response = server._process_http_request(
        None, SimpleNamespace(path=path, headers=Headers())
    )
    return getattr(response, "status_code", 0)


def test_file_route_requires_signed_url(tmp_path):
    """零散的 /file 请求（无 id / 无签名）一律拒绝，不能用来读任意文件。"""
    server = _file_server(tmp_path)
    assert _file_status(server, "/file") == 400
    assert _file_status(server, "/file?path=config.yaml") == 400
    assert _file_status(server, "/file?id=art_deadbeef&exp=9999999999&sig=nope") == 403
    assert _file_status(server, "/file?id=art_deadbeef&exp=1&sig=nope") == 403


def test_file_route_does_not_leak_workspace_files(tmp_path):
    """工作目录里的任意文件（源码/配置）都不能通过 /file 读到。"""
    (tmp_path / "secret.env").write_text("TOKEN=1", encoding="utf-8")
    server = _file_server(tmp_path)
    for attempt in (
        "/file?path=secret.env",
        "/file?id=secret.env&exp=9999999999&sig=deadbeef",
        "/file?id=../secret.env&exp=9999999999&sig=deadbeef",
    ):
        assert _file_status(server, attempt) != 200, attempt


# ---------------------------------------------------------------- 握手子协议


def test_serve_declares_ws_subprotocol(monkeypatch):
    """回归：服务端必须回显 ``wendream.v1`` 子协议。

    浏览器在请求头里带了 ``Sec-WebSocket-Protocol``，如果服务端不选，
    浏览器按 RFC 6455 判定握手失败并关闭连接，前端会陷入 3 秒重连循环
    （界面一直显示"重连中…"）。Python 客户端对此较宽松，所以必须显式断言。
    """
    import asyncio

    from aieb import remote as remote_module

    server = _server()
    monkeypatch.setattr(server, "_validate_binding", lambda: None)
    monkeypatch.setattr(server, "_init_agent", lambda: None)

    async def _noop() -> None:
        return None

    monkeypatch.setattr(server, "_init_mcp", _noop)

    captured: dict[str, object] = {}

    class _Stop(Exception):
        pass

    class _FakeServe:
        def __init__(self, handler, host, port, **kwargs):
            captured.update(kwargs)
            captured["host"] = host
            captured["port"] = port

        async def __aenter__(self):
            raise _Stop

        async def __aexit__(self, *exc_info):
            return False

    monkeypatch.setattr(remote_module.websockets, "serve", _FakeServe)

    with pytest.raises(_Stop):
        asyncio.run(server.run())

    assert captured["subprotocols"] == list(remote_module.ACCEPTED_WS_SUBPROTOCOLS)
    # 新品牌子协议优先宣告，历史品牌子协议仅用于旧页面/旧脚本握手兼容
    assert captured["subprotocols"][0] == remote_module.WS_SUBPROTOCOL == "aieb.v1"
    assert "wendream.v1" in captured["subprotocols"]
