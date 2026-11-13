"""删除会话时的数据清理。

一个会话在磁盘上可能留下这些数据：

* ``.aieb/sessions/<id>.jsonl`` / ``.meta`` —— 消息记录与元数据（含摘要）
* ``.aieb/file-history/<id>/``            —— 本轮文件改动快照（用于 rewind）
* ``.aieb/artifacts/<id>/``               —— 用户产物（含缩略图/副本）
* ``.aieb/memory/*.md``                   —— 由该会话贡献的项目级记忆
* ``~/.aieb/memory/*.md``                 —— 由该会话贡献的用户级长期记忆

记忆的两种处置方式由用户决定（Web 删除确认框里的勾选项）：

* **删除**（默认）：两个目录都做 ``prune_session``——独占的记忆删文件并
  修索引，被其它会话共用的只摘除本会话来源；
* **保留**：做 ``detach_session``——文件不动，但来源元数据改写为
  ``detached``，UI 会显示「来自已删除的对话（你选择保留）」。

无法归属的（全局 tool-results、skills、来源未知的历史记忆）一律不动。
"""

from __future__ import annotations

import logging
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from aieb import artifacts as artifacts_mod
from aieb.memory import provenance

log = logging.getLogger(__name__)

_WIN_RETRIES = 4
_WIN_DELAY = 0.15


@dataclass
class PurgeReport:
    session_id: str
    session_files_removed: int = 0
    file_history_removed: int = 0
    artifact_files_removed: int = 0
    artifacts_removed: list[str] = field(default_factory=list)
    memories_removed: list[str] = field(default_factory=list)
    memories_kept_shared: list[str] = field(default_factory=list)
    memories_kept_detached: list[str] = field(default_factory=list)
    memories_untracked: list[str] = field(default_factory=list)
    memories_detached: list[str] = field(default_factory=list)
    memories_purged: bool = True
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def summary(self) -> str:
        parts = [
            f"会话 {self.session_id} 已删除",
            f"  消息/元数据文件: {self.session_files_removed}",
            f"  文件改动快照: {self.file_history_removed}",
            f"  产物文件: {self.artifact_files_removed}",
        ]
        if self.artifacts_removed:
            parts.append("  产物: " + "、".join(self.artifacts_removed[:6]) + ("…" if len(self.artifacts_removed) > 6 else ""))
        if self.memories_removed:
            parts.append("  专属记忆: " + "、".join(self.memories_removed))
        if self.memories_kept_shared:
            parts.append("  保留的共享记忆: " + "、".join(self.memories_kept_shared))
        if self.memories_detached:
            parts.append("  保留的长期记忆: " + "、".join(self.memories_detached))
        if self.memories_kept_detached:
            parts.append(
                "  保留的已删除对话记忆: " + "、".join(self.memories_kept_detached)
                + "（来源对话已删除，你之前选择了保留）"
            )
        if self.memories_untracked:
            parts.append(
                "  未删除（来源未知的历史记忆）: " + "、".join(self.memories_untracked)
                + "，可在 /memory 中手动删除"
            )
        if self.errors:
            parts.append("  失败: " + "; ".join(self.errors))
        return "\n".join(parts)


def _retry(func, *args, **kwargs) -> bool:
    """Windows 上文件可能被短暂占用，做几次重试。"""
    for attempt in range(_WIN_RETRIES):
        try:
            func(*args, **kwargs)
            return True
        except FileNotFoundError:
            return True
        except OSError as exc:
            if attempt == _WIN_RETRIES - 1:
                log.warning("purge step failed after retries: %s (%s)", func.__name__, exc)
                return False
            time.sleep(_WIN_DELAY * (attempt + 1))
    return False


