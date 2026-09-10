"""压缩器单元测试(PLAN §6 M4:test_compressor.py)。

头/尾保护边界、摘要插入与标记、摘要迭代、租约拒绝、token 估算触发、
context-overflow 重试路径(假端点)。
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from mini_hermes import compressor as comp
from mini_hermes.compressor import Compressor, estimate_tokens, split_head_middle_tail
from mini_hermes.runtime import AgentRuntime, TurnError
from mini_hermes.state_db import SessionDB
from tests.fakes.openai_server import error_scenario, text_scenario

SUMMARY_TEXT = "## Resolved\n- 已确立事实 A\n\n## Pending\n- 未决任务 B"


@pytest.fixture()
def seeded(tmp_path):
    """20 条消息(user/assistant 交替)的会话。"""
    db = SessionDB(tmp_path / "state.db")
    db.create_session("s1")
    for i in range(20):
        role = "user" if i % 2 == 0 else "assistant"
        db.append_message("s1", {"role": role, "content": f"消息{i} " + "内容" * 30})
    yield db
    db.close()


def _compressor(db, summarize=None, **kw):
    return Compressor(
        db, "s1",
        summarize=summarize or (lambda prompt: SUMMARY_TEXT),
        **kw,
    )


# ------------------------------------------------------------ head/tail 边界


def test_split_boundaries():
    msgs = ([{"role": "system", "content": "sys"}]
            + [{"role": "user", "content": "u0"},
               {"role": "assistant", "content": "a0"}]
            + [{"role": "user", "content": f"u{i}"} for i in range(1, 10)])
    head, middle, tail = split_head_middle_tail(msgs, tail_n=3)
    assert [m["content"] for m in head] == ["sys", "u0", "a0"]
    assert [m["content"] for m in tail] == ["u7", "u8", "u9"]
    assert len(middle) == len(msgs) - 3 - 3


def test_compress_marks_middle_inserts_summary(seeded):
    db = seeded
    messages = db.get_messages("s1")
    head_rowids = [m["_rowid"] for m in messages[:2]]
    tail_rowids = [m["_rowid"] for m in messages[-5:]]

    new = _compressor(db, tail_messages=5).compress(messages)
    assert new is not None

    # 头尾原样(rowid 不变,仍 active)
    assert [m["_rowid"] for m in new[:2]] == head_rowids
    assert [m["_rowid"] for m in new[-5:]] == tail_rowids
    # 摘要插在中间,带 marker 与版本号
    summary = new[2]
    assert summary["content"].startswith("[COMPACTION SUMMARY v1 covers_to=")
    assert SUMMARY_TEXT in summary["content"]
    assert len(new) == 2 + 1 + 5

    # DB:中段 compacted=1,不再进 active 视图;行未被删除(可审计)
    active = db.get_messages("s1")
    assert [m["_rowid"] for m in active] == [m["_rowid"] for m in new]
    everything = db.get_messages("s1", include_compacted=True)
    assert len(everything) == 21  # 20 原始 + 1 摘要
    assert sum(1 for m in db.get_messages("s1", include_compacted=True)
               if m.get("_compacted")) == 13  # 20 - 2头 - 5尾


def test_summary_iterates(seeded):
    """第二次压缩把第一次的摘要并入输入(摘要迭代)。"""
    db = seeded
    c = _compressor(db, tail_messages=2)
    messages = c.compress(db.get_messages("s1"))

    # 追加新消息制造第二个中段
    for i in range(6):
        db.append_message("s1", {"role": "user" if i % 2 == 0 else "assistant",
                                 "content": f"后续{i} " + "新内容" * 30})
    seen_prompts = []
    c2 = _compressor(db, tail_messages=2,
                     summarize=lambda p: seen_prompts.append(p) or SUMMARY_TEXT)
    new2 = c2.compress(db.get_messages("s1"))
    assert new2 is not None
    assert "[COMPACTION SUMMARY v1" in seen_prompts[0]  # 旧摘要进了摘要器输入
    assert db.count_summaries("s1") == 2
    assert any(m["content"].startswith("[COMPACTION SUMMARY v2") for m in new2)


def test_lease_refusal_skips(seeded, monkeypatch):
    """活跃外部租约 → busy wait 后跳过,不改动任何消息。"""
    monkeypatch.setattr(comp, "LEASE_WAIT_S", 0.5)
    db = seeded
    db._conn.execute(
        "INSERT INTO compression_locks(session_id, holder, acquired_at, expires_at)"
        f" VALUES ('s1', '{os.getpid()}@testhost:peer', '2026-01-01T00:00:00+00:00',"
        "        '2999-01-01T00:00:00+00:00')"
    )
    before = db.get_messages("s1", include_compacted=True)
    assert _compressor(db).compress(db.get_messages("s1")) is None
    after = db.get_messages("s1", include_compacted=True)
    assert [m["_rowid"] for m in after] == [m["_rowid"] for m in before]


def test_empty_middle_skips(seeded):
    db = seeded
    msgs = db.get_messages("s1")[:3]  # 只有头
    assert _compressor(db, tail_messages=5).compress(msgs) is None


# ----------------------------------------------------------------- token 触发


def test_estimate_tokens_uses_persisted_counts():
    msgs = [{"role": "user", "content": "x" * 400, "token_count": 50},
            {"role": "assistant", "content": "y" * 400}]
    assert estimate_tokens(msgs) == 50 + (400 // 4 + 4)


def test_maybe_compress_threshold(seeded):
    db = seeded
    msgs = db.get_messages("s1")
    assert _compressor(db, tail_messages=5, threshold=10**9).maybe_compress(msgs) is None
    assert _compressor(db, tail_messages=5, threshold=10).maybe_compress(msgs) is not None


# ------------------------------------------- context-overflow 重试(runtime 集成)


def _runtime_with_compression(db, fake_openai, threshold):
    rt = AgentRuntime(
        db, "s1",
        base_url=fake_openai.base_url, api_key="fake", model="fake-model",
        compression=SimpleNamespace(enabled=True, threshold=threshold,
                                    tail_messages=3),
    )
    rt.messages = db.get_messages("s1")
    return rt


def test_context_overflow_triggers_compress_and_retry(seeded, fake_openai, monkeypatch):
    """400 + context-length 签名 → 压缩 → 重试一次成功。"""
    monkeypatch.setattr("mini_hermes.runtime.RETRY_BASE_S", 0.01)
    db = seeded
    fake_openai.queue_scenario(
        error_scenario(400, "This model's maximum context length exceeded (fake)")
    )
    fake_openai.queue_scenario(text_scenario(SUMMARY_TEXT))       # 辅模型摘要
    fake_openai.queue_scenario(text_scenario("压缩后回答"))        # 重试的主调用

    rt = _runtime_with_compression(db, fake_openai, threshold=10**9)
    assert rt.run_turn("继续") == "压缩后回答"
    assert db.count_summaries("s1") == 1  # 溢出路径真的压缩了
    active = db.get_messages("s1")
    assert any(m["content"].startswith("[COMPACTION SUMMARY") for m in active)


def test_context_overflow_only_retried_once(seeded, fake_openai, monkeypatch):
    """压缩后仍溢出 → 不再第二次压缩/重试,上浮 TurnError。"""
    monkeypatch.setattr("mini_hermes.runtime.RETRY_BASE_S", 0.01)
    db = seeded
    fake_openai.queue_scenario(
        error_scenario(400, "maximum context length exceeded (fake)"))
    fake_openai.queue_scenario(text_scenario(SUMMARY_TEXT))
    fake_openai.queue_scenario(
        error_scenario(400, "maximum context length exceeded again (fake)"))

    rt = _runtime_with_compression(db, fake_openai, threshold=10**9)
    with pytest.raises(TurnError):
        rt.run_turn("继续")
    assert db.count_summaries("s1") == 1  # 只压缩了一次


def test_plain_400_not_retried_no_compression(seeded, fake_openai):
    """普通 400(无溢出签名)→ 不压缩不重试。"""
    db = seeded
    fake_openai.queue_scenario(error_scenario(400, "bad request (fake)"))
    rt = _runtime_with_compression(db, fake_openai, threshold=10**9)
    with pytest.raises(TurnError):
        rt.run_turn("继续")
    assert db.count_summaries("s1") == 0
