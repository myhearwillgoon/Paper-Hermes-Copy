"""压缩混沌测试(PLAN §6 M4:test_compress_crash.py)。

kill -9 × 压缩关键路径:
(a) before_aux_summarize(摘要调用前)—— 什么都没发生
(b) before_compaction_txn(摘要已返回,事务未开始)—— 什么都没落库
(c) inside_compaction_txn(事务中途:UPDATE 已执行,INSERT 未执行)—— 整体回滚
三种杀法后:transcript 一致(无半标记状态);重启后续跑,压缩正常完成。
"""

from __future__ import annotations

import signal
import subprocess
import sys
from pathlib import Path

import pytest

from mini_hermes.state_db import SessionDB
from tests.chaos.spawn import prep_barrier_go, wait_exit, wait_for_file, write_config
from tests.fakes.openai_server import text_scenario

AUX_SUMMARY = text_scenario("## Resolved\n- 事实甲\n\n## Pending\n- 任务乙")
MAIN_ANSWER = text_scenario("压缩后的回答")

_CHILD = """
import sys
from types import SimpleNamespace
from mini_hermes.state_db import SessionDB
from mini_hermes.runtime import AgentRuntime

db_path, base_url = sys.argv[1], sys.argv[2]
db = SessionDB(db_path)
db.create_session('s1')
n = db._conn.execute("SELECT COUNT(*) c FROM messages WHERE session_id='s1'").fetchone()['c']
if n == 0:
    for i in range(12):
        db.append_message('s1', {'role': 'user' if i % 2 == 0 else 'assistant',
                                 'content': f'消息{i} ' + '长文本' * 30})
rt = AgentRuntime(db, 's1', base_url=base_url, api_key='fake', model='fake-model',
                  compression=SimpleNamespace(enabled=True, threshold=10, tail_messages=3))
rt.messages = db.get_messages('s1')
print(rt.run_turn('继续'))
db.close()
"""


def _spawn_child(db_path: Path, base_url: str, barrier_dir: Path | None):
    import os

    env = dict(os.environ)
    env.pop("MINI_HERMES_TEST_BARRIER_DIR", None)
    if barrier_dir is not None:
        env["MINI_HERMES_TEST_BARRIER_DIR"] = str(barrier_dir)
    return subprocess.Popen(
        [sys.executable, "-c", _CHILD, str(db_path), base_url],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
    )


def _counts(db_path: Path):
    with SessionDB(db_path) as db:
        total = db._conn.execute(
            "SELECT COUNT(*) c FROM messages WHERE session_id='s1'").fetchone()["c"]
        compacted = db._conn.execute(
            "SELECT COUNT(*) c FROM messages WHERE session_id='s1' AND compacted=1"
        ).fetchone()["c"]
        summaries = db.count_summaries("s1")
    return total, compacted, summaries


@pytest.mark.parametrize(
    "kill_point", ["before_aux_summarize", "before_compaction_txn", "inside_compaction_txn"]
)
def test_compress_crash_consistency(tmp_path, fake_openai, kill_point):
    # P1 在 before_aux_summarize 不消耗任何场景;另两个杀点消耗 1 个 aux 摘要
    aux_consumed_by_p1 = 0 if kill_point == "before_aux_summarize" else 1
    for _ in range(aux_consumed_by_p1 + 1):
        fake_openai.queue_scenario(AUX_SUMMARY)
    fake_openai.queue_scenario(MAIN_ANSWER)
    db_path = tmp_path / "state.db"
    barrier_dir = tmp_path / "barrier"
    prep_barrier_go(barrier_dir, except_point=kill_point)

    # P1:杀在压缩关键路径上
    p1 = _spawn_child(db_path, fake_openai.base_url, barrier_dir)
    wait_for_file(barrier_dir / kill_point)
    p1.send_signal(signal.SIGKILL)
    rc1, _, err1 = wait_exit(p1)
    assert rc1 == -signal.SIGKILL, err1

    # 一致性:无半标记状态 —— 要么全没发生,要么(不可能)全落
    total, compacted, summaries = _counts(db_path)
    assert compacted == 0 and summaries == 0, (
        f"杀点 {kill_point} 后存在半完成压缩:compacted={compacted} summaries={summaries}"
    )

    # P2:无 barrier 重启 → 压缩完成 + 轮次正常结束
    p2 = _spawn_child(db_path, fake_openai.base_url, None)
    rc2, out2, err2 = wait_exit(p2, timeout_s=120)
    assert rc2 == 0, f"P2 失败:{err2}"
    assert "压缩后的回答" in out2

    total2, compacted2, summaries2 = _counts(db_path)
    assert summaries2 == 1
    assert compacted2 > 0
    # 每行要么 active 要么 compacted,无第三态
    assert compacted2 < total2
    with SessionDB(db_path) as db:
        active = db.get_messages("s1")
        assert any(m["content"].startswith("[COMPACTION SUMMARY v1") for m in active)
