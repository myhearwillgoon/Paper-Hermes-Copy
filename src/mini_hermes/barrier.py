"""测试专用 barrier(混沌测试确定性冻结点)。

仅当环境变量 MINI_HERMES_TEST_BARRIER_DIR 设置时生效:
runtime 在每个关键点 touch <dir>/<point>,然后轮询等待 <dir>/<point>.go
(超时 30s 后放行并记日志,防止测试挂死)。kill -9 场景不需要 .go。
每个 point 每进程只触发一次。

生产路径不得依赖本模块 —— 未设环境变量时 hit() 是零成本 no-op。
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

from .logging import log_event

POINTS = (
    "before_api_call",
    "after_user_persist",
    "before_tool_exec",
    "after_tool_exec",
    "before_finalize",
    # workflow 引擎(M3)
    "before_phase_turn",
    "before_gate_execution",
    # 压缩(M4)
    "before_aux_summarize",
    "before_compaction_txn",
    "inside_compaction_txn",
)

_TIMEOUT_S = 30.0
_POLL_S = 0.05


class TestBarrier:
    def __init__(self, directory: str | None = None):
        self._dir = Path(directory) if directory else None
        self._fired: set[str] = set()
        self._logger = logging.getLogger("mini_hermes")

    @classmethod
    def from_env(cls) -> "TestBarrier":
        return cls(os.environ.get("MINI_HERMES_TEST_BARRIER_DIR"))

    @property
    def enabled(self) -> bool:
        return self._dir is not None

    def hit(self, point: str) -> None:
        if self._dir is None or point in self._fired:
            return
        self._fired.add(point)
        self._dir.mkdir(parents=True, exist_ok=True)
        (self._dir / point).touch()
        log_event(self._logger, logging.DEBUG, "barrier_wait", point=point)
        go = self._dir / f"{point}.go"
        deadline = time.monotonic() + _TIMEOUT_S
        while not go.exists():
            if time.monotonic() > deadline:
                log_event(self._logger, logging.WARNING, "barrier_wait",
                          point=point, timed_out=True)
                return
            time.sleep(_POLL_S)
