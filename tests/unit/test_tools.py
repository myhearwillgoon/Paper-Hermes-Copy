"""5 核心工具单元测试(PLAN §6 M2:test_tools.py)。"""

from __future__ import annotations

import time

import pytest

from mini_hermes.state_db import SessionDB
from mini_hermes.tools.registry import Registry, ToolContext, discover_builtin_tools


@pytest.fixture()
def registry():
    return discover_builtin_tools(Registry())


@pytest.fixture()
def ctx(tmp_path):
    return ToolContext(cwd=tmp_path, session_id="s1")


# ------------------------------------------------------------------ registry


def test_discover_registers_five_tools(registry):
    assert set(registry.names()) >= {"terminal", "read_file", "write_file",
                                     "search_files", "todo"}
    schemas = registry.schemas()
    assert all(s["type"] == "function" for s in schemas)
    assert all("name" in s["function"] and "parameters" in s["function"] for s in schemas)


def test_unknown_tool_clear_error(registry, ctx):
    result = registry.execute("nope", "{}", ctx)
    assert result.startswith("Error: unknown tool")


def test_bad_arguments_error(registry, ctx):
    result = registry.execute("terminal", "{not json", ctx)
    assert result.startswith("Error: bad arguments")


def test_max_result_chars_truncation(registry, ctx, tmp_path):
    big = tmp_path / "big.txt"
    big.write_text("x" * 100_000)
    result = registry.execute("read_file", {"path": str(big)}, ctx)
    assert "capped" in result or "truncated" in result
    assert len(result) < 105_000


# ------------------------------------------------------------------ terminal


def test_terminal_basic(registry, ctx):
    result = registry.execute("terminal", {"command": "echo hello"}, ctx)
    assert "hello" in result


def test_terminal_stderr_merged_and_exit_code(registry, ctx):
    result = registry.execute("terminal", {"command": "echo oops >&2; exit 3"}, ctx)
    assert "oops" in result
    assert "exit code: 3" in result


def test_terminal_timeout_kills(registry, ctx):
    started = time.monotonic()
    result = registry.execute("terminal", {"command": "sleep 30", "timeout": 1}, ctx)
    assert time.monotonic() - started < 10
    assert result.startswith("Error: command timed out")


def test_terminal_timeout_capped(registry, ctx):
    result = registry.execute("terminal",
                              {"command": "echo ok", "timeout": 99999}, ctx)
    assert "ok" in result  # 99999 被钳到 300,不报错


def test_terminal_cwd(registry, ctx, tmp_path):
    result = registry.execute("terminal",
                              {"command": "pwd", "cwd": str(tmp_path)}, ctx)
    assert str(tmp_path) in result


def test_terminal_bad_cwd(registry, ctx):
    result = registry.execute("terminal",
                              {"command": "x", "cwd": "/nonexistent-xyz"}, ctx)
    assert result.startswith("Error: cwd 不存在")


# ------------------------------------------------------------------ read_file


def test_read_file_line_numbers_and_paging(registry, ctx, tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("l1\nl2\nl3\nl4\nl5\n")
    result = registry.execute("read_file", {"path": str(f), "offset": 2, "limit": 2}, ctx)
    assert "2\tl2" in result and "3\tl3" in result
    assert "l1" not in result and "l4" not in result


def test_read_file_binary_detected(registry, ctx, tmp_path):
    f = tmp_path / "bin.dat"
    f.write_bytes(b"abc\x00def")
    result = registry.execute("read_file", {"path": str(f)}, ctx)
    assert result.startswith("Error:") and "二进制" in result


def test_read_file_missing(registry, ctx):
    assert registry.execute("read_file", {"path": "nope.txt"}, ctx).startswith("Error:")


# ----------------------------------------------------------------- write_file


def test_write_file_overwrite_and_append(registry, ctx, tmp_path):
    f = tmp_path / "sub" / "dir" / "w.txt"  # 父目录不存在 → 自动创建
    registry.execute("write_file", {"path": str(f), "content": "hello"}, ctx)
    assert f.read_text() == "hello"
    registry.execute("write_file",
                     {"path": str(f), "content": "+", "mode": "append"}, ctx)
    assert f.read_text() == "hello+"
    registry.execute("write_file", {"path": str(f), "content": "new"}, ctx)
    assert f.read_text() == "new"


# --------------------------------------------------------------- search_files


def test_search_files_fallback_and_glob(registry, ctx, tmp_path, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: None)  # 强制纯 Python 回退
    (tmp_path / "a.py").write_text("def foo():\n    pass\n")
    (tmp_path / "b.txt").write_text("foo bar\n")
    result = registry.execute("search_files", {"pattern": "foo"}, ctx)
    assert "a.py:1:" in result and "b.txt:1:" in result
    only_py = registry.execute("search_files", {"pattern": "foo", "glob": "*.py"}, ctx)
    assert "a.py" in only_py and "b.txt" not in only_py


def test_search_files_rg_path(registry, ctx, tmp_path, monkeypatch):
    """rg 在 PATH 上时走 rg(用假 rg 验证命令行组装)。"""
    calls = []

    class FakeProc:
        stdout = "fake-rg:1:hit\n"
        returncode = 0

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return FakeProc()

    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/rg")
    monkeypatch.setattr("subprocess.run", fake_run)
    result = registry.execute("search_files",
                              {"pattern": "hit", "glob": "*.py"}, ctx)
    assert "fake-rg:1:hit" in result
    assert "--glob" in calls[0] and "*.py" in calls[0]


def test_search_files_bad_regex(registry, ctx):
    assert registry.execute("search_files", {"pattern": "(unclosed"}, ctx).startswith("Error:")


# ----------------------------------------------------------------------- todo


@pytest.fixture()
def db_ctx(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    db.create_session("s1")
    yield db, ToolContext(cwd=tmp_path, session_id="s1", db=db)
    db.close()


def test_todo_add_list_update_clear(registry, db_ctx):
    _, ctx = db_ctx
    registry.execute("todo", {"action": "add", "text": "写 M2"}, ctx)
    out = registry.execute("todo", {"action": "add", "text": "跑测试"}, ctx)
    assert "#2" in out
    out = registry.execute("todo", {"action": "update", "id": 1, "status": "done"}, ctx)
    assert "● #1 写 M2" in out and "○ #2 跑测试" in out
    assert registry.execute("todo", {"action": "clear"}, ctx) == "(cleared)"
    assert "(todo list empty)" in registry.execute("todo", {"action": "list"}, ctx)


def test_todo_rehydrated_on_resume(registry, db_ctx, tmp_path):
    """todos 落在 state_meta:新 ToolContext(模拟 resume 后)能读到。"""
    db, ctx = db_ctx
    registry.execute("todo", {"action": "add", "text": "崩溃前加的"}, ctx)
    ctx2 = ToolContext(cwd=tmp_path, session_id="s1", db=db)  # 模拟 resume 重建
    out = registry.execute("todo", {"action": "list"}, ctx2)
    assert "崩溃前加的" in out


def test_todo_requires_db(registry, ctx):
    assert registry.execute("todo", {"action": "list"}, ctx).startswith("Error:")
