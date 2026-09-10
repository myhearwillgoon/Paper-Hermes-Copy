"""落盘链 —— Hermes 骨架组件 #2-#5。

- #4 flush_new_messages:`_db_persisted` 标记,幂等只写新行
- #2 crash_persist:用户消息在首次 API 调用前落盘,失败吞掉(只记日志)
- #3 pre_side_effect_persist:tool-call 在工具执行前落盘,失败即中止本轮
  (不变量 1:副作用工具绝不从未落盘的状态执行)
- #5 finalize_persist:剥掉临时脚手架(`_` 前缀键 / `_ephemeral` 消息)后落盘
"""

from __future__ import annotations

import logging
from typing import Optional

from .logging import log_event
from .state_db import SessionDB

_logger = logging.getLogger("mini_hermes")

PERSISTED_MARK = "_db_persisted"


class SessionPersistenceFailed(RuntimeError):
    """pre-side-effect 落盘失败:本轮必须中止,不重试不降级(不变量 1)。

    异常消息固定含 `session_persistence_failed` 供日志/测试断言。
    """

    def __init__(self, cause: BaseException):
        super().__init__(f"session_persistence_failed: {cause}")
        self.cause = cause


def _storable(msg: dict) -> Optional[dict]:
    """剥掉临时脚手架。`_ephemeral` 消息不落盘;`_` 前缀键不进库。"""
    if msg.get("_ephemeral"):
        return None
    return {k: v for k, v in msg.items() if not k.startswith("_")}


def flush_new_messages(db: SessionDB, session_id: str, messages: list[dict]) -> int:
    """幂等落盘:只写未打标记的行,写完打标。返回本次写入行数。

    任何落盘点都可重复调用 —— 重复调用只写新行(不变量 2)。
    """
    written = 0
    for msg in messages:
        if msg.get(PERSISTED_MARK):
            continue
        row = _storable(msg)
        if row is None:
            msg[PERSISTED_MARK] = True  # ephemeral 视为已处理,不再重试
            continue
        msg["_rowid"] = db.append_message(session_id, row)
        msg[PERSISTED_MARK] = True
        written += 1
    if written:
        log_event(_logger, logging.DEBUG, "turn_persist",
                  session_id=session_id, point="flush", rows_written=written)
    return written


def crash_persist(db: SessionDB, session_id: str, messages: list[dict]) -> bool:
    """组件 #2:首次 API 调用前把用户消息落盘。失败吞掉(记日志),不阻断对话。"""
    try:
        flush_new_messages(db, session_id, messages)
        log_event(_logger, logging.DEBUG, "crash_persist", session_id=session_id, ok=True)
        return True
    except Exception as e:
        log_event(_logger, logging.WARNING, "crash_persist",
                  session_id=session_id, ok=False, error=str(e))
        return False


def pre_side_effect_persist(db: SessionDB, session_id: str, messages: list[dict]) -> None:
    """组件 #3:工具执行前落盘 tool-call。失败抛 SessionPersistenceFailed。"""
    try:
        flush_new_messages(db, session_id, messages)
    except Exception as e:
        log_event(_logger, logging.ERROR, "pre_side_effect_persist",
                  session_id=session_id, ok=False, error=str(e))
        raise SessionPersistenceFailed(e) from e
    log_event(_logger, logging.DEBUG, "pre_side_effect_persist",
              session_id=session_id, ok=True)


def finalize_persist(db: SessionDB, session_id: str, messages: list[dict]) -> bool:
    """组件 #5:本轮结束(含所有错误退出路径)剥脚手架后落盘。失败只记日志。"""
    try:
        flush_new_messages(db, session_id, messages)
        return True
    except Exception as e:
        log_event(_logger, logging.WARNING, "finalize_persist",
                  session_id=session_id, ok=False, error=str(e))
        return False
