"""看门狗 + 心跳 —— Hermes 骨架组件 #9。

- Heartbeat:daemon 线程每 interval 触摸 <data_dir>/heartbeat(供外部观察)
- Watchdog:turn 进行中且主循环超过 deadline 没有 beat(卡死)→
  faulthandler dump 全部线程栈 → os._exit(75)
  不变量 4:看门狗只杀不救;复活是外部 supervisor 的职责(组件 #10)。
"""

from __future__ import annotations

import faulthandler
import logging
import os
import threading
import time
from pathlib import Path
from typing import Optional

from .logging import log_event
from . import supervisor

_logger = logging.getLogger("mini_hermes")


class Heartbeat:
    """心跳文件:内容 = "pid iso-ts",每 interval_s 重写一次。"""

    def __init__(self, path: str | Path, interval_s: float = 1.0):
        self.path = Path(path)
        self.interval_s = interval_s
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def _tick(self) -> None:
        from datetime import datetime, timezone

        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            f"{os.getpid()} {datetime.now(timezone.utc).isoformat()}", encoding="utf-8"
        )

    def start(self) -> None:
        def loop() -> None:
            while not self._stop.wait(self.interval_s):
                try:
                    self._tick()
                except BaseException as e:
                    log_event(_logger, logging.ERROR, "daemon_thread_error",
                              thread="heartbeat", error=repr(e))

        self._tick()  # 启动即落一次,测试无需等一个 interval
        self._thread = threading.Thread(target=loop, name="mini-hermes-heartbeat", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)


class Watchdog:
    """冻结检测:turn 活跃期间 beat 超时 → dump 栈 → os._exit(75)。"""

    def __init__(
        self,
        deadline_s: float = 300.0,
        dump_path: str | Path = "watchdog_dump.log",
        check_interval_s: float = 0.2,
        exit_code: int = supervisor.EXIT_RESTART,
        logger: Optional[logging.Logger] = None,
    ):
        self.deadline_s = deadline_s
        self.dump_path = Path(dump_path)
        self.check_interval_s = check_interval_s
        self.exit_code = exit_code
        self.logger = logger or _logger

        self._active = False
        self._last_beat = time.monotonic()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    # 主循环调用 -------------------------------------------------------------

    def beat(self) -> None:
        with self._lock:
            self._last_beat = time.monotonic()

    def turn_begin(self) -> None:
        with self._lock:
            self._active = True
            self._last_beat = time.monotonic()

    def turn_end(self) -> None:
        with self._lock:
            self._active = False

    # 看门狗线程 -------------------------------------------------------------

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._loop, name="mini-hermes-watchdog", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    def _loop(self) -> None:
        while not self._stop.wait(self.check_interval_s):
            with self._lock:
                frozen = self._active and (
                    time.monotonic() - self._last_beat > self.deadline_s
                )
                frozen_for = time.monotonic() - self._last_beat
            if frozen:
                self._kill(frozen_for)

    def _kill(self, frozen_for_s: float) -> None:
        """只杀不救(不变量 4):dump 全部线程栈,然后 os._exit。"""
        log_event(self.logger, logging.CRITICAL, "watchdog_kill",
                  frozen_for_s=round(frozen_for_s, 3))
        try:
            self.dump_path.parent.mkdir(parents=True, exist_ok=True)
            with self.dump_path.open("w", encoding="utf-8") as f:
                faulthandler.dump_traceback(file=f, all_threads=True)
        except BaseException:
            pass  # dump 失败也要照杀
        os._exit(self.exit_code)
