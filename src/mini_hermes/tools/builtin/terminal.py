"""terminal 工具:非交互 subprocess,超时即杀,合并 stdout+stderr,结果截断。"""

from __future__ import annotations

import subprocess
from pathlib import Path

from ..registry import Registry, ToolContext

DEFAULT_TIMEOUT_S = 60
MAX_TIMEOUT_S = 300
MAX_RESULT_CHARS = 30_000

_SCHEMA = {
    "type": "object",
    "properties": {
        "command": {"type": "string", "description": "要执行的 shell 命令(非交互)"},
        "timeout": {"type": "integer",
                    "description": f"超时秒数,默认 {DEFAULT_TIMEOUT_S},上限 {MAX_TIMEOUT_S}"},
        "cwd": {"type": "string", "description": "工作目录,默认会话 cwd"},
    },
    "required": ["command"],
}


def _terminal(args: dict, ctx: ToolContext) -> str:
    command = args.get("command", "")
    if not command.strip():
        return "Error: empty command"
    try:
        timeout = min(int(args.get("timeout", DEFAULT_TIMEOUT_S)), MAX_TIMEOUT_S)
    except (TypeError, ValueError):
        return f"Error: bad timeout {args.get('timeout')!r}"
    cwd = args.get("cwd") or str(ctx.cwd)
    if not Path(cwd).is_dir():
        return f"Error: cwd 不存在: {cwd}"
    try:
        proc = subprocess.run(
            ["bash", "-c", command],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return f"Error: command timed out after {timeout}s(killed): {command}"
    output = (proc.stdout or "") + (proc.stderr or "")
    if not output:
        output = "(no output)"
    if proc.returncode != 0:
        output += f"\n[exit code: {proc.returncode}]"
    return output


def register_tools(registry: Registry) -> None:
    registry.register(
        "terminal",
        "在 shell 中执行非交互命令,返回合并的 stdout+stderr。",
        _SCHEMA,
        _terminal,
        max_result_chars=MAX_RESULT_CHARS,
    )
