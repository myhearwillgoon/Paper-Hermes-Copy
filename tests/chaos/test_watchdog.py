"""看门狗测试(不变量 4:只杀不救):主循环卡死 → dump 栈 → exit 75。"""

from __future__ import annotations

import subprocess
import sys

from tests.chaos.spawn import REPO_ROOT, wait_exit
from tests.fakes.openai_server import tool_call_scenario

_CHILD = """
import sys, threading
from mini_hermes.state_db import SessionDB
from mini_hermes.runtime import AgentRuntime, ToolRegistry
from mini_hermes.watchdog import Watchdog

db_path, dump_path, base_url = sys.argv[1], sys.argv[2], sys.argv[3]
db = SessionDB(db_path)
db.create_session('s1')

tools = ToolRegistry()
tools.register('wedge', '永远卡住的测试工具',
               {'type': 'object', 'properties': {}},
               lambda args, ctx: threading.Event().wait())  # 永远卡住

wd = Watchdog(deadline_s=1.0, dump_path=dump_path, check_interval_s=0.1)
wd.start()
rt = AgentRuntime(db, 's1', base_url=base_url, api_key='fake', model='fake-model',
                  tools=tools, watchdog=wd)
rt.run_turn('go')  # 卡在 wedge 工具里,看门狗应杀死本进程
"""


def test_watchdog_kills_wedged_process(tmp_path, fake_openai):
    fake_openai.queue_scenario(
        tool_call_scenario([{"id": "c1", "name": "wedge", "arguments": "{}"}])
    )
    dump = tmp_path / "watchdog_dump.log"
    proc = subprocess.Popen(
        [sys.executable, "-c", _CHILD,
         str(tmp_path / "state.db"), str(dump), fake_openai.base_url],
        cwd=REPO_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    rc, _, err = wait_exit(proc, timeout_s=60)

    assert rc == 75, f"看门狗应以 75 杀死卡死进程,实际 {rc}\n{err}"
    assert dump.exists(), "卡死前应 dump 线程栈"
    content = dump.read_text(encoding="utf-8")
    # 主线程栈应显示它卡在 run_turn(工具执行)里
    assert "Thread" in content and "run_turn" in content
