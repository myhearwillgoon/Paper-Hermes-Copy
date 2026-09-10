"""不变量层(PLAN §7):独立于功能测试存在,功能重构后必须仍然通过。

M2 先落地不变量 1 与 2;其余随各自子系统(M3-M5)进层。
"""

from __future__ import annotations

import sqlite3

import pytest

from mini_hermes import persistence
from mini_hermes.runtime import AgentRuntime, ToolRegistry, make_write_marker_file_tool
from mini_hermes.state_db import SessionDB
from tests.fakes.openai_server import text_scenario, tool_call_scenario


class _FailAfterNWrites:
    """SessionDB 代理:前 N 次写放行,之后 append_message 必失败。"""

    def __init__(self, db: SessionDB, n: int):
        self._db = db
        self._n = n
        self._writes = 0

    def append_message(self, session_id, msg):
        self._writes += 1
        if self._writes > self._n:
            raise sqlite3.OperationalError("forced write failure (invariant test)")
        return self._db.append_message(session_id, msg)

    def __getattr__(self, name):
        return getattr(self._db, name)


def test_invariant_1_tool_never_runs_from_unpersisted_state(tmp_path, fake_openai):
    """不变量 1:副作用工具绝不从未落盘的状态执行。

    pre-side-effect 落盘失败 → 本轮中止(session_persistence_failed),
    工具的副作用(marker 文件)必须不存在。
    """
    fake_openai.queue_scenario(
        tool_call_scenario(
            [{"id": "c1", "name": "write_marker_file", "arguments": '{"text": "x"}'}]
        )
    )
    marker = tmp_path / "marker.txt"
    db = SessionDB(tmp_path / "state.db")
    db.create_session("s1")
    # 放行第 1 次写(crash-persist 的用户消息),之后的写全部失败
    broken = _FailAfterNWrites(db, n=1)

    tools = ToolRegistry()
    tools.register(*make_write_marker_file_tool(str(marker)))
    rt = AgentRuntime(
        broken, "s1",
        base_url=fake_openai.base_url, api_key="fake", model="fake-model",
        tools=tools,
    )
    with pytest.raises(persistence.SessionPersistenceFailed,
                       match="session_persistence_failed"):
        rt.run_turn("干活")

    assert not marker.exists(), "不变量 1 被违反:工具从未落盘的状态执行了"
    db.close()


def test_invariant_2_flush_idempotent_no_duplicate_rows(tmp_path, fake_openai):
    """不变量 2:任何落盘点幂等 —— 重复 flush 只写新行。"""
    fake_openai.queue_scenario(text_scenario("答"))
    db = SessionDB(tmp_path / "state.db")
    db.create_session("s1")
    rt = AgentRuntime(
        db, "s1",
        base_url=fake_openai.base_url, api_key="fake", model="fake-model",
    )
    rt.run_turn("问")
    before = db.get_messages("s1")
    # 从任意落盘点重复调用 flush:一行都不多写
    assert persistence.flush_new_messages(db, "s1", rt.messages) == 0
    persistence.crash_persist(db, "s1", rt.messages)
    persistence.finalize_persist(db, "s1", rt.messages)
    after = db.get_messages("s1")
    assert [r["_rowid"] for r in after] == [r["_rowid"] for r in before]
    db.close()
