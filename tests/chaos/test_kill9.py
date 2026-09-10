"""M1 验收:kill -9 × 4 个落盘点 × 崩溃 → 重启 → 消息不丢不重、tool-call 不重复执行。

流程(每个落盘点一个用例,确定性由 barrier 保证,不 sleep-and-hope):
1. 假端点排好场景队列(跨两个进程共享,按请求顺序消费)
2. P1 = CLI 子进程,barrier 冻结在目标落盘点 → kill -9
3. P2 = CLI --resume(不带 barrier)跑到干净结束
4. 断言:最终消息序列精确匹配、无重复行、工具执行次数符合该点的语义
"""

from __future__ import annotations

import signal

import pytest

from mini_hermes.state_db import SessionDB
from tests.chaos.spawn import (
    barrier_env,
    prep_barrier_go,
    spawn_chat,
    wait_exit,
    wait_for_file,
    write_config,
)
from tests.fakes.openai_server import text_scenario, tool_call_scenario

MARKER_TOOL_CALL = tool_call_scenario(
    [{"id": "call_1", "name": "write_marker_file", "arguments": '{"text": "x"}'}]
)
FINAL = text_scenario("完成")


def _read_rows(data_dir):
    with SessionDB(data_dir / "state.db") as db:
        return db.get_messages("s1")


def _run_kill9_cycle(tmp_path, fake_openai, point: str, scenarios: list[dict]):
    """一个 kill-9 循环,返回 (rows, marker_lines, p2_stdout)。"""
    for s in scenarios:
        fake_openai.queue_scenario(s)
    data_dir = tmp_path / "data"
    barrier_dir = tmp_path / "barrier"
    marker = tmp_path / "marker.txt"
    config = write_config(tmp_path, fake_openai.base_url, data_dir)

    # P1:冻结在目标落盘点,然后 kill -9(其余 barrier 点预放行)
    prep_barrier_go(barrier_dir, except_point=point)
    p1 = spawn_chat(
        config, "--session", "s1", "--message", "干活",
        env_extra=barrier_env(barrier_dir, marker),
    )
    wait_for_file(barrier_dir / point)
    p1.send_signal(signal.SIGKILL)
    rc1, _, err1 = wait_exit(p1)
    assert rc1 == -signal.SIGKILL, f"P1 应被 SIGKILL 杀死,实际 {rc1}\n{err1}"

    # P2:resume 跑到干净结束(不带 barrier / marker)
    p2 = spawn_chat(config, "--resume", "s1")
    rc2, out2, err2 = wait_exit(p2)
    assert rc2 == 0, f"P2 应干净退出,实际 {rc2}\n{err2}"

    rows = _read_rows(data_dir)
    marker_lines = marker.read_text().splitlines() if marker.exists() else []
    return rows, marker_lines, out2


def _assert_no_duplicates(rows):
    rowids = [r["_rowid"] for r in rows]
    assert rowids == sorted(rowids) and len(set(rowids)) == len(rowids)


@pytest.mark.parametrize("point", ["before_api_call", "after_user_persist"])
def test_kill9_before_first_api_call(tmp_path, fake_openai, point):
    """用户消息已落盘、未调 API 时被杀:resume 后续跑,不丢不重。"""
    rows, marker_lines, out = _run_kill9_cycle(tmp_path, fake_openai, point, [FINAL])

    assert [(r["role"], r["content"]) for r in rows] == [
        ("user", "干活"), ("assistant", "完成"),
    ]
    _assert_no_duplicates(rows)
    assert marker_lines == []  # 工具从未执行
    assert "完成" in out


def test_kill9_before_tool_exec(tmp_path, fake_openai):
    """tool-call 已落盘、工具未执行被杀:resume 补合成结果,工具 0 次执行。"""
    rows, marker_lines, out = _run_kill9_cycle(
        tmp_path, fake_openai, "before_tool_exec", [MARKER_TOOL_CALL, FINAL]
    )

    assert [r["role"] for r in rows] == ["user", "assistant", "tool", "assistant"]
    assert rows[1]["tool_calls"][0]["id"] == "call_1"
    assert rows[2]["tool_call_id"] == "call_1"
    assert "interrupted by crash" in rows[2]["content"]  # 合成结果
    assert rows[3]["content"] == "完成"
    _assert_no_duplicates(rows)
    assert marker_lines == []  # 关键断言:工具没有执行过
    assert "完成" in out


def test_kill9_after_tool_exec(tmp_path, fake_openai):
    """工具已执行且结果已落盘后被杀:resume 不重执行(marker 恰好 1 行)。"""
    rows, marker_lines, out = _run_kill9_cycle(
        tmp_path, fake_openai, "after_tool_exec", [MARKER_TOOL_CALL, FINAL]
    )

    assert [r["role"] for r in rows] == ["user", "assistant", "tool", "assistant"]
    assert rows[2]["content"] == "marked"  # 真实工具结果,不是合成行
    assert rows[3]["content"] == "完成"
    _assert_no_duplicates(rows)
    assert marker_lines == ["x"]  # 关键断言:恰好执行一次,resume 未重放
    assert "完成" in out
