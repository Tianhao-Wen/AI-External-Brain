"""知识库：PDF/TXT 解析、分块、BM25 检索与带页码的引用。

刻意保持轻量：不引入向量数据库，用字符 n-gram + BM25 做中文友好的关键词检索，
并如实标注引用来源（文档名 + 页码）。扫描件没有可用 OCR 时返回
``status='no_text'``，由界面明确告知"文本无法解析"，而不是假装解析成功。
"""

from __future__ import annotations

import math
import re
import threading
import unicodedata
import uuid
from array import array
from collections import Counter
from pathlib import Path
from typing import Any

from aieb.brain.chunkkey import chunk_key, content_hash, document_key
from aieb.brain.store import BrainStore

CHUNK_TARGET_CHARS = 700
CHUNK_OVERLAP_CHARS = 120
MAX_PDF_BYTES = 40 * 1024 * 1024
MAX_TEXT_BYTES = 8 * 1024 * 1024
# 平均每页少于这么多字符就认为"没有可解析文本层"（扫描件）
MIN_CHARS_PER_PAGE = 24

_ASCII_WORD_RE = re.compile(r"[a-zA-Z0-9_]+")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")

STOPWORDS = {
    "的", "了", "和", "是", "在", "我们", "他们", "这个", "那个", "以及", "并且",
    "对于", "关于", "一个", "可以", "需要", "进行", "通过", "the", "and", "for",
    "with", "that", "this", "are", "was", "from", "you", "not",
}


def normalize_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "")
    # 统一换行，压掉多余空行，保留段落结构
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t\u3000]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def tokenize(text: str) -> list[str]:
    """中文用字符 bigram + 单字，英文用单词。

    这样既不需要 jieba 之类的分词依赖，又能让 BM25 在中文查询上拿到可用召回。
    """
    text = normalize_text(text).lower()
    tokens: list[str] = [w for w in _ASCII_WORD_RE.findall(text) if w not in STOPWORDS]
    for seq in re.findall(r"[\u4e00-\u9fff]+", text):
        if len(seq) == 1:
            tokens.append(seq)
            continue
        tokens.extend(seq[i:i + 2] for i in range(len(seq) - 1))
        tokens.extend(ch for ch in seq)
    return [t for t in tokens if t.strip()]


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------


class DocumentParseError(RuntimeError):
    """文档无法解析（格式错误、损坏、加密等）。"""


def extract_pdf(data: bytes) -> dict[str, Any]:
    """提取 PDF 每页文本。返回 {pages:[{page,text}], chars, page_count, has_text}。"""
    try:
        # PyMuPDF >= 1.24.1 推荐 `import pymupdf`；旧版只提供 `fitz`，
        # 而 2.x 起 `import fitz` 会发 FutureWarning。两者都兼容。
        try:
            import pymupdf as fitz
        except ImportError:  # pragma: no cover - 老版本 PyMuPDF
            import fitz  # type: ignore[no-redef]  # PyMuPDF
    except ImportError as exc:  # pragma: no cover - 依赖缺失时明确报错
        raise DocumentParseError("服务端未安装 PDF 解析组件（PyMuPDF）") from exc

    try:
        with fitz.open(stream=data, filetype="pdf") as doc:
            if doc.needs_pass:
                raise DocumentParseError("PDF 已加密，无法读取内容")
            pages: list[dict[str, Any]] = []
            for index, page in enumerate(doc, start=1):
                text = normalize_text(page.get_text("text") or "")
                pages.append({"page": index, "text": text})
            page_count = doc.page_count
    except DocumentParseError:
        raise
    except Exception as exc:  # noqa: BLE001 - 损坏文件必须转成可读错误
        raise DocumentParseError(f"PDF 解析失败：{exc}") from exc

    chars = sum(len(p["text"]) for p in pages)
    has_text = page_count > 0 and chars >= MIN_CHARS_PER_PAGE * page_count
    return {"pages": pages, "chars": chars, "page_count": page_count, "has_text": has_text}


def decode_text(data: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "big5"):
        try:
            text = data.decode(encoding)
        except UnicodeDecodeError:
            continue
        if "\ufffd" not in text:
            return text
    return data.decode("utf-8", errors="replace")


