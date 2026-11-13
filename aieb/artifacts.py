"""用户产物（Artifact）管理：把「要交付给用户的文件」与「工作目录里的中间文件」区分开。

设计要点
--------
* 产物写入独立存储区 ``.aieb/artifacts/<session_id>/<turn>/``，与会话一一对应，
  不污染用户的工作目录，也不受工作目录后续改动影响。
* 前端永远拿不到本地路径，只能拿到服务端生成的 ``art_id``；下载走 ``/file?id=..&sig=..``
  的签名 URL，签名由服务端进程内密钥生成，无法靠猜路径访问。
* 只有「本轮新建、且看起来是交付物」的文件会被收进来：见 :func:`should_deliver`。
  修改已有工作文件、临时脚本、缓存/日志一律不收。
"""

from __future__ import annotations

import json
import mimetypes
import os
import re
import secrets
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

ARTIFACT_ROOT = Path(".aieb") / "artifacts"
MAX_ARTIFACT_BYTES = 50 * 1024 * 1024
MAX_ARTIFACTS_PER_TURN = 20

IMAGE_EXTS = {"png", "jpg", "jpeg", "gif", "webp", "bmp"}
DOCUMENT_EXTS = {"pdf", "docx", "doc", "xlsx", "xls", "pptx", "ppt", "csv", "md", "txt", "json", "zip", "html"}
# 多模态生成的音频/视频：仍然走同一个 ArtifactStore 与签名下载，只是分类更准确
AUDIO_EXTS = {"wav", "mp3", "m4a", "aac", "ogg", "flac", "amr"}
VIDEO_EXTS = {"mp4", "mov", "webm", "mkv", "m4v"}
# 只允许在这些扩展名上 inline 预览；SVG/HTML 一律当附件下载，避免 XSS。
INLINE_SAFE_EXTS = IMAGE_EXTS

_TEMP_NAME_RE = re.compile(
    r"^(?:\.|tmp|temp|~)|(?:\.(?:tmp|temp|log|pyc|pyo|bak|swp|cache|part|lock))$",
    re.IGNORECASE,
)
_TEMP_DIR_MARKERS = {
    "__pycache__", "tmp", "temp", ".cache", ".git", "node_modules",
    ".venv", "venv", ".mypy_cache", ".pytest_cache", ".idea", ".vscode",
    "dist", "build", ".aieb",
}
_UNSAFE_NAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass
class Artifact:
    id: str
    name: str
    size: int
    kind: str  # image | document | file
    ext: str
    turn: int
    stored: str
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "size": self.size,
            "kind": self.kind,
            "ext": self.ext,
            "turn": self.turn,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Artifact | None:
        try:
            return cls(
                id=str(data["id"]),
                name=str(data["name"]),
                size=int(data.get("size", 0)),
                kind=str(data.get("kind", "file")),
                ext=str(data.get("ext", "")),
                turn=int(data.get("turn", 0)),
                stored=str(data["stored"]),
                created_at=str(data.get("created_at", "")),
            )
        except (KeyError, TypeError, ValueError):
            return None


# ---------------------------------------------------------------------------
# 分类规则
# ---------------------------------------------------------------------------


def ext_of(path: str | Path) -> str:
    return Path(path).suffix.lower().lstrip(".")


def kind_of(ext: str) -> str:
    if ext in IMAGE_EXTS:
        return "image"
    if ext in AUDIO_EXTS:
        return "audio"
    if ext in VIDEO_EXTS:
        return "video"
    if ext in DOCUMENT_EXTS:
        return "document"
    return "file"


def is_temp_path(rel: str) -> bool:
    """临时/缓存/隐藏文件判定（只看路径形态，不看是不是"交付物"）。"""
    parts = [p for p in Path(rel).parts if p not in ("", ".")]
    if not parts:
        return True
    for part in parts[:-1]:
        lowered = part.lower()
        if lowered in _TEMP_DIR_MARKERS or lowered.startswith("."):
            return True
    name = parts[-1]
    return bool(_TEMP_NAME_RE.search(name))


def is_mentioned(rel: str, name: str, text: str) -> bool:
    """最终回答里是否点名了这个文件（交付意图最强的信号）。"""
    if not text:
        return False
    if name and name in text:
        return True
    rel_posix = rel.replace("\\", "/")
    return rel_posix in text or rel_posix.replace("/", "\\") in text


def should_deliver(rel: str, ext: str, mentioned: bool) -> bool:
    """是否作为用户产物交付。

    规则（精确优先，宁缺毋滥）：
    1. 临时/缓存/隐藏路径 → 永不交付。
    2. 回答里点名了的文件 → 交付（临时脚本被点名也可以交付）。
    3. 没点名时，只有「文档类/图片类」才兜底交付；源码、脚本、配置等一律不展示，
       因为它们绝大多数是执行过程中的中间产物。
    """
    if is_temp_path(rel):
        return False
    if mentioned:
        return True
    return kind_of(ext) in {"image", "document"}


