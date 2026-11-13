
from __future__ import annotations

import asyncio
import re
import shlex
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from aieb.tools.base import Tool, ToolResult
from aieb.tools.encoding import DecodeResult, decode_subprocess_output
from aieb.tools.workspace import is_within

if TYPE_CHECKING:
    from aieb.sandbox import Sandbox, SandboxConfig

MAX_TIMEOUT = 600

# 特殊命令的退出码语义映射
# 这些命令的 exit code 1 不代表错误，只有 >= 阈值才算真正的错误
# 例如 grep 返回 1 仅表示"没有匹配行"，不是执行出错
_COMMAND_ERROR_THRESHOLDS: dict[str, int] = {
    "grep": 2,   # exit 1 = 没有匹配到内容
    "egrep": 2,
    "fgrep": 2,
    "rg": 2,     # ripgrep，与 grep 语义一致
    "diff": 2,   # exit 1 = 文件内容有差异
    "find": 2,   # exit 1 = 部分成功（如权限不足跳过某些目录）
    "test": 2,   # exit 1 = 条件为假
    "[": 2,      # test 的别名形式
}


def _extract_last_command_name(command: str) -> str | None:
    """从命令字符串中提取最后一个管道段的基础命令名。

    管道中最后一个命令决定了整体退出码，所以只看最后一段。
    例如 "cat file | grep pattern" → "grep"
    """
    # 按管道符拆分，取最后一段
    last_segment = command.rsplit("|", maxsplit=1)[-1].strip()
    if not last_segment:
        return None

    # 跳过常见的环境变量赋值前缀，如 "FOO=bar command ..."
    # 也要处理 sudo/env 等包装命令
    try:
        tokens = shlex.split(last_segment)
    except ValueError:
        # shlex 解析失败时，用简单的空格分割兜底
        tokens = last_segment.split()

    for token in tokens:
        # 跳过形如 VAR=VALUE 的环境变量赋值
        if re.match(r"^[A-Za-z_]\w*=", token):
            continue
        # 取 basename（去掉路径前缀，如 /usr/bin/grep → grep）
        base = token.rsplit("/", maxsplit=1)[-1]
        return base

    return None


def _interpret_exit_code(command: str, exit_code: int) -> bool:
    """根据命令语义判断退出码是否代表真正的错误。

    返回 True 表示是错误，False 表示不是错误。
    """
    if exit_code == 0:
        return False

    cmd_name = _extract_last_command_name(command)
    if cmd_name and cmd_name in _COMMAND_ERROR_THRESHOLDS:
        # 只有退出码 >= 阈值时才视为错误
        return exit_code >= _COMMAND_ERROR_THRESHOLDS[cmd_name]

    # 默认行为：非零退出码即为错误
    return True


# 特殊命令的退出码提示信息
# 帮助 LLM 理解非零退出码的含义，而不是简单地标记为错误
_EXIT_CODE_HINTS: dict[str, str] = {
    "grep": "no matches found",
    "egrep": "no matches found",
    "fgrep": "no matches found",
    "rg": "no matches found",
    "diff": "files differ",
    "find": "some directories were inaccessible",
    "test": "condition is false",
    "[": "condition is false",
}


def _exit_code_hint(command: str, exit_code: int) -> str:
    """为非零退出码生成可读提示。

    对于特殊命令（grep/diff/test 等），附加语义说明让 LLM 理解退出码含义。
    普通命令只显示退出码数字。
    """
    cmd_name = _extract_last_command_name(command)
    hint = _EXIT_CODE_HINTS.get(cmd_name, "") if cmd_name else ""
    if hint:
        return f"Exit code {exit_code} ({hint})"
    return f"Exit code {exit_code}"


class Params(BaseModel):
    command: str = Field(description="Shell command to execute")
    timeout: int = Field(default=120, description="Timeout in seconds (max 600)")


KILL_WAIT_SECONDS = 2.0


async def _terminate(proc: asyncio.subprocess.Process) -> None:
    """尽力终止子进程——自己也不能挂住。

    Windows 上存在一种恶性情况：``proc.kill()`` 之后 ``proc.wait()`` 仍可能
    永远不返回（子进程的管道还有别的句柄持有者）。如果这里无保护地 await，
    超时处理路径本身就会把工具调用永久挂死。
    """
    try:
        proc.kill()
    except (ProcessLookupError, OSError):
        pass
    try:
        await asyncio.wait_for(proc.wait(), timeout=KILL_WAIT_SECONDS)
    except (asyncio.TimeoutError, ProcessLookupError, OSError):
        pass
    # 显式关闭管道，避免在 Proactor 事件循环上遗留 "unclosed transport"
    for stream in (proc.stdout, proc.stderr, proc.stdin):
        transport = getattr(stream, "_transport", None)
        if transport is not None:
            try:
                transport.close()
            except Exception:
                pass


