from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from aieb.permissions.dangerous import DangerousCommandDetector, is_safe_command
from aieb.permissions.modes import DecisionEffect, PermissionMode, mode_decide
from aieb.permissions.rules import RuleEngine, extract_content, extract_path
from aieb.permissions.sandbox import PathSandbox
from aieb.tools.base import Tool

_PLAN_MODE_ALLOWED_TOOLS = frozenset({"Agent", "ToolSearch", "AskUserQuestion", "ExitPlanMode"})

# 受控记忆写入工具：参数里没有路径，落盘范围由记忆服务限定在受管记忆目录内。
# 它们不需要路径沙箱授权，否则用户每说一句"记住我的偏好"都要点一次确认。
# 注意：这只放行这些专用工具，不影响 ReadFile / WriteFile / Bash 的原有策略。
_MANAGED_MEMORY_TOOLS = frozenset({"SaveMemory"})

# ALLOW_ALWAYS 时允许“同目录复用”的文件类工具
_DIR_SCOPED_TOOLS = frozenset({"ReadFile", "WriteFile", "EditFile"})


@dataclass
class Decision:
    effect: DecisionEffect
    reason: str
    # 命中的规则（如有）。仅用于观测，不参与决策。
    rule: str = ""


class PermissionChecker:


    def __init__(
        self,
        detector: DangerousCommandDetector,
        sandbox: PathSandbox,
        rule_engine: RuleEngine,
        mode: PermissionMode = PermissionMode.DEFAULT,
        sandbox_enabled: bool = False,
        observer: Any = None,
    ) -> None:
        self.detector = detector
        self.sandbox = sandbox
        self.rule_engine = rule_engine
        self.mode = mode
        self.plan_file_path: str = ""
        # OS 级沙箱是否启用（开启后命令类工具可自动放行，因为内核会兜底）
        self.sandbox_enabled = sandbox_enabled
        # Layer 4b: 会话级 allow-always 集合（内存中，不持久化）
        # 存放格式为 "ToolName:pattern"，用户选择 "don't ask again" 时记录
        self._session_allowed: set[str] = set()
        # 只读观察者：在决策产生后收到结构化事件，不改变决策语义。
        self.observer = observer


    def add_session_allow(self, tool_name: str, content: str) -> None:
        """将工具+内容模式加入会话级放行集合（Layer 4b）。

        比持久化规则引擎优先级更高，但不写入磁盘——会话结束即消失。
        """
        key = f"{tool_name}:{content}"
        self._session_allowed.add(key)

    def _check_session_allowed(self, tool_name: str, content: str) -> bool:
        """检查是否匹配会话级放行记录。"""
        if not self._session_allowed:
            return False
        key = f"{tool_name}:{content}"
        if key in self._session_allowed:
            return True
        # 前缀匹配：已记录的 pattern 可能带通配尾缀
        for allowed in self._session_allowed:
            if allowed.endswith("*") and key.startswith(allowed[:-1]):
                return True
        return False

    def add_session_allow_dir(self, tool_name: str, path: str) -> None:
        """把**同一个目录**加入会话级放行（仅内存、不落盘）。

        动机：整理一组文件时，用户选了 Allow Always 却仍然对下一个文件重复
        弹窗——因为历史上只记精确路径。这里把同目录的同类读写复用到当前会话，
        不写入权限规则文件，也不会跨会话/跨目录扩大授权。
        """
        if tool_name not in _DIR_SCOPED_TOOLS or not path:
            return
        try:
            parent = os.path.dirname(os.path.abspath(path))
        except (TypeError, ValueError):
            return
        if parent:
            self._session_allowed.add(f"{tool_name}:{parent}{os.sep}*")

    def approve_directory(self, path: str) -> None:
        """用户对本会话显式放行该路径所在目录（由 Allow Always 触发）。

        与 ``add_session_allow_dir`` 的区别：这条作用于**路径沙箱**这一层，
        也就是真正会弹窗的判定点；仍然只限本会话、仍然受禁写清单约束。
        """
        approver = getattr(self.sandbox, "approve_dir", None)
        if callable(approver):
            try:
                approver(path)
            except Exception:  # noqa: BLE001 - 放行登记失败不应影响决策
                pass

    @staticmethod
    def describe_tool_action(tool_name: str, arguments: dict[str, Any]) -> str:
        """为 HITL 确认生成人类可读的操作描述（对齐 Go 版 ExtractContent + formatToolArgs）。"""
        content = extract_content(tool_name, arguments)
        if content:
            return content
        # 无法从标准字段提取时，拼接参数摘要
        parts = []
        for k, v in arguments.items():
            sv = str(v)
            if len(sv) > 80:
                sv = sv[:77] + "..."
            parts.append(f"{k}={sv}")
        return ", ".join(parts) if parts else tool_name


    def check(self, tool: Tool, arguments: dict[str, Any]) -> Decision:
        """返回权限决策，并在决策产生后通知只读观察者。

        观察者只接收事件，不影响返回值和执行路径。
        """
        decision = self._decide(tool, arguments)
        if self.observer is not None:
            try:
                self.observer(tool, arguments, decision)
            except Exception:  # pragma: no cover - 观测失败绝不影响决策
                pass
        return decision

    def _decide(self, tool: Tool, arguments: dict[str, Any]) -> Decision:
        content = extract_content(tool.name, arguments)

        # Layer 0: Plan 模式例外放行
        if self.mode == PermissionMode.PLAN:
            if tool.name in _PLAN_MODE_ALLOWED_TOOLS:
                return Decision(effect="allow", reason="Plan mode: allowed tool")
            if tool.name in ("WriteFile", "EditFile") and content:
                if self._is_plan_file(content):
                    return Decision(effect="allow", reason="Plan mode: plan file write")

        # Layer 0b: 受控记忆写入（无路径参数，写入范围由记忆服务限定）
        if tool.name in _MANAGED_MEMORY_TOOLS and self.mode != PermissionMode.PLAN:
            return Decision(
                effect="allow",
                reason="受管记忆写入（限定在记忆存储区内）",
            )

        # Layer 1: 安全的只读命令（自动放行）
        if tool.category == "command" and is_safe_command(content or ""):
            return Decision(effect="allow", reason="Safe read-only command")

        # Layer 1b: 危险命令黑名单（仅 Bash）
        if tool.category == "command":
            hit, reason = self.detector.detect(content)
            if hit:
                return Decision(effect="deny", reason=f"危险命令拦截: {reason}")

        # Layer 1c: OS 沙箱自动放行
        # 沙箱开启时，命令类工具通过了危险命令检查后直接放行——
        # 内核级隔离会阻止越权写入，无需再弹确认。
        # 拆分复合命令逐条检查，防止通过命令拼接绕过权限检查，
        # deny 规则和 ask 规则不受沙箱影响。
        if self.sandbox_enabled and tool.category == "command":
            import re
            subcommands = [s.strip() for s in re.split(r'\s*(?:&&|\|\||[;|])\s*', content) if s.strip()]
            if not subcommands:
                subcommands = [content]
            has_ask = False
            for sub in subcommands:
                rule_result = self.rule_engine.evaluate(tool.name, sub)
                if rule_result == "deny":
                    return Decision(effect="deny", reason="权限规则拒绝")
                if rule_result == "ask":
                    has_ask = True
            if has_ask:
                return Decision(effect="ask", reason="权限规则要求确认")
            return Decision(effect="allow", reason="OS 沙箱自动放行")

        # Layer 2: 路径沙箱（仅文件类工具）
        # 检查的是工具**真正会解析的路径**：ReadFile/WriteFile/EditFile 的 file_path，
        # Grep/Glob 的搜索根 path。以前 Grep/Glob 把 pattern 当路径校验，等于空转。
        if tool.category in ("read", "write"):
            path_value = extract_path(tool.name, arguments)
            if path_value:
                ok, reason = self.sandbox.check(path_value)
                if not ok and self.mode != PermissionMode.BYPASS:
                    return Decision(effect="ask", reason=f"路径沙箱拦截: {reason}")

        # Layer 3: 规则引擎匹配
        matched = self.rule_engine.match(tool.name, content)
        if matched is not None:
            rule_text = f"{matched.tool_name}({matched.pattern})"
            if matched.effect == "allow":
                return Decision(effect="allow", reason="权限规则放行", rule=rule_text)
            if matched.effect == "deny":
                return Decision(effect="deny", reason="权限规则拒绝", rule=rule_text)
            if matched.effect == "ask":
                return Decision(effect="ask", reason="权限规则要求确认", rule=rule_text)

        # Layer 4b: 会话级放行（内存中，优先于模式兜底）
        if self._check_session_allowed(tool.name, content or ""):
            return Decision(effect="allow", reason="会话级放行（session allow-always）")

        # Layer 4: 权限模式兜底判定
        effect = mode_decide(self.mode, tool.category)
        if effect == "allow":
            return Decision(effect="allow", reason=f"权限模式 {self.mode.value} 放行")
        if effect == "deny":
            return Decision(effect="deny", reason=f"权限模式 {self.mode.value} 拒绝")

        # Layer 5: 触发人工确认（HITL）
        return Decision(effect="ask", reason="需要用户确认")


    def _is_plan_file(self, target_path: str) -> bool:
        if not self.plan_file_path or not target_path:
            return ".aieb/plans/" in target_path
        try:
            abs_target = os.path.abspath(target_path)
            abs_plan = os.path.abspath(self.plan_file_path)
            if abs_target == abs_plan:
                return True
        except Exception:
            pass
        if os.path.basename(target_path) == os.path.basename(self.plan_file_path):
            return True
        return ".aieb/plans/" in target_path