_W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def extract_docx(data: bytes) -> str:
    """抽取 .docx 正文。

    刻意不引入 python-docx：docx 本质是 zip + XML，直接读 ``word/document.xml``
    就够用，也避免为一个格式增加运行期依赖。段落（``w:p``）与表格单元格里的
    文字都在 ``w:t`` 中，按段落拼接即可，表格内容不会丢。
    """
    import io
    import zipfile
    from xml.etree import ElementTree as ET

    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            if "word/document.xml" not in zf.namelist():
                raise DocumentParseError("这个 .docx 结构异常（缺少 word/document.xml）")
            xml = zf.read("word/document.xml")
    except DocumentParseError:
        raise
    except zipfile.BadZipFile as exc:
        raise DocumentParseError(
            "这个文件不是有效的 .docx；如果是旧版 .doc，请另存为 .docx 再上传"
        ) from exc
    except OSError as exc:
        raise DocumentParseError(f"读取 .docx 失败：{exc}") from exc

    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise DocumentParseError("这个 .docx 内容无法解析") from exc

    lines: list[str] = []
    for paragraph in root.iter(f"{_W_NS}p"):
        lines.append("".join(node.text or "" for node in paragraph.iter(f"{_W_NS}t")))
    return "\n".join(lines).strip()


def extract_document_text(filename: str, data: bytes) -> str:
    """把上传文件转成纯文本（会议分析 / 知识库入库共用一套格式支持）。

    支持 PDF、TXT、MD、CSV、DOCX。其它格式统一抛 ``DocumentParseError``，
    由调用方转成用户能看懂的提示。
    """
    ext = Path(filename).suffix.lower().lstrip(".")
    if ext in {"txt", "md", "csv"}:
        return normalize_text(decode_text(data))
    if ext == "docx":
        return normalize_text(extract_docx(data))
    if ext == "pdf":
        parsed = extract_pdf(data)
        if not parsed["has_text"]:
            raise DocumentParseError(
                "未检测到可提取的文字（可能是扫描件/图片型 PDF），当前未启用 OCR"
            )
        return normalize_text(
            "\n\n".join(p["text"] for p in parsed["pages"] if p.get("text"))
        )
    raise DocumentParseError(
        f"暂不支持 .{ext or '未知'} 文件，请上传 PDF / TXT / MD / DOCX"
    )


def split_pages(text: str, page_count: int = 1) -> list[dict[str, Any]]:
    """纯文本按页分隔符切分；没有分隔符时按长度均分成若干"页"。

    纯文本没有真实页码，这里用"第N段"占位（page 从 1 开始），
    回答时引用为"第N段"，不谎称是 PDF 页码。
    """
    parts = [p for p in re.split(r"\f|\n-{3,}\s*第?\s*\d+\s*页\s*-{3,}\n", text) if p.strip()]
    if len(parts) <= 1:
        return [{"page": 1, "text": normalize_text(text)}]
    return [{"page": i, "text": normalize_text(p)} for i, p in enumerate(parts, start=1)]


