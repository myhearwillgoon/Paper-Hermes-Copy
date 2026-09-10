"""文件工具:read_file(行号/分页/二进制检测)、write_file、search_files(rg 或纯 Python)。"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

from ..registry import Registry, ToolContext

READ_CAP_BYTES = 100 * 1024
SEARCH_CAP_LINES = 250

_READ_SCHEMA = {
    "type": "object",
    "properties": {
        "path": {"type": "string"},
        "offset": {"type": "integer", "description": "起始行(1 基),默认 1"},
        "limit": {"type": "integer", "description": "最多读多少行,默认全部(受 100KB 上限约束)"},
    },
    "required": ["path"],
}

_WRITE_SCHEMA = {
    "type": "object",
    "properties": {
        "path": {"type": "string"},
        "content": {"type": "string"},
        "mode": {"type": "string", "enum": ["overwrite", "append"], "default": "overwrite"},
    },
    "required": ["path", "content"],
}

_SEARCH_SCHEMA = {
    "type": "object",
    "properties": {
        "pattern": {"type": "string", "description": "正则表达式"},
        "path": {"type": "string", "description": "搜索根目录,默认会话 cwd"},
        "glob": {"type": "string", "description": "文件名过滤,如 *.py"},
    },
    "required": ["pattern"],
}


def _resolve(ctx: ToolContext, path: str) -> Path:
    p = Path(path).expanduser()
    return p if p.is_absolute() else ctx.cwd / p


def _read_file(args: dict, ctx: ToolContext) -> str:
    path = _resolve(ctx, args.get("path", ""))
    if not path.is_file():
        return f"Error: 文件不存在: {path}"
    raw = path.read_bytes()
    if b"\x00" in raw:
        return f"Error: {path} 是二进制文件(含 NUL),不可读"
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return f"Error: {path} 不是 UTF-8 文本"
    lines = text.splitlines()
    offset = max(int(args.get("offset", 1)), 1)
    limit = args.get("limit")
    end = None if limit is None else offset - 1 + int(limit)
    selected = lines[offset - 1:end]
    out = "".join(f"{offset + i}\t{line}\n" for i, line in enumerate(selected))
    if len(out.encode("utf-8")) > READ_CAP_BYTES:
        out = out.encode("utf-8")[:READ_CAP_BYTES].decode("utf-8", errors="ignore")
        out += f"\n... [capped at {READ_CAP_BYTES} bytes]"
    return out or "(empty file)"


def _write_file(args: dict, ctx: ToolContext) -> str:
    path = _resolve(ctx, args.get("path", ""))
    content = args.get("content")
    if content is None:
        return "Error: 缺少 content"
    mode = args.get("mode", "overwrite")
    if mode not in ("overwrite", "append"):
        return f"Error: 非法 mode {mode!r}"
    path.parent.mkdir(parents=True, exist_ok=True)
    if mode == "append":
        with path.open("a", encoding="utf-8") as f:
            f.write(content)
    else:
        path.write_text(content, encoding="utf-8")
    return f"wrote {len(content)} chars to {path}({mode})"


def _search_files(args: dict, ctx: ToolContext) -> str:
    pattern = args.get("pattern", "")
    root = _resolve(ctx, args.get("path", "."))
    glob = args.get("glob")
    if not pattern:
        return "Error: empty pattern"
    try:
        re.compile(pattern)
    except re.error as e:
        return f"Error: 非法正则: {e}"
    if not root.exists():
        return f"Error: 路径不存在: {root}"

    rg = shutil.which("rg")
    if rg:
        cmd = [rg, "--line-number", "--no-heading", "--color", "never", pattern, str(root)]
        if glob:
            cmd += ["--glob", glob]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        lines = proc.stdout.splitlines()
    else:
        lines = _python_grep(pattern, root, glob)

    capped = lines[:SEARCH_CAP_LINES]
    suffix = f"\n... [capped at {SEARCH_CAP_LINES} lines]" if len(lines) > SEARCH_CAP_LINES else ""
    return "\n".join(capped) + suffix if capped else "(no matches)"


def _python_grep(pattern: str, root: Path, glob: str | None) -> list[str]:
    """rg 不在 PATH 时的纯 Python 回退。"""
    rx = re.compile(pattern)
    results: list[str] = []
    files = root.rglob(glob or "*")
    for path in sorted(files):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for i, line in enumerate(text.splitlines(), start=1):
            if rx.search(line):
                results.append(f"{path}:{i}:{line}")
                if len(results) >= SEARCH_CAP_LINES:
                    return results
    return results


def register_tools(registry: Registry) -> None:
    registry.register("read_file",
                      "读取文本文件,带行号;支持 offset/limit 分页;100KB 上限。",
                      _READ_SCHEMA, _read_file)
    registry.register("write_file",
                      "创建/覆盖/追加写文件,自动创建父目录。",
                      _WRITE_SCHEMA, _write_file)
    registry.register("search_files",
                      "正则内容搜索(rg 优先,否则纯 Python 回退),支持 glob 过滤,250 行上限。",
                      _SEARCH_SCHEMA, _search_files)
