"""Resume 集成测试(PLAN §6 M1:test_resume.py)。

- WAL 崩溃一致性:子进程写完 os._exit(不 close),父进程能读到全部已提交行
- resume 重放出的消息列表与库中完全一致;计数器(user turns)重新水合
- 被中断的 tool-call:补合成 tool-result,绝不重执行(marker 文件只写一次)
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from mini_hermes.runtime import (
    INTERRUPTED_TOOL_RESULT,
    AgentRuntime,
    ToolRegistry,
    make_write_marker_file_tool,
)
from mini_hermes.state_db import SessionDB
from tests.fakes.openai_server import text_scenario, tool_call_scenario


def _runtime(db, session_id, fake_openai, **kw):
    return AgentRuntime(
        db,
        session_id,
        base_url=fake_openai.base_url,
        api_key="fake",
        model="fake-model",
        **kw,
    )


# ------------------------------------------------------------- WAL 崩溃一致性


def test_wal_consistency_after_abrupt_exit(tmp_path):
    db_path = tmp_path / "state.db"
    child = (
        "import os;"
        "from mini_hermes.state_db import SessionDB;"
        f"db = SessionDB(r'{db_path}');"
        "db.create_session('s1');"
        "[db.append_message('s1', {'role':'user','content':f'm{i}'}) for i in range(10)];"
        "os._exit(0)"  # 不 close、不 checkpoint,模拟 abrupt exit
    )
    subprocess.run([sys.executable, "-c", child], check=True)

    with SessionDB(db_path) as db:
        rows = db.get_messages("s1")
        assert [r["content"] for r in rows] == [f"m{i}" for i in range(10)]


# ------------------------------------------------------------- resume 基本语义


def test_resume_reloads_exact_messages_and_counters(tmp_path, fake_openai):
    fake_openai.queue_scenario(text_scenario("答 1"))
    with SessionDB(tmp_path / "state.db") as db:
        db.create_session("s1")
        rt = _runtime(db, "s1", fake_openai)
        rt.run_turn("问 1")

        db_rows = db.get_messages("s1")
        fake_openai.queue_scenario(text_scenario("答 2"))
        rt2 = _runtime(db, "s1", fake_openai)
        assert rt2.resume() is False  # 轮次已完成,无需续跑
        # 重放列表与库一致(内容、顺序、数量)
        assert [(m["role"], m["content"]) for m in rt2.messages] == [
            (m["role"], m["content"]) for m in db_rows
        ]
        assert rt2.user_turns == 1  # 计数器从历史水合
        # resume 后继续对话不丢不重
        rt2.run_turn("问 2")
        rows = db.get_messages("s1")
        assert [r["content"] for r in rows] == ["问 1", "答 1", "问 2", "答 2"]
        assert len(rows) == 4  # 无重复行


# ------------------------------------------------- 中断 tool-call 的恢复语义


def _tool_call_turn_state(db, session_id, *, with_result: bool):
    """手工摆一个崩溃现场:user + assistant(tool_calls) [+ tool result]。"""
    db.append_message(session_id, {"role": "user", "content": "干活的"})
    db.append_message(session_id, {
        "role": "assistant", "content": None,
        "tool_calls": [{"id": "call_1", "type": "function",
                        "function": {"name": "write_marker_file",
                                     "arguments": '{"text": "x"}'}}],
    })
    if with_result:
        db.append_message(session_id, {
            "role": "tool", "tool_call_id": "call_1",
            "name": "write_marker_file", "content": "marked",
        })


def test_resume_interrupted_tool_call_not_reexecuted(tmp_path, fake_openai):
    """崩溃发生在工具执行前:补合成结果,工具一次都不执行。"""
    marker = tmp_path / "marker.txt"
    fake_openai.queue_scenario(text_scenario("收尾"))
    with SessionDB(tmp_path / "state.db") as db:
        db.create_session("s1")
        _tool_call_turn_state(db, "s1", with_result=False)

        tools = ToolRegistry()
        tools.register(*make_write_marker_file_tool(str(marker)))
        rt = _runtime(db, "s1", fake_openai, tools=tools)
        assert rt.resume() is True  # 有未完工作

        rows_before = db.get_messages("s1")
        assert rows_before[-1]["role"] == "tool"
        assert rows_before[-1]["content"] == INTERRUPTED_TOOL_RESULT
        assert rows_before[-1]["tool_call_id"] == "call_1"

        reply = rt.continue_run()
        assert reply == "收尾"
        assert not marker.exists()  # 工具没有被执行
        # 合成行已落盘;最终序列:user, assistant(tc), tool(synthetic), assistant
        rows = db.get_messages("s1")
        assert [r["role"] for r in rows] == ["user", "assistant", "tool", "assistant"]


def test_resume_completed_tool_call_not_reexecuted(tmp_path, fake_openai):
    """崩溃发生在工具执行后:结果已在库,续跑不重执行(marker 仍只写一次)。"""
    marker = tmp_path / "marker.txt"
    marker.write_text("x\n")  # 崩溃前工具已经写过一次
    fake_openai.queue_scenario(text_scenario("收尾"))
    with SessionDB(tmp_path / "state.db") as db:
        db.create_session("s1")
        _tool_call_turn_state(db, "s1", with_result=True)

        tools = ToolRegistry()
        tools.register(*make_write_marker_file_tool(str(marker)))
        rt = _runtime(db, "s1", fake_openai, tools=tools)
        assert rt.resume() is True
        # 结果已齐,不需要合成
        assert all(
            r["content"] != INTERRUPTED_TOOL_RESULT for r in db.get_messages("s1")
        )
        assert rt.continue_run() == "收尾"
        assert marker.read_text().count("\n") == 1  # 没有重执行


def test_turn_error_contained_and_persisted(tmp_path, fake_openai, monkeypatch):
    """#7 每轮错误隔离:API 持续报错(重试 4 次全灭)→ TurnError,进程不死,状态已落盘。"""
    from mini_hermes.runtime import TurnError
    from tests.fakes.openai_server import error_scenario

    monkeypatch.setattr("mini_hermes.runtime.RETRY_BASE_S", 0.01)  # 测试不等真退避
    for _ in range(4):  # 重试预算 4 次全部打满
        fake_openai.queue_scenario(error_scenario(500))
    with SessionDB(tmp_path / "state.db") as db:
        db.create_session("s1")
        rt = _runtime(db, "s1", fake_openai)
        with pytest.raises(TurnError):
            rt.run_turn("hi")
        # 用户消息已落盘(crash-persist),进程活着
        assert [r["role"] for r in db.get_messages("s1")] == ["user"]
        # 下一轮还能跑(错误不致命)
        fake_openai.queue_scenario(text_scenario("恢复"))
        assert rt.run_turn("again") == "恢复"
