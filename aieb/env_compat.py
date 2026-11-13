"""环境变量兼容层。

历史沿革：本项目的前身（WenDream 时期）使用 ``WENDREAM_`` 前缀的环境变量。
品牌统一为「AI 外脑 Demo」后，新前缀为 ``AIEB_``。为了避免破坏仍在使用的部署脚本、
CI 变量和用户本地环境，这里做**只读映射**：

* 优先读取 ``AIEB_<SUFFIX>``；
* 未设置时回退到 ``WENDREAM_<SUFFIX>``（历史别名，仍然有效）；
* 绝不修改 ``os.environ``，也绝不打印变量值（可能含密钥）。
"""

from __future__ import annotations

import os

PREFIX = "AIEB_"
LEGACY_PREFIX = "WENDREAM_"


def names(suffix: str) -> tuple[str, str]:
    """返回 ``(新名, 历史名)`` 两个候选环境变量名。"""
    return f"{PREFIX}{suffix}", f"{LEGACY_PREFIX}{suffix}"


def get(suffix: str, default: str | None = None) -> str | None:
    """按「新名 → 历史名 → 默认值」顺序读取环境变量。"""
    new_name, legacy_name = names(suffix)
    value = os.environ.get(new_name)
    if value not in (None, ""):
        return value
    value = os.environ.get(legacy_name)
    if value not in (None, ""):
        return value
    return default


def is_set(suffix: str) -> bool:
    return get(suffix) is not None