# Windows 盘符绝对路径 / UNC 路径的字面形式（仅用于启发式预检）
_ABSOLUTE_PATH_RE = re.compile(r"(?:[A-Za-z]:[\\/][^\s\"'|&;<>()]*|\\\\\\\\[^\s\"'|&;<>()]+)")


def heuristic_workspace_violation(command: str, work_dir: str | None) -> str:
    """对命令做**启发式**的 workspace 越界预检。

    返回空字符串表示"没看出明显越界"，否则返回原因。

    重要：这只是一种成本很低的早期拒绝，**不是**安全边界。Shell 的引号、
    变量展开、子命令、PowerShell 表达式都能绕过任何静态字符串检查。真正的
    文件系统隔离只能靠内核级沙箱（见 aieb/sandbox）或容器。
    """
    if not work_dir:
        return ""

    # 1) 显式 .. 路径段：语法上就是向上跳，直接拒绝
    for token in re.split(r"[\s\"'|&;<>()]+", command):
        if not token:
            continue
        parts = re.split(r"[\\/]+", token)
        if ".." in parts:
            return f"命令包含父目录引用 '..' ({token})"

    # 2) 字面绝对路径：必须落在 work_dir 内
    for match in _ABSOLUTE_PATH_RE.findall(command):
        if not is_within(work_dir, match):
            return f"命令引用了 workspace 之外的绝对路径 ({match})"

    return ""


class Bash(Tool):
    name = "Bash"
    description = "Execute a shell command and return stdout and stderr."
    params_model = Params
    category = "command"

    # 工作目录，为 None 时使用当前进程的工作目录。
    # 这是 **cwd isolation**：相对路径的基准被固定，但不是文件系统沙箱。
    work_dir: str | None = None

    # 可选：workspace 越界**启发式**预检（默认关闭，供高约束环境使用）。
    # 开启后，明显越界的命令会在启动子进程之前被拒绝；它能降低误伤宿主目录的
    # 概率，但不能提供真正隔离。普通用户/普通命令不受影响（默认 False）。
    restrict_to_workspace: bool = False

    # OS 级沙箱实例和配置（由外部注入，为 None 时不启用沙箱）
    sandbox: Sandbox | None = None
    sandbox_config: SandboxConfig | None = None

    # 最近一次输出的解码信息（仅供观测/测试）
    last_decode: DecodeResult | None = None

    async def execute(self, params: Params) -> ToolResult:
        timeout = min(params.timeout, MAX_TIMEOUT)

        if self.restrict_to_workspace:
            violation = heuristic_workspace_violation(params.command, self.work_dir)
            if violation:
                return ToolResult(
                    output=(
                        f"Error: {violation}. Bash is cwd-scoped to the workspace; "
                        "commands that need files outside it are rejected in this mode."
                    ),
                    is_error=True,
                )

        # 如果启用了 OS 沙箱，将命令包装为沙箱内执行
        actual_command = params.command
        if self.sandbox and self.sandbox_config and self.sandbox.available():
            actual_command = self.sandbox.wrap(params.command, self.sandbox_config)

        try:
            proc = await asyncio.create_subprocess_shell(
                actual_command,
                # 绝不让子进程继承 Agent 的 stdin。否则遇到会读标准输入的命令
                # （例如 Windows 上 `cmd //c ...` —— `//c` 对 cmd.exe 非法，
                # 会变成交互式 cmd）时会永久阻塞，把整个工具调用拖死。
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,  # 合并 stderr 到 stdout
                cwd=self.work_dir,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            await _terminate(proc)
            return ToolResult(output=f"Error: command timed out after {timeout}s", is_error=True)
        except Exception as e:
            return ToolResult(output=f"Error executing command: {e}", is_error=True)

        # 合并流输出，不再区分 stdout/stderr
        # 解码必须还原真实编码：cmd.exe 内部命令输出的是 OEM 代码页（本机 936），
        # 直接按 UTF-8 解码会把几乎每个字节都变成 U+FFFD，模型无法读懂错误信息。
        decoded = decode_subprocess_output(stdout)
        self.last_decode = decoded
        output = decoded.text

        exit_code = proc.returncode or 0
        is_error = _interpret_exit_code(params.command, exit_code)
        if exit_code != 0:
            hint = _exit_code_hint(params.command, exit_code)
            if output:
                output = f"{output.rstrip()}\n\n{hint}"
            else:
                output = hint

        if not output:
            output = "(no output)"

        return ToolResult(output=output, is_error=is_error, metadata=decoded.as_metadata())