def _count_files(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(1 for p in path.rglob("*") if p.is_file())


def purge_session(
    session_id: str,
    *,
    work_dir: str | Path,
    session_manager,
    artifact_root: Path | None = None,
    user_memory_dir: Path | None = None,
    delete_untracked_memories: bool = False,
    purge_memories: bool = True,
    session_title: str = "",
) -> PurgeReport:
    """彻底清理一个会话的消息、快照、产物与记忆。

    ``purge_memories`` 是用户在删除确认框里的选择：

    * ``True``（默认）——两个记忆目录都按来源裁剪；
    * ``False`` ——保留记忆文件，只把该会话从来源里摘到 ``detached``。
    """
    report = PurgeReport(session_id=session_id, memories_purged=purge_memories)
    work_path = Path(work_dir)

    # 1) 消息记录 + 元数据（含 session summary）
    try:
        before = session_manager.sessions_dir
        existed = [
            p for p in (
                before / f"{session_id}.jsonl",
                before / f"{session_id}.meta",
            )
            if p.exists()
        ]
        deleted = session_manager.delete(session_id)
        if existed and not deleted:
            report.errors.append("会话记录删除失败（文件可能被占用）")
        report.session_files_removed = len(existed) if deleted else 0
    except Exception as exc:  # noqa: BLE001
        report.errors.append(f"会话记录删除失败: {exc}")

    # 2) 文件改动快照（rewind 用的备份）
    history_dir = work_path / ".aieb" / "file-history" / session_id
    if history_dir.exists():
        count = _count_files(history_dir)
        if _retry(shutil.rmtree, history_dir, ignore_errors=True):
            report.file_history_removed = count
        else:
            report.errors.append("文件改动快照删除失败")

    # 3) 产物（副本 + 索引），删除后旧下载链接自然失效
    try:
        art_root = Path(artifact_root) if artifact_root else work_path / ".aieb" / "artifacts"
        store = artifacts_mod.ArtifactStore(session_id, root=art_root, workspace=work_path)
        items = list(store.payload_items())
        report.artifacts_removed = [item.name for item in items]
        artifact_dir = store.root
        count = _count_files(artifact_dir)
        if artifact_dir.exists() and not _retry(shutil.rmtree, artifact_dir, ignore_errors=True):
            report.errors.append("产物目录删除失败")
        else:
            report.artifact_files_removed = count
    except Exception as exc:  # noqa: BLE001
        report.errors.append(f"产物清理失败: {exc}")

    # 4) 记忆：项目级 + 用户级一起处理（区别只在开关与来源归属）
    project_mem = work_path / ".aieb" / "memory"
    memory_dirs: list[Path] = [project_mem]
    if user_memory_dir is not None and Path(user_memory_dir).is_dir():
        memory_dirs.append(Path(user_memory_dir))
    try:
        # 先落墓碑：即便有并发中的记忆提取，也不会在删除后又把内容写回来
        for directory in memory_dirs:
            provenance.mark_purged(directory, session_id)

        for directory in memory_dirs:
            if not directory.is_dir():
                continue
            if purge_memories:
                result = provenance.prune_session(
                    directory, session_id, delete_untracked=delete_untracked_memories
                )
                report.memories_removed.extend(result.deleted)
                report.memories_kept_shared.extend(result.kept_shared)
                report.memories_kept_detached.extend(result.kept_detached)
                report.memories_untracked.extend(result.untracked)
                report.errors.extend(result.errors)
            else:
                detached = provenance.detach_session(
                    directory, session_id, title=session_title
                )
                report.memories_detached.extend(
                    detached.detached + detached.kept_shared
                )
                report.errors.extend(detached.errors)
    except Exception as exc:  # noqa: BLE001
        report.errors.append(f"记忆清理失败: {exc}")

    return report


def disk_usage(path: str | Path) -> int:
    """统计目录占用（用于清理效果核对）。"""
    root = Path(path)
    if not root.exists():
        return 0
    total = 0
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            try:
                total += (Path(dirpath) / name).stat().st_size
            except OSError:
                continue
    return total
