"""AI 外脑 Demo 的运行时路径约定。

所有业务数据（知识库、会议、待办、邮件草稿、PPT 产物）都落在
``<work_dir>/.aieb/brain/`` 下，与原始项目及用户私人目录隔离。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

# 项目内运行时目录名（由 scripts/rename_runtime_dir.py 从历史品牌目录迁移而来）
RUNTIME_DIR_NAME = ".aieb"
BRAIN_DIR_NAME = "brain"


def project_runtime_dir(work_dir: str | os.PathLike[str] | None = None) -> Path:
    """返回项目运行时目录：``<work_dir>/.aieb``。"""
    base = Path(work_dir) if work_dir is not None else Path.cwd()
    return base / RUNTIME_DIR_NAME


def brain_data_dir(work_dir: str | os.PathLike[str] | None = None) -> Path:
    """返回外脑业务数据目录：``<work_dir>/.aieb/brain``。"""
    path = project_runtime_dir(work_dir) / BRAIN_DIR_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def uploads_dir(work_dir: str | os.PathLike[str] | None = None) -> Path:
    path = brain_data_dir(work_dir) / "uploads"
    path.mkdir(parents=True, exist_ok=True)
    return path


def exports_dir(work_dir: str | os.PathLike[str] | None = None) -> Path:
    path = brain_data_dir(work_dir) / "exports"
    path.mkdir(parents=True, exist_ok=True)
    return path


def db_path(work_dir: str | os.PathLike[str] | None = None) -> Path:
    return brain_data_dir(work_dir) / "brain.sqlite3"


def acquire_server_lock(runtime_dir: Path) -> tuple[Any | None, str]:
    """给当前工作区加一把「只允许一个服务实例」的排他文件锁。

    同一工作区跑两个服务实例会互相占用 ``.aieb/sessions/*.jsonl``，
    在 Windows 上表现为"会话删不掉（WinError 32 文件被占用）"。
    因此启动时先抢锁，抢不到就直接退出并告诉用户是哪个进程占着。

    用文件锁而不是 PID 探测：进程退出（含崩溃）时操作系统会自动释放，
    不会留下永远解不开的僵尸锁。

    返回 ``(lock_handle, holder)``：成功时 ``lock_handle`` 非空、``holder``
    是当前 PID；失败时 ``lock_handle`` 为 ``None``、``holder`` 是占用者的
    PID（读不到就是 ``"未知"``）。调用方必须持有返回的句柄直到进程退出。
    """
    runtime_dir.mkdir(parents=True, exist_ok=True)
    path = runtime_dir / "server.lock"
    pid_path = runtime_dir / "server.pid"
    handle = path.open("a+", encoding="utf-8")
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        # 抢锁失败：PID 单独写在 server.pid 里。
        # 不能从 server.lock 里读：Windows 的字节锁会让**同一进程的另一个句柄**
        # 也读不了被锁的那一个字节（ERROR_LOCK_VIOLATION），提示里就会永远
        # 显示「未知」，排障时帮不上忙。
        handle.close()
        holder = "未知"
        try:
            holder = pid_path.read_text(encoding="utf-8").strip() or "未知"
        except OSError:
            pass
        return None, holder
    try:
        pid_path.write_text(str(os.getpid()), encoding="utf-8")
    except OSError:
        # 写 PID 只是给人看的，失败不影响锁本身
        pass
    return handle, str(os.getpid())

