"""工具注册表(Hermes `tools/registry.py` 精神:模块导入时自注册)。

- `register(name, description, parameters, handler, max_result_chars=...)`
  注册到指定 Registry;builtin 模块在 discover 时被导入并自注册
- Registry.schemas() 产出 OpenAI function-calling 定义
- Registry.execute():查不到工具/参数坏/handler 抛异常都返回 `Error: ...`
  字符串,绝不把异常抛进 loop;按 max_result_chars 截断
- ToolContext:cwd / session_id / config / db,handler 的第二参数
"""

from __future__ import annotations

import importlib
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from ..logging import log_event

_logger = logging.getLogger("mini_hermes")

DEFAULT_MAX_RESULT_CHARS = 50_000

BUILTIN_MODULES = [
    "mini_hermes.tools.builtin.terminal",
    "mini_hermes.tools.builtin.files",
    "mini_hermes.tools.builtin.todo",
    "mini_hermes.tools.builtin.gates",
    "mini_hermes.tools.builtin.web_search",
]

# 学习闭环工具(PLAN §5:no_skills 时不注册)
LEARNING_MODULES = [
    "mini_hermes.tools.builtin.learning_tools",
]


@dataclass
class ToolContext:
    cwd: Path = field(default_factory=lambda: Path.cwd())
    session_id: str = ""
    config: Any = None
    db: Any = None  # SessionDB;todo 等工具用它持久化
    phase: Optional[str] = None  # 当前 workflow phase(skill_usage 归因,不变量 3)


@dataclass
class ToolDef:
    name: str
    description: str
    parameters: dict
    handler: Callable[[dict, ToolContext], str]
    max_result_chars: int = DEFAULT_MAX_RESULT_CHARS


class Registry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolDef] = {}

    def register(
        self,
        name: str,
        description: str,
        parameters: dict,
        handler: Callable[[dict, ToolContext], str],
        *,
        max_result_chars: int = DEFAULT_MAX_RESULT_CHARS,
    ) -> None:
        self._tools[name] = ToolDef(name, description, parameters, handler, max_result_chars)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def subset(self, names: list[str]) -> "Registry":
        """按允许名单生成受限 Registry(workflow phase toolset 隔离用)。"""
        sub = Registry()
        for name in names:
            if name not in self._tools:
                raise KeyError(f"toolset 引用了未注册的工具:{name!r}")
            sub._tools[name] = self._tools[name]
        return sub

    def schemas(self) -> list[dict]:
        """OpenAI function-calling 工具定义。"""
        return [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.parameters,
                },
            }
            for t in self._tools.values()
        ]

    def execute(
        self,
        name: str,
        arguments: str | dict,
        ctx: Optional[ToolContext] = None,
    ) -> str:
        """执行工具,返回字符串结果(含 `Error: ...`),绝不抛出。"""
        ctx = ctx or ToolContext()
        tool = self._tools.get(name)
        if tool is None:
            return f"Error: unknown tool {name!r}(可用:{', '.join(self.names())})"
        if isinstance(arguments, str):
            try:
                args = json.loads(arguments) if arguments.strip() else {}
            except json.JSONDecodeError as e:
                return f"Error: bad arguments for {name}: {e}"
        else:
            args = dict(arguments)

        log_event(_logger, logging.INFO, "tool_call",
                  session_id=ctx.session_id or None, tool_name=name,
                  arguments_preview=json.dumps(args, ensure_ascii=False)[:200])
        started = time.monotonic()
        try:
            result = tool.handler(args, ctx)
            ok = not str(result).startswith("Error:")
            result_str = str(result)
        except Exception as e:  # 工具失败 = 工具结果,不是 loop 失败
            ok = False
            result_str = f"Error: tool {name} crashed: {e}"
        duration_ms = int((time.monotonic() - started) * 1000)
        log_event(_logger, logging.INFO, "tool_result",
                  session_id=ctx.session_id or None, tool_name=name,
                  ok=ok, duration_ms=duration_ms)

        if len(result_str) > tool.max_result_chars:
            result_str = (
                result_str[: tool.max_result_chars]
                + f"\n... [truncated at {tool.max_result_chars} chars]"
            )
        return result_str


_DEFAULT = Registry()


def default_registry() -> Registry:
    return _DEFAULT


def register(
    name: str,
    description: str,
    parameters: dict,
    handler: Callable[[dict, ToolContext], str],
    *,
    max_result_chars: int = DEFAULT_MAX_RESULT_CHARS,
) -> None:
    """模块级自注册入口(builtin 工具模块用)。"""
    _DEFAULT.register(name, description, parameters, handler,
                      max_result_chars=max_result_chars)


def discover_builtin_tools(
    registry: Optional[Registry] = None, *, include_learning: bool = True
) -> Registry:
    """导入 builtin 工具包完成自注册,返回填好的 Registry。

    include_learning=False 时不注册 skill/memory/session_search(no_skills 隔离)。
    """
    registry = registry or _DEFAULT
    modules = list(BUILTIN_MODULES)
    if include_learning:
        modules += LEARNING_MODULES
    for module_name in modules:
        module = importlib.import_module(module_name)
        register_tools = getattr(module, "register_tools", None)
        if register_tools is not None:
            register_tools(registry)
    return registry
