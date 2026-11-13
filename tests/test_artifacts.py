"""用户产物（Artifact）功能测试：交付识别、存储隔离、签名下载、历史恢复。"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from websockets.datastructures import Headers

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from aieb import artifacts  # noqa: E402
from aieb.remote import RemoteServer  # noqa: E402


# ---------------------------------------------------------------- 工具函数


def make_server(workspace: Path, root: Path, session_id: str = "session_test") -> RemoteServer:
    server = RemoteServer(providers=[SimpleNamespace(name="p", protocol="anthropic", model="m")])
    server.agent = SimpleNamespace(work_dir=str(workspace), session_id=session_id)
    server.session_id = session_id
    server.artifact_root = root
    server._stores.clear()
    return server


def publish(server: RemoteServer, before: dict, written: set[str], text: str, turn: int = 1):
    return server._publish_artifacts(
        snapshot_before=before,
        written_paths=written,
        final_text=text,
        turn=turn,
    )


def file_response(server: RemoteServer, url: str):
    """按签名 URL 走一遍 /file 路由。"""
    return server._process_http_request(
        None, SimpleNamespace(path=url, headers=Headers())
    )


# ---------------------------------------------------------------- 1. 单个文件


def test_single_file_produces_card(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    server = make_server(ws, tmp_path / "art")
    before = server._snapshot_files()

    (ws / "杭州旅行攻略.md").write_text("# 攻略\n第一天…", encoding="utf-8")
    cards = publish(server, before, set(), "攻略已经整理好了：杭州旅行攻略.md")

    assert len(cards) == 1
    card = cards[0]
    assert card["name"] == "杭州旅行攻略.md"
    assert card["kind"] == "document"
    assert card["type_label"] == "文本"
    assert card["size"] > 0
    assert card["available"] is True
    assert card["url"].startswith("/file?id=art_")
    # 存储到独立目录，且是复制而不是引用原文件
    stored = list((tmp_path / "art" / "session_test").rglob("*.md"))
    assert len(stored) == 1
    assert stored[0].name.endswith("杭州旅行攻略.md")
    assert stored[0].read_text(encoding="utf-8").startswith("# 攻略")


# ---------------------------------------------------------------- 2. 多个文件


def test_multiple_files_all_delivered(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    server = make_server(ws, tmp_path / "art")
    before = server._snapshot_files()

    (ws / "攻略.pdf").write_bytes(b"%PDF-1.4 fake")
    (ws / "预算.xlsx").write_bytes(b"PK\x03\x04fake")
    (ws / "费用分析.png").write_bytes(b"\x89PNG\r\n\x1a\nfake")
    cards = publish(server, before, set(), "都做好了")

    names = {c["name"] for c in cards}
    assert names == {"攻略.pdf", "预算.xlsx", "费用分析.png"}
    kinds = {c["name"]: c["kind"] for c in cards}
    assert kinds["费用分析.png"] == "image"
    assert all(c["available"] for c in cards)


# ---------------------------------------------------------------- 3. 图片预览


def test_image_gets_preview_url_and_inline_content_type(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    server = make_server(ws, tmp_path / "art")
    before = server._snapshot_files()
    (ws / "chart.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)

    card = publish(server, before, set(), "")[0]
    assert card["preview"] is True
    assert card["preview_url"] and "inline=1" in card["preview_url"]

    inline = file_response(server, card["preview_url"])
    assert inline.status_code == 200
    assert inline.headers["Content-Type"] == "image/png"
    assert inline.headers["Content-Disposition"].startswith("inline")

    download = file_response(server, card["url"])
    assert download.headers["Content-Disposition"].startswith("attachment")


# ---------------------------------------------------------------- 4. 临时脚本


def test_temp_script_is_not_delivered(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    server = make_server(ws, tmp_path / "art")
    before = server._snapshot_files()

    (ws / "gen_chart.py").write_text("import matplotlib\n", encoding="utf-8")
    (ws / "debug.log").write_text("noise\n", encoding="utf-8")
    (ws / "tmp_scratch.txt").write_text("scratch\n", encoding="utf-8")
    cards = publish(server, before, set(), "结果见 chart.png")

    assert cards == []


# ---------------------------------------------------------------- 5. 修改工作文件


def test_modifying_existing_workfile_creates_no_card(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "app.py").write_text("print(1)\n", encoding="utf-8")
    (ws / "README.md").write_text("# old\n", encoding="utf-8")
    server = make_server(ws, tmp_path / "art")
    before = server._snapshot_files()

    (ws / "app.py").write_text("print(2)\n", encoding="utf-8")
    (ws / "README.md").write_text("# new content\n", encoding="utf-8")
    cards = publish(server, before, {str(ws / "app.py"), str(ws / "README.md")}, "改好了")

    assert cards == []


# ---------------------------------------------------------------- 6. 历史会话恢复


def test_store_reloads_index_for_history(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    root = tmp_path / "art"
    server = make_server(ws, root)
    before = server._snapshot_files()
    (ws / "报告.md").write_text("内容", encoding="utf-8")
    publish(server, before, set(), "", turn=2)

    # 模拟重启：新 store 实例从 index.json 读回
    fresh = artifacts.ArtifactStore("session_test", root=root, workspace=ws)
    items = fresh.payload()
    assert [i["name"] for i in items] == ["报告.md"]
    assert items[0]["turn"] == 2

    # 重放时按轮次分组
    server2 = make_server(ws, root)
    grouped = server2._replay_artifacts()
    assert set(grouped) == {2}
    assert grouped[2][0]["available"] is True


# ---------------------------------------------------------------- 7. 文件被删除


def test_deleted_file_is_reported_unavailable(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    root = tmp_path / "art"
    server = make_server(ws, root)
    before = server._snapshot_files()
    (ws / "gone.md").write_text("x", encoding="utf-8")
    card = publish(server, before, set(), "")[0]

    stored = list((root / "session_test").rglob("*.md"))
    assert len(stored) == 1
    stored[0].unlink()

    reloaded = artifacts.ArtifactStore("session_test", root=root, workspace=ws)
    payload = reloaded.payload()
    assert payload[0]["available"] is False

    # 重启后签名密钥换了，前端会拿到新 URL；此时文件已删除 → 404
    restarted = make_server(ws, root)
    url = restarted.artifact_url(payload[0]["id"])
    assert file_response(restarted, url).status_code == 404


# ---------------------------------------------------------------- 8. 越权访问


def test_cross_session_artifact_not_accessible(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    root = tmp_path / "art"

    a = make_server(ws, root, session_id="session_a")
    before = a._snapshot_files()
    (ws / "secret_a.md").write_text("A 的产物", encoding="utf-8")
    card = publish(a, before, set(), "")[0]

    # 另一个会话拿着 A 的 id 去请求：签名对不上（签名含 session_id）→ 403/404
    b = make_server(ws, root, session_id="session_b")
    assert file_response(b, card["url"]).status_code == 403


def test_unsigned_or_tampered_requests_rejected(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    root = tmp_path / "art"
    server = make_server(ws, root)
    before = server._snapshot_files()
    (ws / "a.md").write_text("x", encoding="utf-8")
    card = publish(server, before, set(), "")[0]

    q = parse_qs(urlsplit(card["url"]).query)
    artifact_id = q["id"][0]

    assert file_response(server, "/file").status_code == 400
    assert file_response(server, f"/file?id={artifact_id}").status_code == 400
    tampered = card["url"].replace(q["sig"][0], "0" * len(q["sig"][0]))
    assert file_response(server, tampered).status_code == 403
    # 过期签名
    assert file_response(server, f"/file?id={artifact_id}&exp=1&sig=deadbeef").status_code == 403


def test_path_like_id_is_not_traversed(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "app.py").write_text("print(1)", encoding="utf-8")
    server = make_server(ws, tmp_path / "art")

    for evil in ("../app.py", "..%2Fapp.py", "C:\\Windows\\win.ini", "....//app.py"):
        resp = file_response(server, f"/file?id={evil}&exp=9999999999&sig=deadbeef")
        assert resp.status_code in (403, 404), evil


# ---------------------------------------------------------------- 9. Windows 路径


def test_windows_style_written_paths_are_normalised(tmp_path):
    ws = tmp_path / "ws"
    (ws / "out").mkdir(parents=True)
    server = make_server(ws, tmp_path / "art")
    before = server._snapshot_files()

    target = ws / "out" / "报告.docx"
    target.write_bytes(b"PK\x03\x04doc")
    # 工具参数可能是绝对路径 + 反斜杠（Windows）
    written = {str(target).replace("/", "\\")}
    rel = server._to_relative(next(iter(written)), ws)
    assert rel == "out/报告.docx"

    cards = publish(server, before, written, "")
    assert [c["name"] for c in cards] == ["报告.docx"]


def test_safe_filename_strips_unsafe_characters():
    assert artifacts.safe_filename("../../etc/passwd") == "passwd"
    assert artifacts.safe_filename("a<b>c:d.txt") == "a_b_c_d.txt"
    assert artifacts.safe_filename("") == "file"


# ---------------------------------------------------------------- 10. 分类规则


@pytest.mark.parametrize(
    ("rel", "ext", "mentioned", "expected"),
    [
        ("report.md", "md", False, True),
        ("chart.png", "png", False, True),
        ("data.csv", "csv", False, True),
        ("gen.py", "py", False, False),
        ("gen.py", "py", True, True),
        ("__pycache__/x.pyc", "pyc", True, False),
        ("tmp_scratch.txt", "txt", True, False),
        (".hidden", "", True, False),
    ],
)
def test_should_deliver_rules(rel, ext, mentioned, expected):
    assert artifacts.should_deliver(rel, ext, mentioned) is expected


def test_no_artifacts_no_noise(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    server = make_server(ws, tmp_path / "art")
    before = server._snapshot_files()
    assert publish(server, before, set(), "什么也没做") == []
    assert server._replay_artifacts() == {}


def test_index_file_is_valid_json(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    root = tmp_path / "art"
    server = make_server(ws, root)
    before = server._snapshot_files()
    (ws / "a.md").write_text("x", encoding="utf-8")
    publish(server, before, set(), "")

    data = json.loads((root / "session_test" / "index.json").read_text(encoding="utf-8"))
    assert data["session"] == "session_test"
    assert data["artifacts"][0]["stored"].endswith("a.md")
