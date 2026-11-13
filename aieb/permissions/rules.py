from __future__ import annotations

import re
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path
from typing import Any, Literal

import yaml

Effect = Literal["allow", "deny", "ask"]

_RULE_RE = re.compile(r"^(\w+)\((.+)\)$")

_CONTENT_FIELDS: dict[str, str] = {
    "Bash": "command",
    "ReadFile": "file_path",
    "WriteFile": "file_path",
    "EditFile": "file_path",
    "Glob": "pattern",
    "Grep": "pattern",
}

# 真正需要交给路径沙箱检查的字段。
#
# 注意 Grep / Glob 与上面的“内容字段”是分开的：规则匹配想知道的是 pattern，
# 而路径安全必须检查 path（搜索根目录）。以前两者共用 pattern，导致
# PathSandbox 拿着 "**/*.py" 去当文件路径校验，实际是空转。
_PATH_FIELDS: dict[str, str] = {
    "ReadFile": "file_path",
    "WriteFile": "file_path",
    "EditFile": "file_path",
    "Grep": "path",
    "Glob": "path",
}

# 这些工具的 path 参数缺省值就是 workspace 根
_PATH_DEFAULTS: dict[str, str] = {"Grep": ".", "Glob": "."}


@dataclass(frozen=True)
class Rule:
    tool_name: str
    pattern: str
    effect: Effect


    def matches(self, tool_name: str, content: str) -> bool:
        if self.tool_name != tool_name:
            return False
        return fnmatch(content, self.pattern)


def parse_rule(raw: str, effect: Effect) -> Rule:
    m = _RULE_RE.match(raw.strip())
    if not m:
        raise ValueError(f"无效的规则语法: {raw}")
    return Rule(tool_name=m.group(1), pattern=m.group(2), effect=effect)


def extract_content(tool_name: str, arguments: dict[str, Any]) -> str:
    field = _CONTENT_FIELDS.get(tool_name)
    if field is None:
        return ""
    return str(arguments.get(field, ""))


def extract_path(tool_name: str, arguments: dict[str, Any]) -> str:
    """返回该工具**实际会被解析的路径**（供路径沙箱使用）。

    - ReadFile/WriteFile/EditFile -> file_path
    - Grep/Glob -> path（搜索根；未提供时回退到 workspace 根 "."）
    - 其他工具（如 Bash）没有单一路径概念 -> 返回空字符串，交给更上层的策略处理
    """
    field = _PATH_FIELDS.get(tool_name)
    if field is None:
        return ""
    value = arguments.get(field)
    if value in (None, ""):
        return _PATH_DEFAULTS.get(tool_name, "")
    return str(value)


def _load_rules_file(path: Path) -> list[Rule]:
    if not path.is_file():
        return []
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (yaml.YAMLError, OSError):
        return []
    if not isinstance(raw, list):
        return []
    rules: list[Rule] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        rule_str = entry.get("rule", "")
        effect = entry.get("effect", "")
        if effect not in ("allow", "deny", "ask"):
            continue
        try:
            rules.append(parse_rule(rule_str, effect))
        except ValueError:
            continue
    return rules


class RuleEngine:


    def __init__(
        self,
        user_rules_path: Path | None = None,
        project_rules_path: Path | None = None,
        local_rules_path: Path | None = None,
    ) -> None:
        self._user_path = user_rules_path
        self._project_path = project_rules_path
        self._local_path = local_rules_path

    def _load_tiers(self) -> list[list[Rule]]:
        tiers: list[list[Rule]] = []
        for p in (self._user_path, self._project_path, self._local_path):
            tiers.append(_load_rules_file(p) if p else [])
        return tiers


    def evaluate(self, tool_name: str, content: str) -> Effect | None:
        matched = self.match(tool_name, content)
        return matched.effect if matched else None

    def match(self, tool_name: str, content: str) -> Rule | None:
        """返回命中的规则（只读），便于观测层记录是哪条规则生效。

        与 evaluate() 使用完全相同的匹配顺序与优先级，不改变决策结果。
        """
        for rules in self._load_tiers():
            for rule in reversed(rules):
                if rule.matches(tool_name, content):
                    return rule
        return None


    def append_local_rule(self, rule: Rule) -> None:
        if self._local_path is None:
            return
        self._local_path.parent.mkdir(parents=True, exist_ok=True)
        existing = _load_rules_file(self._local_path)
        existing.append(rule)
        entries = [{"rule": f"{r.tool_name}({r.pattern})", "effect": r.effect} for r in existing]
        self._local_path.write_text(yaml.dump(entries, allow_unicode=True), encoding="utf-8")
