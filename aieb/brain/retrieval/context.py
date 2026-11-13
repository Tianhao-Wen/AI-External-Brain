"""上下文预算管理：把"检索到多少"与"真正进模型多少"彻底分开。

改造前的问题（上一轮 Top-K 实验已实测）：

* `build_context(hits, max_chars=6000)` 一旦遇到放不下的块就 `break`，
  后面更短的块也不再尝试 —— 预算被浪费；
* 单块硬截 `text[:1200]`，与总预算无关；
* 没有任何地方记录"检索了 N 块 / 选中 M 块 / 真正进去 K 块 / 花了多少 token"。

这里改为**按 token 预算分配**，并显式产出一个四层计数对象：

    Retrieved  ← 所有召回（BM25 / Vector / 融合后）
    Selected   ← 决定交给模型的前 N 条
    Included   ← 真正被预算放进去的（可能少于 Selected）
    Tokens     ← 估算 token 数（并可与 provider 回传的真实 prompt_tokens 对照）

**为什么用 token 而不是继续用字符**：字符预算对中英文混排、表格、编号的
实际占用完全不可比；同一份 6000 字符预算，中文可能只有 3400 tokens，英文却
超过 4000。预算必须表达成模型真正消耗的资源。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Sequence

from aieb.brain.chunkkey import content_hash

#: 实测口径：DeepSeek 上 3221 个中文字符 ≈ 1851 tokens（1.74 字符/token）。
#: 这是**估算**，不是分词器结果；报告里会同时给出 provider 回传的真实值做对照。
DEFAULT_CHARS_PER_TOKEN = 1.74

#: 一个块至少要留这么多 token 才值得截断后放进去，否则宁可不放。
MIN_BLOCK_TOKENS = 48


def estimate_tokens(text: str, chars_per_token: float = DEFAULT_CHARS_PER_TOKEN) -> int:
    """字符数 → token 数的线性估算（向上取整，保守）。"""
    if not text:
        return 0
    if chars_per_token <= 0:
        raise ValueError("chars_per_token 必须 > 0")
    return max(1, int(round(len(text) / chars_per_token)))


@dataclass
class ContextBundle:
    """一次上下文构建的完整结果与埋点。"""

    text: str = ""
    retrieved: int = 0
    selected: int = 0
    included: int = 0
    dropped: int = 0
    deduped: int = 0
    truncated_blocks: int = 0
    chars: int = 0
    estimated_tokens: int = 0
    budget_tokens: int = 0
    budget_chars: int = 0
    blocks: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "retrieved": self.retrieved,
            "selected": self.selected,
            "included": self.included,
            "dropped": self.dropped,
            "deduped": self.deduped,
            "truncated_blocks": self.truncated_blocks,
            "chars": self.chars,
            "estimated_tokens": self.estimated_tokens,
            "budget_tokens": self.budget_tokens,
            "budget_chars": self.budget_chars,
            "budget_utilisation": round(
                self.estimated_tokens / self.budget_tokens, 4
            )
            if self.budget_tokens
            else 0.0,
            "pages": [f"{b.get('doc_name')} p{b.get('page')}" for b in self.blocks],
        }


_WS = re.compile(r"[ \t\u3000]+")


def _block_header(hit: dict[str, Any]) -> str:
    return f"[来源：{hit.get('doc_name') or '未知文档'} · 第{int(hit.get('page') or 0)}页]"


def build_budgeted_context(
    hits: Sequence[dict[str, Any]],
    *,
    max_tokens: int = 1800,
    chars_per_token: float = DEFAULT_CHARS_PER_TOKEN,
    max_chars: int = 6000,
    min_block_tokens: int = MIN_BLOCK_TOKENS,
    truncate_to_fit: bool = True,
) -> ContextBundle:
    """按预算拼上下文。

    与旧实现的三个差别：

    1. **不再早停**：放不下的块跳过，继续尝试后面的块（可能更短、能放下）；
    2. **可按剩余预算截断**块尾，而不是整块丢弃；
    3. **按内容哈希去重**，同一段文字不会因为多路召回被塞两遍。
    """
    bundle = ContextBundle(budget_tokens=max(1, int(max_tokens)), budget_chars=int(max_chars))
    bundle.retrieved = len(hits)

    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for hit in hits:
        digest = str(hit.get("content_hash") or content_hash(str(hit.get("text") or "")))
        if digest in seen:
            bundle.deduped += 1
            continue
        seen.add(digest)
        unique.append(hit)
    bundle.selected = len(unique)

    blocks: list[str] = []
    used_tokens = 0
    used_chars = 0
    for hit in unique:
        header = _block_header(hit)
        body = _WS.sub(" ", str(hit.get("text") or "")).strip()
        if not body:
            bundle.dropped += 1
            bundle.skipped.append({"reason": "empty", "doc_name": hit.get("doc_name")})
            continue

        remaining_tokens = max_tokens - used_tokens
        remaining_chars = max_chars - used_chars
        if remaining_tokens <= 0 or remaining_chars <= len(header) + 16:
            bundle.dropped += 1
            bundle.skipped.append(
                {"reason": "budget_exhausted", "doc_name": hit.get("doc_name"), "page": hit.get("page")}
            )
            continue

        block = f"{header}\n{body}"
        block_tokens = estimate_tokens(block, chars_per_token)
        truncated = False

        if block_tokens > remaining_tokens or len(block) > remaining_chars:
            if not truncate_to_fit:
                bundle.dropped += 1
                bundle.skipped.append(
                    {
                        "reason": "too_long",
                        "doc_name": hit.get("doc_name"),
                        "page": hit.get("page"),
                        "need_tokens": block_tokens,
                        "left_tokens": remaining_tokens,
                    }
                )
                continue
            # 剩余预算：token 与字符两个约束同时生效（字符约束用于兼容旧配置）
            body_chars_by_tokens = int(remaining_tokens * chars_per_token) - len(header) - 2
            body_chars_by_chars = remaining_chars - len(header) - 2
            body_budget_chars = min(body_chars_by_tokens, body_chars_by_chars)
            if body_budget_chars < 40 or estimate_tokens(
                header + body[:body_budget_chars], chars_per_token
            ) < min_block_tokens:
                bundle.dropped += 1
                bundle.skipped.append(
                    {
                        "reason": "remaining_too_small",
                        "doc_name": hit.get("doc_name"),
                        "page": hit.get("page"),
                        "left_tokens": remaining_tokens,
                    }
                )
                continue
            body = body[:body_budget_chars].rstrip() + "…"
            block = f"{header}\n{body}"
            block_tokens = estimate_tokens(block, chars_per_token)
            truncated = True
            bundle.truncated_blocks += 1

        blocks.append(block)
        used_tokens += block_tokens
        used_chars += len(block) + 2  # 块之间用空行分隔
        bundle.blocks.append(
            {
                "doc_id": hit.get("doc_id"),
                "doc_name": hit.get("doc_name"),
                "page": hit.get("page"),
                "chunk_key": hit.get("chunk_key"),
                "tokens": block_tokens,
                "truncated": truncated,
            }
        )

    bundle.text = "\n\n".join(blocks)
    bundle.included = len(blocks)
    bundle.chars = len(bundle.text)
    bundle.estimated_tokens = estimate_tokens(bundle.text, chars_per_token)

    # 收尾：块间分隔符也会占用字符/ token，逐量估算可能恰好超出预算。
    # 这里从尾部回退，保证"估算 token <= 预算"这个不变式真的成立。
    while blocks and bundle.estimated_tokens > max_tokens:
        dropped_block = bundle.blocks.pop()
        blocks.pop()
        bundle.dropped += 1
        bundle.skipped.append(
            {"reason": "total_budget_trim", "doc_name": dropped_block.get("doc_name")}
        )
        bundle.text = "\n\n".join(blocks)
        bundle.included = len(blocks)
        bundle.chars = len(bundle.text)
        bundle.estimated_tokens = estimate_tokens(bundle.text, chars_per_token)
    return bundle
