"""结构化 JSON-lines 日志基线(PLAN M0 交付物,schema 见 docs/logging-schema.md)。

每行一个 JSON 对象:{ts, level, event, logger, session_id?, run_id?, ...事件字段}。
stdlib logging + JSON formatter;由 config.logging 配置级别与目录。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

RESERVED_ATTRS = {
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
    "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
    "created", "msecs", "relativeCreated", "thread", "threadName",
    "processName", "process", "message", "asctime",
    # 业务上下文字段由 log_event 显式放入 extra
    "event", "session_id", "run_id",
}


class JsonFormatter(logging.Formatter):
    """每行一个 JSON 对象。事件字段经 extra 传入。"""

    def format(self, record: logging.LogRecord) -> str:
        obj: dict = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "event": getattr(record, "event", "log"),
        }
        for key in ("session_id", "run_id"):
            value = getattr(record, key, None)
            if value is not None:
                obj[key] = value
        # extra 里的事件特定字段(排除保留属性)
        for key, value in record.__dict__.items():
            if key not in RESERVED_ATTRS and not key.startswith("_"):
                obj[key] = value
        if record.exc_info:
            obj["exception"] = self.formatException(record.exc_info)
        return json.dumps(obj, ensure_ascii=False, default=str)


def log_event(
    logger: logging.Logger,
    level: int,
    event: str,
    *,
    session_id: Optional[str] = None,
    run_id: Optional[str] = None,
    exc_info: bool = False,
    **fields,
) -> None:
    """发出一条结构化事件。event 取值见 docs/logging-schema.md。"""
    extra = {"event": event, **fields}
    if session_id is not None:
        extra["session_id"] = session_id
    if run_id is not None:
        extra["run_id"] = run_id
    logger.log(level, event, extra=extra, exc_info=exc_info)


def configure_logging(
    level: str = "INFO",
    log_dir: Optional[str | Path] = None,
    logger_name: str = "mini_hermes",
) -> logging.Logger:
    """按 config 配置 JSON-lines 日志。

    - log_dir 给出时:写 <log_dir>/mini-hermes.jsonl(并同时输出到 stderr)
    - 否则只输出 stderr
    重复调用幂等(先清掉旧 handler)。
    """
    logger = logging.getLogger(logger_name)
    logger.setLevel(level.upper())
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)

    formatter = JsonFormatter()

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)

    if log_dir is not None:
        path = Path(log_dir).expanduser()
        path.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(path / "mini-hermes.jsonl", encoding="utf-8")
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    return logger


def configure_from_config(config, logger_name: str = "mini_hermes") -> logging.Logger:
    """从 Config 对象配置(config.logging.level / .dir)。"""
    return configure_logging(
        level=config.logging.level,
        log_dir=config.logging.dir,
        logger_name=logger_name,
    )