def safe_filename(name: str, fallback: str = "file") -> str:
    """只保留基名，再去掉路径分隔符/控制字符，限制长度，避免恶意文件名。"""
    base = Path((name or "").replace("\\", "/")).name
    cleaned = _UNSAFE_NAME_RE.sub("_", base).strip().strip(".")
    if not cleaned:
        cleaned = fallback
    return cleaned[:120]


# ---------------------------------------------------------------------------
# 存储
# ---------------------------------------------------------------------------


class ArtifactStore:
    """某个会话的产物存储。一个会话一个目录，索引存 ``index.json``。"""

    def __init__(
        self,
        session_id: str,
        root: Path | None = None,
        workspace: Path | None = None,
    ) -> None:
        safe_session = safe_filename(session_id, fallback="session")
        self.session_id = session_id
        self.root = (Path(root) if root else ARTIFACT_ROOT) / safe_session
        self.workspace = Path(workspace) if workspace else Path.cwd()
        self._items: list[Artifact] = []
        self.load()

    # -- 索引持久化 ------------------------------------------------------
    @property
    def index_path(self) -> Path:
        return self.root / "index.json"

    def load(self) -> None:
        self._items = []
        try:
            data = json.loads(self.index_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        for raw in data.get("artifacts", []):
            item = Artifact.from_dict(raw)
            if item is not None:
                self._items.append(item)

    def save(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        payload = {"session": self.session_id, "artifacts": [self._full(item) for item in self._items]}
        self.index_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def _full(self, item: Artifact) -> dict[str, Any]:
        data = item.to_dict()
        data["stored"] = item.stored
        return data

    # -- 写入 ------------------------------------------------------------
    def add_file(self, src: Path, turn: int, name: str | None = None) -> Artifact | None:
        """把工作目录里的文件复制进产物区；失败或无意义时返回 None。"""
        try:
            src = Path(src)
            if not src.is_file():
                return None
            size = src.stat().st_size
            if size <= 0 or size > MAX_ARTIFACT_BYTES:
                return None
        except OSError:
            return None

        artifact_id = "art_" + secrets.token_hex(6)
        display = safe_filename(name or src.name, fallback="file")
        turn_dir = self.root / str(max(turn, 0))
        try:
            turn_dir.mkdir(parents=True, exist_ok=True)
            stored = turn_dir / f"{artifact_id}__{display}"
            shutil.copy2(src, stored)
        except OSError:
            return None

        ext = ext_of(display)
        item = Artifact(
            id=artifact_id,
            name=display,
            size=size,
            kind=kind_of(ext),
            ext=ext,
            turn=turn,
            stored=str(stored.relative_to(self.root)).replace("\\", "/"),
        )
        self._items.append(item)
        try:
            self.save()
        except OSError:
            pass
        return item

    # -- 查询 ------------------------------------------------------------
    def get(self, artifact_id: str) -> Artifact | None:
        for item in self._items:
            if item.id == artifact_id:
                return item
        return None

    def by_turn(self, turn: int) -> list[Artifact]:
        return [item for item in self._items if item.turn == turn]

    def payload_items(self) -> list[Artifact]:
        """所有产物（供重放时按轮次分组）。"""
        return list(self._items)

    def path_of(self, item: Artifact) -> Path | None:
        """解析产物在磁盘上的真实路径；越界（符号链接/Junction/穿越）返回 None。"""
        try:
            root_real = Path(os.path.realpath(self.root))
            candidate = Path(os.path.realpath(self.root / item.stored))
        except OSError:
            return None
        try:
            candidate.relative_to(root_real)
        except ValueError:
            return None
        return candidate if candidate.is_file() else None

    def delete(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    # -- 给前端的载荷 ----------------------------------------------------
    def payload(self, items: Iterable[Artifact] | None = None) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for item in items if items is not None else self._items:
            data = item.to_dict()
            data["available"] = self.path_of(item) is not None
            data["type_label"] = type_label(item.kind, item.ext)
            data["preview"] = item.ext in INLINE_SAFE_EXTS
            result.append(data)
        return result


def type_label(kind: str, ext: str) -> str:
    if kind == "image":
        return "图片"
    if kind == "audio":
        return "音频"
    if kind == "video":
        return "视频"
    if ext in {"xlsx", "xls", "csv"}:
        return "表格"
    if ext == "pdf":
        return "PDF"
    if ext in {"docx", "doc"}:
        return "文档"
    if ext in {"pptx", "ppt"}:
        return "演示文稿"
    if ext == "zip":
        return "压缩包"
    if ext in {"md", "txt", "json", "html"}:
        return "文本"
    return "文件"


def content_type_for(path: Path, inline: bool) -> tuple[str, str]:
    """返回 (Content-Type, Content-Disposition)。不安全的类型强制下载。"""
    ext = ext_of(path)
    guessed = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    allow_inline = inline and ext in INLINE_SAFE_EXTS
    if not allow_inline:
        return "application/octet-stream", "attachment"
    return guessed, "inline"
