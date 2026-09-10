"""Session DB schema(PLAN §4)+ 幂等迁移入口。

8 张表:schema_version / sessions / messages / messages_fts /
compression_locks / state_meta / skill_usage / experiment_runs。
"""

from __future__ import annotations

import sqlite3

SCHEMA_VERSION = 2

_DDL = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    id            TEXT PRIMARY KEY,
    source        TEXT,
    model         TEXT,
    system_prompt TEXT,
    started_at    TEXT NOT NULL,
    ended_at      TEXT,
    end_reason    TEXT,
    message_count INTEGER NOT NULL DEFAULT 0,
    input_tokens  INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cwd           TEXT,
    title         TEXT,
    archived      INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS messages (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id    TEXT NOT NULL REFERENCES sessions(id),
    role          TEXT NOT NULL,
    content       TEXT,
    tool_call_id  TEXT,
    tool_calls    TEXT,          -- JSON 数组(assistant 消息)
    tool_name     TEXT,
    timestamp     TEXT NOT NULL,
    token_count   INTEGER,
    finish_reason TEXT,
    reasoning     TEXT,
    active        INTEGER NOT NULL DEFAULT 1,
    compacted     INTEGER NOT NULL DEFAULT 0,
    api_content   TEXT           -- 原始 API 消息 JSON
);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, id);

-- FTS5 external-content,触发器保持同步(简单起见全量镜像 content)
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    content,
    content='messages',
    content_rowid='id'
);
CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
END;
CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content)
    VALUES ('delete', old.id, old.content);
END;
CREATE TRIGGER IF NOT EXISTS messages_au AFTER UPDATE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, content)
    VALUES ('delete', old.id, old.content);
    INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
END;

CREATE TABLE IF NOT EXISTS compression_locks (
    session_id  TEXT PRIMARY KEY,
    holder      TEXT NOT NULL,   -- "<pid>:<nonce>"
    acquired_at TEXT NOT NULL,
    expires_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS state_meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS skill_usage (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id        TEXT NOT NULL,
    skill_name        TEXT NOT NULL,
    phase             TEXT,
    loaded_at         TEXT NOT NULL,
    gate_results_json TEXT
);

CREATE TABLE IF NOT EXISTS experiment_runs (
    id                TEXT PRIMARY KEY,
    task_id           TEXT,
    arm               TEXT,      -- treatment | control
    session_id        TEXT,
    skill_lib_commit  TEXT,
    no_skills_flag    INTEGER,
    gates_passed      INTEGER,
    gate_retries_json TEXT,
    waiver_count      INTEGER,
    input_tokens      INTEGER,
    output_tokens     INTEGER,
    aux_tokens        INTEGER,
    cost_usd          REAL,
    llm_judge_json    TEXT,
    started_at        TEXT,
    ended_at          TEXT
);
"""

# v2:M3 workflow 引擎(E10:编排状态走落盘链)
_DDL_V2 = """
CREATE TABLE IF NOT EXISTS workflow_runs (
    run_id            TEXT PRIMARY KEY,
    session_id        TEXT NOT NULL,
    workflow_name     TEXT NOT NULL,
    current_phase     TEXT,
    phase_states_json TEXT NOT NULL DEFAULT '{}',
    status            TEXT NOT NULL,   -- IN_PROGRESS/PAUSED_GATE_BLOCK/PAUSED_USER_DECISION/PAUSED_ERROR/COMPLETED/ABORTED
    task_json         TEXT,
    workdir           TEXT,
    started_at        TEXT NOT NULL,
    ended_at          TEXT
);

CREATE TABLE IF NOT EXISTS workflow_checkpoints (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id              TEXT NOT NULL REFERENCES workflow_runs(run_id),
    phase_id            TEXT NOT NULL,
    artifact_paths_json TEXT NOT NULL,
    gate_result_json    TEXT,
    created_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_workflow_checkpoints_run
    ON workflow_checkpoints(run_id, id);
"""


def migrate(conn: sqlite3.Connection) -> int:
    """幂等迁移:v1 地基 + 逐版本升级。返回当前版本。"""
    conn.executescript(_DDL)
    row = conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
    if row is None:
        conn.execute("INSERT INTO schema_version(version) VALUES (1)")
        current = 1
    else:
        current = row[0]

    if current < 2:
        conn.executescript(_DDL_V2)
        conn.execute("UPDATE schema_version SET version=2")
        current = 2
    if current != SCHEMA_VERSION:
        raise RuntimeError(f"不支持的 schema 版本:{current}(当前代码 {SCHEMA_VERSION})")
    conn.commit()
    return SCHEMA_VERSION
