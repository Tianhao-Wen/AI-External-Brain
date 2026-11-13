"""稳定的内容标识：chunk / 文档的确定性哈希。

为什么要单独一个模块：现有 `chunks.id` 是插入时随机生成的 `chk_xxx`，
**同一份文件重复上传会得到全新主键**，所以它不能作为向量库的幂等键。
这里提供三样东西：

* :func:`content_hash`    —— chunk 内容哈希（决定"是不是同一段文字"）
* :func:`document_key`    —— 原始文件字节哈希（决定"是不是同一份文件"）
* :func:`chunk_key`       —— 二者的组合，作为向量点的逻辑主键

三者都是纯函数、无副作用、不依赖 store，所以 store / 检索 / 向量库都能安全引用。
"""

from __future__ import annotations

import hashlib
import unicodedata


def normalize_for_hash(text: str) -> str:
    """用于内容哈希的标准化：只做无歧义的等价化，不改变语义。"""
    text = unicodedata.normalize("NFKC", text or "")
    lines = [line.strip() for line in text.replace("\r\n", "\n").split("\n")]
    return "\n".join(line for line in lines if line)


def content_hash(text: str) -> str:
    """chunk 内容哈希（16 位十六进制）。"""
    return hashlib.sha256(normalize_for_hash(text).encode("utf-8")).hexdigest()[:16]


def document_key(data: bytes) -> str:
    """原始文件字节哈希（16 位十六进制）：同一份文件必然同键。"""
    return hashlib.sha256(data or b"").hexdigest()[:16]


def chunk_key(doc_key: str, chunk_hash: str, page: int) -> str:
    """向量点的逻辑主键。

    包含页号是必要的：同一段文字重复出现在不同页时，引用页码不同，
    不能被去重成一条。
    """
    return f"{doc_key}:{int(page)}:{chunk_hash}"