def chunk_pages(
    pages: list[dict[str, Any]],
    target: int = CHUNK_TARGET_CHARS,
    overlap: int = CHUNK_OVERLAP_CHARS,
) -> list[dict[str, Any]]:
    """按页分块：同一块内不跨页，保证引用页码准确。"""
    chunks: list[dict[str, Any]] = []
    idx = 0
    for page in pages:
        text = page["text"].strip()
        if not text:
            continue
        start = 0
        length = len(text)
        while start < length:
            end = min(start + target, length)
            piece = text[start:end]
            # 尽量在句末切分，避免把一句话劈开
            if end < length:
                cut = max(piece.rfind("。"), piece.rfind("！"), piece.rfind("？"), piece.rfind("\n"))
                if cut >= target // 2:
                    piece = piece[: cut + 1]
                    end = start + len(piece)
            piece = piece.strip()
            if piece:
                chunks.append(
                    {
                        "page": page["page"],
                        "idx": idx,
                        "text": piece,
                        # 内容哈希在入库时就算好：向量索引、去重、缓存都要用，
                        # 后续再算会在大规模重建时重复扫文本。
                        "content_hash": content_hash(piece),
                    }
                )
                idx += 1
            if end >= length:
                break
            # 重叠是为了让跨块的句子仍可被检索命中；必须保证每次前进至少半个窗口，
            # 否则末尾短块会退化成逐字滑动，把同一段文字重复几十遍。
            start = max(start + 1, end - overlap, end - target // 2)
    return chunks


def ingest(
    store: BrainStore,
    *,
    filename: str,
    data: bytes,
    upload_dir: Path,
    scope_id: str = "default",
) -> dict[str, Any]:
    """把上传文件解析入库。返回文档记录（含 status/error）。"""
    ext = Path(filename).suffix.lower().lstrip(".")
    size = len(data)
    safe_name = Path(filename).name

    # 不支持的格式在落库前就拒掉：否则文档列表会堆满"解析失败"的无意义条目
    if ext not in {"pdf", "txt", "md", "csv", "docx"}:
        raise DocumentParseError(f"暂不支持 .{ext or '未知'} 文件，请上传 PDF / TXT / MD / DOCX")

    # 重复上传去重：按**文件内容哈希**而不是文件名。
    # 不这么做的话，反复上传同一份 PDF 会不断新增文档记录与向量，
    # 而 chunk 主键是随机的，外部索引无法靠它做幂等。
    doc_key = document_key(data)
    existing = store.find_document_by_key(doc_key)
    if existing is not None:
        return {**existing, "reused": True}

    upload_dir.mkdir(parents=True, exist_ok=True)
    stored = upload_dir / f"{uuid.uuid4().hex[:12]}_{safe_name}"
    try:
        stored.write_bytes(data)
    except OSError as exc:
        raise DocumentParseError(f"保存上传文件失败：{exc}") from exc

    doc = store.add_document(
        name=safe_name,
        ext=ext,
        size=size,
        stored_path=str(stored),
        status="parsing",
        document_key=doc_key,
        scope_id=scope_id,
    )
    doc_id = doc["id"]

    try:
        if ext == "pdf":
            if size > MAX_PDF_BYTES:
                raise DocumentParseError("PDF 超过 40MB，请拆分后再上传")
            parsed = extract_pdf(data)
            if not parsed["has_text"]:
                store.update_document(
                    doc_id,
                    status="no_text",
                    error="未检测到可提取的文字（可能是扫描件/图片型 PDF），"
                          "当前未启用 OCR，无法解析内容",
                    pages=parsed["page_count"],
                )
                return store.get_document(doc_id) or doc
            pages = parsed["pages"]
            page_count = parsed["page_count"]
        elif ext in {"txt", "md", "csv", "docx"}:
            if size > MAX_TEXT_BYTES:
                raise DocumentParseError("文本文件超过 8MB，请拆分后再上传")
            raw_text = extract_docx(data) if ext == "docx" else decode_text(data)
            text = normalize_text(raw_text)
            if not text:
                raise DocumentParseError("文件内容为空")
            pages = split_pages(text)
            page_count = len(pages)
        else:  # pragma: no cover - 已在入口处拦截
            raise DocumentParseError(f"暂不支持 .{ext} 文件，请上传 PDF / TXT / MD / DOCX")

        chunks = chunk_pages(pages)
        if not chunks:
            raise DocumentParseError("未解析到有效文本内容")
        store.add_chunks(doc_id, chunks)
        chars = sum(len(c["text"]) for c in chunks)
        store.update_document(
            doc_id,
            status="ready",
            pages=page_count,
            chars=chars,
            error="",
        )
        store.add_activity(
            "document",
            f"上传知识文档：{safe_name}",
            f"{page_count} 页 / {len(chunks)} 个文本块",
        )
        return store.get_document(doc_id) or doc
    except DocumentParseError as exc:
        store.update_document(doc_id, status="failed", error=str(exc))
        return store.get_document(doc_id) or doc
    except Exception as exc:  # noqa: BLE001 - 任何解析异常都要落库成可读状态
        store.update_document(doc_id, status="failed", error=f"解析失败：{exc}")
        return store.get_document(doc_id) or doc


def delete_document(store: BrainStore, doc_id: str) -> bool:
    """删除文档：数据库记录 + 分块（级联）+ 原始文件 + 关联活动记录。

    活动记录按标题精确匹配清理，且仅在**同名文档全部删完**时才清理，
    避免用户上传了多份同名文件时把仍然存在的文档的活动记录误删。
    这样删除之后，「最新活动」里不会继续残留已删除文档的痕迹。
    """
    doc = store.get_document(doc_id)
    if doc is None:
        return False
    stored = Path(doc.get("stored_path") or "")
    ok = store.delete_document(doc_id)
    if ok:
        try:
            if stored.is_file():
                stored.unlink()
        except OSError:
            pass
        name = doc.get("name") or ""
        if name and not any(d.get("name") == name for d in store.list_documents()):
            store.delete_activities_by_titles([
                f"上传知识文档：{name}",
                f"删除知识文档：{name}",
            ])
    return ok


# ---------------------------------------------------------------------------
# 检索（BM25）
# ---------------------------------------------------------------------------


class BM25Index:
    """基于分块的内存 BM25 索引。

    写入策略：

    * 首次使用 / 删除文档 → :meth:`rebuild`（全量）；
    * 新文档入库 → :meth:`add_chunks`（增量），避免批量入库退化成 O(N²)。

    检索策略（这次改造的重点）：

    1. **idf 提出循环**：原实现把 `math.log` 放在 (doc, token) 双层循环里，
       75k 规模下单次查询要算 110 万次对数，而 idf 只与 token 有关。
    2. **倒排表 + 定长数组**：`postings[token]` 与 `post_tf[token]` 用 `array('i')` 存，
       比 `list[int]`（每个 int 都是 28 字节对象）省 7~9 倍内存。
    3. **向量化打分**（numpy 可用时）：按 token 遍历 postings，把整列候选的
       贡献一次性加到分数数组上。

    打分公式、idf、平均长度、并列时的排序规则（`(score, index)` 降序）
    与旧实现**完全一致**：同一个 doc 的累加顺序不变，每项仍按
    `((idf*tf)*(K1+1))/denom` 计算。等价性由 `tests/test_bm25_equivalence.py` 逐条比对。
    """

    K1 = 1.5
    B = 0.75

    def __init__(
        self, store: BrainStore, *, use_inverted: bool = True, use_numpy: bool = True
    ) -> None:
        self.store = store
        self.use_inverted = use_inverted
        self.use_numpy = use_numpy
        self._numpy_ready = False
        if use_numpy:
            try:  # numpy 是可选加速项：没有它也要能跑（退回纯 Python）
                import numpy  # noqa: F401

                self._numpy_ready = True
            except ImportError:  # pragma: no cover - 精简部署
                self._numpy_ready = False
        self._docs: list[dict[str, Any]] = []
        #: 并发保护：重建 / 增量追加 / 打分共用一把可重入锁。
        #: 不加锁时，“删除触发 rebuild” 与 “上传触发 add_chunks” 并发会让
        #: `_docs` 与 `_lengths`/postings 长度错位，打分直接越界或返回脏结果。
        self._lock = threading.RLock()
        #: 仅在**没有 numpy** 时填充（纯 Python 回退需要按 doc 查 tf）
        self._tf: list[dict[str, int]] = []
        self._lengths: Any = array("d")
        self._length_arr: Any = array("d")
        self._postings: dict[str, Any] = {}
        self._post_tf: dict[str, Any] = {}
        self._doc_indexes: dict[str, list[int]] = {}
        self._df: Counter[str] = Counter()
        self._total_len: int = 0
        self._avg_len: float = 0.0
        self._built = False

    # -- 建索引 -------------------------------------------------------

    def rebuild(self) -> None:
        with self._lock:
            self._docs = self.store.all_chunks()
            self._reset()
            pending = list(self._docs)
            self._docs = []
            for doc in pending:
                self._append(doc)
            self._refresh_avg_len()
            self._built = True

    def _reset(self) -> None:
        self._tf = []
        self._lengths = array("d")
        self._length_arr = self._lengths
        self._postings = {}
        self._post_tf = {}
        self._doc_indexes = {}
        self._df = Counter()
        self._total_len = 0
        self._avg_len = 0.0

    def _append(self, chunk: dict[str, Any]) -> int:
        """把一条分块写进内存索引（rebuild 与 add_chunks 共用同一套逻辑）。

        返回新分块的下标。
        """
        tokens = tokenize(chunk["text"])
        tf: dict[str, int] = {}
        for token in tokens:
            tf[token] = tf.get(token, 0) + 1
        index = len(self._docs)
        self._docs.append(
            {
                "id": chunk.get("id", ""),
                "doc_id": chunk["doc_id"],
                "doc_name": chunk.get("doc_name", ""),
                "page": int(chunk.get("page", 0)),
                "idx": int(chunk.get("idx", 0)),
                "text": chunk["text"],
                "content_hash": chunk.get("content_hash") or content_hash(chunk["text"]),
                "document_key": chunk.get("document_key", ""),
            }
        )
        if not self._numpy_ready:
            self._tf.append(tf)
        self._lengths.append(float(len(tokens)))
        for token, freq in tf.items():
            postings = self._postings.get(token)
            if postings is None:
                postings = self._postings[token] = array("i")
                self._post_tf[token] = array("i")
            postings.append(index)
            self._post_tf[token].append(freq)
            self._df[token] += 1
        self._doc_indexes.setdefault(chunk["doc_id"], []).append(index)
        self._total_len += len(tokens)
        return index

    def _refresh_avg_len(self) -> None:
        self._avg_len = self._total_len / len(self._docs) if self._docs else 0.0

    def add_chunks(self, chunks: list[dict[str, Any]]) -> int:
        """增量追加分块（不重算已有部分）。返回新增条数。"""
        with self._lock:
            if not self._built:
                self.rebuild()
                return 0
            added = 0
            for chunk in chunks:
                self._append(chunk)
                added += 1
            if added:
                self._refresh_avg_len()
            return added

    def invalidate(self) -> None:
        """标记索引失效，下次检索前全量重建（用于删除文档等难以增量的场景）。"""
        with self._lock:
            self._built = False

    def ensure(self) -> None:
        with self._lock:
            if not self._built:
                self.rebuild()

    @property
    def size(self) -> int:
        self.ensure()
        with self._lock:
            return len(self._docs)

    # -- 检索 ---------------------------------------------------------

    def _candidate_indexes(self, tokens: list[str], doc_ids: set[str] | None) -> list[int]:
        """命中至少一个 query token 的分块下标（纯 Python 回退路径使用）。"""
        candidates: set[int] = set()
        for token in tokens:
            listing = self._postings.get(token)
            if listing:
                candidates.update(listing)
        if doc_ids:
            allowed: set[int] = set()
            for doc_id in doc_ids:
                allowed.update(self._doc_indexes.get(doc_id, ()))
            candidates &= allowed
        return list(candidates)

    def _allowed_mask(self, doc_ids: set[str] | None):
        """把 doc_ids 白名单转成布尔掩码（numpy 路径用；None 表示不限制）。"""
        import numpy as np

        if not doc_ids:
            return None
        mask = np.zeros(len(self._docs), dtype=bool)
        for doc_id in doc_ids:
            for index in self._doc_indexes.get(doc_id, ()):
                mask[index] = True
        return mask

    def _idf(self, token: str, total: int) -> float:
        df = self._df.get(token, 0) or 1
        return math.log(1 + (total - df + 0.5) / (df + 0.5))

    def _search_python(self, tokens: list[str], top_k: int, doc_ids: set[str] | None) -> list[tuple[float, int]]:
        """纯 Python 打分（numpy 不可用时的回退路径）。

        与旧实现的差别只有两处，且都不改变数值：

        1. idf 按 token 预先算好（旧实现把它放在 (doc, token) 双层循环里，
           75k 规模下单次查询要算 110 万次对数）；
        2. 遍历顺序改成 token 外层、postings 内层 —— 同一个 doc 收到的
           加法次序仍然是「按 query token 出现顺序」，与旧实现一致。

        返回 `[(score, index), ...]`，已按分数降序、下标降序排列。
        """
        total = len(self._docs)
        lengths = self._lengths
        avg = self._avg_len or 1
        allowed: set[int] | None = None
        if doc_ids:
            allowed = set()
            for doc_id in doc_ids:
                allowed.update(self._doc_indexes.get(doc_id, ()))

        scores: dict[int, float] = {}
        for token in tokens:
            postings = self._postings.get(token)
            if not postings:
                continue
            idf = self._idf(token, total)
            tfs = self._post_tf[token]
            for position, index in enumerate(postings):
                if allowed is not None and index not in allowed:
                    continue
                freq = tfs[position]
                length = lengths[index] or 1
                denom = freq + self.K1 * (1 - self.B + self.B * length / avg)
                scores[index] = scores.get(index, 0.0) + idf * freq * (self.K1 + 1) / denom

        scored = [(score, index) for index, score in scores.items() if score > 0]
        scored.sort(reverse=True)
        return scored

    def _search_numpy(self, tokens: list[str], top_k: int, doc_ids: set[str] | None):
        """向量化打分。

        为什么结果与纯 Python 完全一致：

        * 每个 doc 的累加**顺序**仍是"按 query token 出现顺序逐个相加"——
          只是把外层从 doc 换成 token，同一个 doc 收到的加法次序不变；
        * 每一项仍按 `((idf*tf)*(K1+1))/denom` 的顺序计算，numpy 不会插入 FMA；
        * idf 仍用 `math.log` 标量计算（只是提到循环外）。

        因此它不是"近似优化"，而是同一公式的等价向量化实现（见 tests/test_bm25_equivalence.py）。
        """
        import numpy as np

        from array import array

        total = len(self._docs)
        scores = np.zeros(total, dtype=np.float64)
        lengths = np.frombuffer(self._length_arr, dtype=np.float64)
        mask = self._allowed_mask(doc_ids)
        avg = self._avg_len or 1

        for token in tokens:
            postings = self._postings.get(token)
            if not postings:
                continue
            idx = np.frombuffer(postings, dtype=np.int32) if isinstance(postings, array) else np.asarray(postings, dtype=np.int32)
            tfs = self._post_tf[token]
            tf_arr = (
                np.frombuffer(tfs, dtype=np.int32)
                if isinstance(tfs, array)
                else np.asarray(tfs, dtype=np.int32)
            ).astype(np.float64)
            if mask is not None:
                keep = mask[idx]
                if not keep.any():
                    continue
                idx = idx[keep]
                tf_arr = tf_arr[keep]
            idf = self._idf(token, total)
            denom = tf_arr + self.K1 * (1 - self.B + self.B * lengths[idx] / avg)
            # 与标量版的求值顺序保持一致：((idf * tf) * (K1 + 1)) / denom
            scores[idx] += (idf * tf_arr) * (self.K1 + 1) / denom

        nonzero = np.flatnonzero(scores)
        if nonzero.size == 0:
            return []
        order = np.lexsort((-nonzero, -scores[nonzero]))[:top_k]
        picked = nonzero[order]
        return [(float(scores[i]), int(i)) for i in picked]

    def search(self, query: str, top_k: int = 5, doc_ids: set[str] | None = None) -> list[dict[str, Any]]:
        self.ensure()
        tokens = tokenize(query)
        if not tokens or not self._docs:
            return []
        scored = self._score(tokens, top_k, doc_ids)
        hits: list[dict[str, Any]] = []
        for score, i in scored[:top_k]:
            item = self._docs[i]
            hits.append({
                "doc_id": item["doc_id"],
                "doc_name": item["doc_name"],
                "page": item["page"],
                "text": item["text"],
                "score": round(score, 3),
                # 稳定的 chunk 标识：混合检索用它做跨路去重，向量库用它做幂等键。
                # 老数据（document_key 为空）会退化成 doc_id 派生，仍保持可去重。
                "chunk_key": chunk_key(
                    item.get("document_key") or item["doc_id"],
                    item.get("content_hash") or content_hash(item["text"]),
                    int(item["page"]),
                ),
                "content_hash": item.get("content_hash") or content_hash(item["text"]),
                "source": "bm25",
            })
        return hits

    def _score(self, tokens: list[str], top_k: int, doc_ids: set[str] | None):
        """返回 `[(score, index), ...]`，已按分数降序、下标降序，长度不超过 top_k。"""
        with self._lock:
            if self.use_numpy and self._numpy_ready:
                scored = self._search_numpy(tokens, top_k, doc_ids)
            else:
                scored = self._search_python(tokens, top_k, doc_ids)
            return scored[:top_k]


def format_citations(hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把检索结果整理成前端引用卡片。

    除文档与页码外保留 `chunk_key`（chunk_id），便于把界面上的引用
    对应回具体分块；旧调用方只用 doc_id/doc_name/page，新增字段是兼容的。
    """
    return [
        {
            "doc_id": h["doc_id"],
            "doc_name": h["doc_name"],
            "page": h["page"],
            "chunk_key": h.get("chunk_key", ""),
            "excerpt": h["text"][:220],
            "score": h["score"],
        }
        for h in hits
    ]


def build_context(hits: list[dict[str, Any]], max_chars: int = 6000) -> str:
    """把检索到的分块拼成给模型看的上下文，带明确来源标记。"""
    lines: list[str] = []
    used = 0
    for hit in hits:
        header = f"[来源：{hit['doc_name']} · 第{hit['page']}页]"
        snippet = hit["text"][:1200]
        block = f"{header}\n{snippet}"
        if used + len(block) > max_chars:
            break
        lines.append(block)
        used += len(block)
    return "\n\n".join(lines)


# ---------------------------------------------------------------------------
# 引用校验
# ---------------------------------------------------------------------------

#: 与 ``benchmarks/evaluators/rag.py`` 的 CITATION_RE 保持一致：
#: 评测按这个格式解析引用，产品也按这个格式校验引用。
CITATION_MARK_RE = re.compile(r"\[来源[:：]\s*(?P<name>[^·・|\]]+?)\s*[·・|]\s*第\s*(?P<page>\d+)\s*页\]")

_DIGIT_TOKEN = re.compile(r"\d+(?:\.\d+)?")
_SQUASH_RE = re.compile(r"[\s\u3000，。、；：！？,.;:!?\"'“”‘’（）()\-—_~·…%]+")


def _squash(text: str) -> str:
    return _SQUASH_RE.sub("", text or "").lower()


def _bigrams(text: str) -> set[str]:
    body = _squash(text)
    return {body[i : i + 2] for i in range(len(body) - 1)} if len(body) > 1 else set()


def _support_score(sentence: str, page_text: str) -> float:
    """一句话与某页正文的支撑度：数字命中优先，其次字符二元组重合率。"""
    words = _DIGIT_TOKEN.findall(sentence)
    if words and all(w in page_text for w in words):
        return 1.0
    a, b = _bigrams(sentence), _bigrams(page_text)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a)


def citation_pruning_enabled() -> bool:
    """引用剪枝是否开启（默认关闭，实测会误删正确引用）。"""
    import os

    return os.environ.get("AIEB_CITATION_PRUNE", "").strip().lower() in {"1", "on", "true", "yes"}


def prune_citations(
    answer: str,
    hits: list[dict[str, Any]],
    *,
    min_support: float = 0.10,
) -> tuple[str, list[dict[str, Any]]]:
    """校验回答里的引用标记，删掉"该页其实支撑不了这句话"的标记。

    **默认不启用**（见 :func:`citation_pruning_enabled`）。Benchmark 实测：
    在开发集与已暴露测试集上，开启后引用准确率反而下降 1一4 个百分点（正确引用
    被误删多于错误引用被删），因此产品默认关闭，只在需要诊断时用
    ``AIEB_CITATION_PRUNE=1`` 打开。实现与单测保留，方便后续把支撑度判定做准
    之后再启用。

    只校验"引用是否存在依据"，**不判断答案本身对不对**——那是另一件事。
    """
    if not answer:
        return answer, []

    page_text = {(h["doc_name"], int(h["page"])): str(h.get("text") or "") for h in hits}
    # 键必须带上页码：同名文档的不同页不能互相顶掉（早先只按文档名建索引，
    # 导致所有标记都去比对同一页，校验结果完全失真）。
    normalized = {(_squash(name), page): (name, page) for (name, page) in page_text}

    pieces: list[str] = []
    kept_meta: list[dict[str, Any]] = []
    dropped = 0
    cursor = 0
    for match in CITATION_MARK_RE.finditer(answer):
        prefix = answer[cursor : match.start()]
        pieces.append(prefix)
        cursor = match.end()

        sentence = prefix
        for sep in ("。", "；", "\n", "！", "？"):
            sentence = sentence.split(sep)[-1]

        name = match.group("name").strip()
        page = int(match.group("page"))
        key = normalized.get((_squash(name), page), (name, page))
        text = page_text.get(key)
        if text is None:
            # 页码不在检索结果里：可能是模型自己算出来的合理引用，保留但不计支撑度
            pieces.append(match.group(0))
            kept_meta.append({"doc_name": name, "page": page, "support": None})
            continue
        score = _support_score(sentence, text)
        if score >= min_support:
            pieces.append(match.group(0))
            kept_meta.append({"doc_name": key[0], "page": page, "support": round(score, 3)})
        else:
            dropped += 1
    pieces.append(answer[cursor:])

    if dropped and not kept_meta:
        # 全被判无依据：宁可多标也不制造没有出处的答案
        return answer, []
    return "".join(pieces), kept_meta
