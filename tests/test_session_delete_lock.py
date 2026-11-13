"""会话删除在「文件被占用」时的行为，以及单实例启动锁。

背景：同一工作区如果同时跑两个服务实例，两个进程会各自打开同一份
``.aieb/sessions/<id>.jsonl``。Windows 不允许删除仍被打开的文件（WinError 32），
于是用户看到「点了删除但对话还在」。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from aieb.brain.paths import acquire_server_lock  # noqa: E402
from aieb.memory.session import SessionDeleteError, SessionManager  # noqa: E402


def test_server_lock_rejects_second_instance(tmp_path: Path) -> None:
    first, pid = acquire_server_lock(tmp_path)
    assert first is not None, "第一个实例应该能抢到锁"
    assert pid == str(os.getpid())

    second, holder = acquire_server_lock(tmp_path)
    assert second is None, "同一工作区不允许第二个实例"
    assert holder == str(os.getpid()), "占用者信息要能读回来（方便提示用户）"

    first.close()
    third, _ = acquire_server_lock(tmp_path)
    assert third is not None, "前一个实例退出后锁应能被重新获取"
    third.close()


@pytest.mark.skipif(os.name != "nt", reason="仅 Windows 的删除语义会因句柄占用失败")
def test_delete_removes_meta_even_if_jsonl_locked(tmp_path: Path) -> None:
    manager = SessionManager(str(tmp_path))
    session = manager.create()
    sid = session.session_id
    session.close()

    sessions_dir = tmp_path / ".aieb" / "sessions"
    jsonl = sessions_dir / f"{sid}.jsonl"
    assert jsonl.exists()

    # 模拟「另一个服务实例」正开着这个文件
    holder = open(jsonl, "rb")  # noqa: SIM115
    try:
        with pytest.raises(SessionDeleteError) as exc:
            manager.delete(sid)
        assert "占用" in str(exc.value)
        # 关键：正文删不掉，但元数据必须先没，列表里不能再出现这个会话
        assert not (sessions_dir / f"{sid}.meta").exists()
        assert all(meta.id != sid for meta in manager.list())
    finally:
        holder.close()

    # 占用解除后再删一次应该彻底干净
    assert manager.delete(sid) is True
    assert not jsonl.exists()
