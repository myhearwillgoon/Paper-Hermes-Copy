"""SIGTERM 混沌测试:mid-turn 收到 SIGTERM → 优雅退出(不是 -15)、落盘链完好、resume 可用。"""

from __future__ import annotations

import signal

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


def test_sigterm_mid_turn_clean_exit(tmp_path, fake_openai):
    fake_openai.queue_scenario(
        tool_call_scenario(
            [{"id": "call_1", "name": "write_marker_file", "arguments": '{"text": "x"}'}]
        )
    )
    fake_openai.queue_scenario(text_scenario("完成"))

    data_dir = tmp_path / "data"
    barrier_dir = tmp_path / "barrier"
    marker = tmp_path / "marker.txt"
    config = write_config(tmp_path, fake_openai.base_url, data_dir)

    # P1:冻结在 before_tool_exec(turn 正中间),发 SIGTERM 后放行
    prep_barrier_go(barrier_dir, except_point="before_tool_exec")
    p1 = spawn_chat(
        config, "--session", "s1", "--message", "干活",
        env_extra=barrier_env(barrier_dir, marker),
    )
    wait_for_file(barrier_dir / "before_tool_exec")
    p1.send_signal(signal.SIGTERM)
    (barrier_dir / "before_tool_exec.go").touch()  # 放行,让它走完当前落盘
    rc1, _, err1 = wait_exit(p1)
    assert rc1 == 0, f"SIGTERM 应优雅退出(exit 0),实际 {rc1}\n{err1}"

    # 落盘链完好:user + assistant(tc) + tool(已执行并落盘)
    with SessionDB(data_dir / "state.db") as db:
        rows = db.get_messages("s1")
    assert [r["role"] for r in rows] == ["user", "assistant", "tool"]
    assert marker.read_text().splitlines() == ["x"]

    # P2:resume 续跑到干净结束,工具不重执行
    p2 = spawn_chat(config, "--resume", "s1")
    rc2, out2, err2 = wait_exit(p2)
    assert rc2 == 0, f"resume 应干净退出,实际 {rc2}\n{err2}"
    with SessionDB(data_dir / "state.db") as db:
        rows = db.get_messages("s1")
    assert [r["role"] for r in rows] == ["user", "assistant", "tool", "assistant"]
    assert rows[-1]["content"] == "完成"
    assert marker.read_text().splitlines() == ["x"]  # 仍只有一次
    assert "完成" in out2
