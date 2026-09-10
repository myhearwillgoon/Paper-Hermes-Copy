"""落盘链单元测试(PLAN §6 M1:test_persistence.py)。

覆盖:flush 幂等/去重、标记打点、append-only rowid 顺序、busy-retry、
compression_locks 租约(获取/过期回收/死 PID 回收)。
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time

import pytest

from mini_hermes import persistence
from mini_hermes.persistence import SessionPersistenceFailed
from mini_hermes.state_db import BusyTimeoutError, SessionDB


@pytest.fixture()
def db(tmp_path):
    with SessionDB(tmp_path / "state.db") as d:
        d.create_session("s1")
        yield d


# ----------------------------------------------------------- flush 幂等/去重


def test_flush_stamps_marker_and_is_idempotent(db):
    msgs = [{"role": "user", "content": "你好"}]
    assert persistence.flush_new_messages(db, "s1", msgs) == 1
    assert msgs[0][persistence.PERSISTED_MARK] is True
    assert "_rowid" in msgs[0]
    # 重复调用:只写新行(不变量 2)
    assert persistence.flush_new_messages(db, "s1", msgs) == 0
    assert len(db.get_messages("s1")) == 1


def test_flush_incremental_only_new_rows(db):
    msgs = [{"role": "user", "content": "m1"}]
    persistence.flush_new_messages(db, "s1", msgs)
    msgs.append({"role": "assistant", "content": "m2"})
    assert persistence.flush_new_messages(db, "s1", msgs) == 1
    rows = db.get_messages("s1")
    assert [r["content"] for r in rows] == ["m1", "m2"]


def test_ephemeral_scaffolding_stripped(db):
    msgs = [
        {"role": "user", "content": "real", "_note": "scratch", "_db_persisted": False},
        {"role": "assistant", "content": "temp", "_ephemeral": True},
    ]
    assert persistence.flush_new_messages(db, "s1", msgs) == 1
    rows = db.get_messages("s1")
    assert len(rows) == 1
    assert rows[0]["content"] == "real"
    # `_` 前缀键不进 api_content
    raw = db._conn.execute("SELECT api_content FROM messages").fetchone()
    assert "_note" not in raw["api_content"]
    # ephemeral 也被打标,不会反复尝试
    assert msgs[1][persistence.PERSISTED_MARK] is True


def test_append_only_rowid_order(db):
    """ORDER BY rowid:时间戳乱序不影响读取顺序。"""
    for i in range(5):
        db.append_message("s1", {"role": "user", "content": f"m{i}"})
    rows = db.get_messages("s1")
    assert [r["content"] for r in rows] == [f"m{i}" for i in range(5)]
    assert [r["_rowid"] for r in rows] == sorted(r["_rowid"] for r in rows)


# ---------------------------------------------------------------- busy-retry


def test_busy_retry_exhausts_budget(db):
    """另一个连接持有写锁 → 写在预算内重试后抛 BusyTimeoutError。"""
    blocker = sqlite3.connect(str(db.path), isolation_level=None)
    blocker.execute("BEGIN IMMEDIATE")
    try:
        started = time.monotonic()
        with pytest.raises(BusyTimeoutError):
            db._with_busy_retry(
                lambda: db._conn.execute(
                    "INSERT INTO state_meta(key, value) VALUES ('k', 'v')"
                ),
                budget_s=1.0,
            )
        assert time.monotonic() - started >= 1.0
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()


def test_busy_retry_succeeds_after_lock_release(db):
    """锁释放后重试成功(不抛异常)。"""
    locked = threading.Event()

    def hold_then_release():
        # sqlite 连接有线程亲和性,在持有线程内创建
        blocker = sqlite3.connect(str(db.path), isolation_level=None)
        blocker.execute("BEGIN IMMEDIATE")
        locked.set()
        time.sleep(0.3)
        blocker.execute("ROLLBACK")
        blocker.close()

    threading.Thread(target=hold_then_release).start()
    locked.wait(timeout=5)
    db.meta_set("k", "v")  # 内部走 busy-retry,预算 20s
    assert db.meta_get("k") == "v"


# --------------------------------------------------------- pre-side-effect


class _BrokenDB(SessionDB):
    def append_message(self, session_id, msg):  # noqa: ARG002
        raise sqlite3.OperationalError("disk on fire")


def test_pre_side_effect_failure_aborts_turn(tmp_path):
    broken = _BrokenDB.__new__(_BrokenDB)  # 不连真库,只要方法
    msgs = [{"role": "assistant", "content": None,
             "tool_calls": [{"id": "c1", "type": "function",
                             "function": {"name": "x", "arguments": "{}"}}]}]
    with pytest.raises(SessionPersistenceFailed, match="session_persistence_failed"):
        persistence.pre_side_effect_persist(broken, "s1", msgs)


def test_crash_persist_swallows_failure(tmp_path):
    broken = _BrokenDB.__new__(_BrokenDB)
    msgs = [{"role": "user", "content": "hi"}]
    assert persistence.crash_persist(broken, "s1", msgs) is False  # 不抛


# ---------------------------------------------------------- compression 锁


def test_compression_lock_acquire_and_release(db):
    holder = db.acquire_compression_lock("s1")
    assert holder is not None
    # 活跃持有方占用 → 再次获取失败
    assert db.acquire_compression_lock("s1") is None
    # 非持有方不能释放
    assert db.release_compression_lock("s1", "0:intruder") is False
    # 持有方释放后可重新获取
    assert db.release_compression_lock("s1", holder) is True
    assert db.acquire_compression_lock("s1") is not None


def test_compression_lock_expired_reclaim(db):
    holder = db.acquire_compression_lock("s1", ttl_s=60)
    # 手动把过期时间改到过去
    db._conn.execute(
        "UPDATE compression_locks SET expires_at=? WHERE holder=?",
        ("2000-01-01T00:00:00+00:00", holder),
    )
    assert db.acquire_compression_lock("s1") is not None


def test_compression_lock_dead_pid_reclaim(db):
    db._conn.execute(
        "INSERT INTO compression_locks(session_id, holder, acquired_at, expires_at)"
        " VALUES ('s1', '99999999:deadbeef', '2026-01-01T00:00:00+00:00',"
        "        '2999-01-01T00:00:00+00:00')"
    )
    # 未过期但 PID 不存在 → 可回收
    assert db.acquire_compression_lock("s1") is not None


def test_compression_lock_live_pid_not_reclaimed(db):
    db._conn.execute(
        "INSERT INTO compression_locks(session_id, holder, acquired_at, expires_at)"
        f" VALUES ('s1', '{os.getpid()}:other', '2026-01-01T00:00:00+00:00',"
        "        '2999-01-01T00:00:00+00:00')"
    )
    assert db.acquire_compression_lock("s1") is None


# ----------------------------------------------------------------- FTS / schema


def test_fts_syncs_on_insert(db):
    db.append_message("s1", {"role": "user", "content": "丘斯特洛夫斯基"})
    rows = db._conn.execute(
        "SELECT rowid FROM messages_fts WHERE messages_fts MATCH '丘斯特洛夫斯基'"
    ).fetchall()
    assert len(rows) == 1


def test_schema_migrate_idempotent(tmp_path):
    with SessionDB(tmp_path / "a.db") as d1:
        pass
    # 二次打开同一文件:CREATE IF NOT EXISTS + version 行不重复
    with SessionDB(tmp_path / "a.db") as d2:
        row = d2._conn.execute("SELECT COUNT(*) c FROM schema_version").fetchone()
        assert row["c"] == 1
