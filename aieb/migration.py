"""一次性 legacy 迁移：旧运行时目录 `.mewcode/` → 新目录 `.aieb/`。

本模块是整个代码库中**唯一**允许出现旧品牌目录名的位置
（属于 LEGACY MIGRATION REFERENCE，见 docs/MIGRATION_RENAMING.md）。

设计原则：
* 幂等：重复调用不会重复迁移；
* 不丢数据：目标已存在时只补齐缺失项，**绝不覆盖**；
* 可回退：优先 `rename`（同卷原子移动），跨卷时复制并保留源目录；
* 不输出任何 secret（只统计条目数量）。
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

# 旧品牌目录名：仅用于迁移，不要在产品其它位置硬编码
LEGACY_DIR_NAME = ".mewcode"
NEW_DIR_NAME = ".aieb"

# 旧/新项目指令文件名
LEGACY_INSTRUCTIONS = "MEWCODE.md"
NEW_INSTRUCTIONS = "AI-EXTERNAL-BRAIN.md"
LEGACY_LOCAL_INSTRUCTIONS = "MEWCODE.local.md"
NEW_LOCAL_INSTRUCTIONS = "AI-EXTERNAL-BRAIN.local.md"

# 中间品牌名（WenDream 时期）的项目指令文件，迁移时同样视为待改名对象
INTERIM_INSTRUCTIONS = "WENDREAM.md"
INTERIM_LOCAL_INSTRUCTIONS = "WENDREAM.local.md"

MARKER_NAME = ".migrated_from_mewcode"


@dataclass
class MigrationResult:
    migrated: bool = False
    action: str = ""  # "moved" | "merged" | "skipped" | "none"
    copied_entries: int = 0
    source: Path | None = None
    target: Path | None = None
    notes: list[str] = field(default_factory=list)

    def describe(self) -> str:
        if self.action == "none":
            return "no legacy runtime directory found"
        if self.action == "skipped":
            return f"legacy directory kept as-is ({self.source})"
        return f"{self.action} {self.source} -> {self.target} ({self.copied_entries} entries)"


def _write_marker(target: Path, source: Path) -> None:
    try:
        target.mkdir(parents=True, exist_ok=True)
        (target / MARKER_NAME).write_text(
            f"migrated_from={source.name}\nat={datetime.now().isoformat(timespec='seconds')}\n",
            encoding="utf-8",
        )
    except OSError:
        pass


def migrate_runtime_dir(base: Path, *, dry_run: bool = False) -> MigrationResult:
    """把 ``base/.mewcode`` 迁移到 ``base/.aieb``（幂等、不覆盖）。"""
    legacy = base / LEGACY_DIR_NAME
    current = base / NEW_DIR_NAME
    result = MigrationResult(source=legacy, target=current)

    if not legacy.exists() or not legacy.is_dir():
        result.action = "none"
        return result

    if not current.exists():
        result.action = "moved"
        result.migrated = True
        result.notes.append("renamed legacy runtime directory")
        if not dry_run:
            try:
                legacy.rename(current)
            except OSError:
                shutil.copytree(legacy, current)
                result.action = "moved(copied)"
                result.notes.append("cross-device move: copied and kept the legacy directory")
            _write_marker(current, legacy)
            result.copied_entries = len(list(current.iterdir())) if current.exists() else 0
        return result

    # 目标已存在：只补齐缺失项，绝不覆盖
    result.action = "merged"
    copied = 0
    for item in legacy.iterdir():
        if item.name == MARKER_NAME:
            continue
        dest = current / item.name
        if dest.exists():
            continue
        if not dry_run:
            try:
                if item.is_dir():
                    shutil.copytree(item, dest)
                else:
                    shutil.copy2(item, dest)
            except OSError:
                result.notes.append(f"skipped (copy failed): {item.name}")
                continue
        copied += 1
    result.copied_entries = copied
    result.migrated = copied > 0
    if copied:
        result.notes.append("existing .aieb data was preserved; only missing entries were copied")
    else:
        result.action = "skipped"
        result.notes.append("both directories exist and .aieb already has all entries")
    return result


def migrate_instructions(base: Path, *, dry_run: bool = False) -> bool:
    """把旧的指令文件改名成新名字（仅当目标不存在时）。

    链路：``MEWCODE.md`` / ``WENDREAM.md`` → ``AI-EXTERNAL-BRAIN.md``。
    目标文件已存在时绝不覆盖，避免冲掉用户新写的内容。
    """
    changed = False
    for legacy_name, new_name in (
        (LEGACY_INSTRUCTIONS, NEW_INSTRUCTIONS),
        (LEGACY_LOCAL_INSTRUCTIONS, NEW_LOCAL_INSTRUCTIONS),
        (INTERIM_INSTRUCTIONS, NEW_INSTRUCTIONS),
        (INTERIM_LOCAL_INSTRUCTIONS, NEW_LOCAL_INSTRUCTIONS),
    ):
        legacy = base / legacy_name
        current = base / new_name
        if legacy.exists() and not current.exists():
            if not dry_run:
                legacy.rename(current)
            changed = True
    return changed


def migrate_all(
    project_root: Path | None = None,
    *,
    include_home: bool = True,
    dry_run: bool = False,
) -> list[MigrationResult]:
    """迁移项目目录（以及用户 home）下的 legacy 运行时数据。"""
    roots: list[Path] = [Path(project_root or Path.cwd()).resolve()]
    if include_home:
        home = Path.home()
        if home not in roots:
            roots.append(home)

    results: list[MigrationResult] = []
    for root in roots:
        if not root.exists():
            continue
        res = migrate_runtime_dir(root, dry_run=dry_run)
        if not dry_run:
            migrate_instructions(root)
        results.append(res)
    return results


def migrate_and_report(project_root: Path | None = None, *, include_home: bool = True) -> str:
    """执行迁移并返回一行人类可读提示（没有迁移时返回空串）。"""
    try:
        results = migrate_all(project_root, include_home=include_home)
    except Exception as exc:  # pragma: no cover - 迁移失败绝不能阻断启动
        return f"[aieb] legacy migration skipped: {type(exc).__name__}"

    lines = [
        f"[aieb] migrated legacy runtime data: {res.describe()}"
        for res in results
        if res.migrated
    ]
    return "\n".join(lines)
