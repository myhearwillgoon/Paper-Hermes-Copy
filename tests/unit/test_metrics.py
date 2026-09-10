"""metrics.py 单元测试:schema 默认值 + JSONL writer/reader round-trip。"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from mini_hermes.metrics import (
    MetricsReader,
    MetricsWriter,
    RunMetrics,
    TokenUsage,
)


def test_schema_defaults():
    m = RunMetrics(session_id="s1")
    assert m.arm is None
    assert m.run_id is None
    assert m.main_tokens.total == 0
    assert m.aux_tokens.total == 0
    assert m.tool_call_count == 0
    assert m.started_at.tzinfo is not None  # 必须带时区(UTC)
    assert m.ended_at is None


def test_extra_field_rejected():
    with pytest.raises(Exception):
        RunMetrics(session_id="s1", bogus=1)


def test_jsonl_round_trip(tmp_path):
    path = tmp_path / "metrics.jsonl"
    writer = MetricsWriter(path)

    m1 = RunMetrics(
        session_id="s1",
        run_id="r1",
        arm="treatment",
        main_tokens=TokenUsage(input_tokens=100, output_tokens=50),
        aux_tokens=TokenUsage(input_tokens=30, output_tokens=10),
        tool_call_count=7,
        started_at=datetime(2026, 8, 1, 12, 0, 0, tzinfo=timezone.utc),
        ended_at=datetime(2026, 8, 1, 12, 30, 0, tzinfo=timezone.utc),
    )
    m2 = RunMetrics(session_id="s2", arm="control", tool_call_count=3)
    writer.write(m1)
    writer.write(m2)

    loaded = MetricsReader(path).read_all()
    assert loaded == [m1, m2]
    assert loaded[0].main_tokens.input_tokens == 100
    assert loaded[0].aux_tokens.output_tokens == 10
    assert loaded[1].ended_at is None


def test_reader_missing_file_returns_empty(tmp_path):
    assert MetricsReader(tmp_path / "nope.jsonl").read_all() == []


def test_writer_creates_parent_dirs(tmp_path):
    path = tmp_path / "deep" / "nested" / "m.jsonl"
    MetricsWriter(path).write(RunMetrics(session_id="s"))
    assert path.exists()


def test_append_semantics(tmp_path):
    """JSONL 追加写:两个 writer 实例写同一文件不互相覆盖。"""
    path = tmp_path / "m.jsonl"
    MetricsWriter(path).write(RunMetrics(session_id="a"))
    MetricsWriter(path).write(RunMetrics(session_id="b"))
    assert [m.session_id for m in MetricsReader(path).read_all()] == ["a", "b"]
