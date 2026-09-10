"""logging.py 单元测试:JSON-lines 格式、公共字段、文件输出、异常字段。"""

from __future__ import annotations

import json
import logging

import pytest

from mini_hermes.logging import JsonFormatter, configure_logging, log_event


def _parse_lines(path):
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def test_log_event_required_fields(tmp_path):
    logger = configure_logging("INFO", log_dir=tmp_path, logger_name="t1")
    log_event(logger, logging.INFO, "cassette_record", cassette_key="abc", url="http://x")

    (record,) = _parse_lines(tmp_path / "mini-hermes.jsonl")
    assert record["event"] == "cassette_record"
    assert record["level"] == "INFO"
    assert record["logger"] == "t1"
    assert "ts" in record
    assert record["cassette_key"] == "abc"
    assert record["url"] == "http://x"
    assert "session_id" not in record  # 可选字段缺省不出现


def test_log_event_context_fields(tmp_path):
    logger = configure_logging("INFO", log_dir=tmp_path, logger_name="t2")
    log_event(logger, logging.WARNING, "llm_error", session_id="s1", run_id="r1", status=429)

    (record,) = _parse_lines(tmp_path / "mini-hermes.jsonl")
    assert record["session_id"] == "s1"
    assert record["run_id"] == "r1"
    assert record["status"] == 429


def test_exception_field(tmp_path):
    logger = configure_logging("INFO", log_dir=tmp_path, logger_name="t3")
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        log_event(logger, logging.ERROR, "watchdog_kill", exc_info=True)

    (record,) = _parse_lines(tmp_path / "mini-hermes.jsonl")
    assert "RuntimeError: boom" in record["exception"]


def test_level_filtering(tmp_path):
    logger = configure_logging("WARNING", log_dir=tmp_path, logger_name="t4")
    log_event(logger, logging.DEBUG, "cassette_replay")
    log_event(logger, logging.INFO, "config_loaded")
    log_event(logger, logging.ERROR, "cassette_miss")

    records = _parse_lines(tmp_path / "mini-hermes.jsonl")
    assert [r["event"] for r in records] == ["cassette_miss"]


def test_configure_idempotent(tmp_path):
    """重复 configure 不叠加 handler(不产生重复行)。"""
    logger = configure_logging("INFO", log_dir=tmp_path, logger_name="t5")
    logger = configure_logging("INFO", log_dir=tmp_path, logger_name="t5")
    log_event(logger, logging.INFO, "config_loaded")
    assert len(_parse_lines(tmp_path / "mini-hermes.jsonl")) == 1


def test_formatter_plain_record_is_valid_json():
    """直接 logger.info(...) 也能产出合法 JSON(event 缺省为 'log')。"""
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "hello", (), None)
    obj = json.loads(JsonFormatter().format(record))
    assert obj["event"] == "log"
    assert obj["level"] == "INFO"
