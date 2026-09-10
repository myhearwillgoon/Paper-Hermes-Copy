"""SessionDB —— Hermes 骨架组件 #1:SQLite WAL append-only writer。

- WAL 模式,一进程一连接
- append-only messages(一行一消息,ORDER BY rowid —— 绝不按 timestamp)
- busy-retry + jitter + 时间预算(常规 20s / transcript 写 60s,对标 Hermes)
- compression_locks 租约:过期回收 + 持有方 PID 死亡回收
"""

from __future__ import annotations

import json
import os
import random
import re
import socket
import sqlite3
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from . import state_schema

ROUTINE_BUDGET_S = 20.0
TRANSCRIPT_BUDGET_S = 60.0


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


class BusyTimeoutError(RuntimeError):
    """busy-retry 时间预算耗尽。"""


class SessionDB:
    """一进程一连接;所有写操作走 busy-retry。"""

    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(self.path), timeout=0.25, isolation_level=None, check_same_thread=False
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        state_schema.migrate(self._conn)
        self._fts_backfill_if_needed()

    def _fts_backfill_if_needed(self) -> None:
        """messages_fts 与 messages 行数不一致 → 全量 rebuild(崩溃/旧库缝隙)。"""
        msgs = self._conn.execute("SELECT COUNT(*) c FROM messages").fetchone()["c"]
        fts = self._conn.execute("SELECT COUNT(*) c FROM messages_fts").fetchone()["c"]
        if msgs != fts:
            self._conn.execute("INSERT INTO messages_fts(messages_fts) VALUES('rebuild')")

    # -- skill_usage(不变量 3 的数据路径)----------------------------------------

    def add_skill_usage(self, session_id: str, skill_name: str,
                        phase: Optional[str] = None) -> None:
        def op():
            self._conn.execute(
                "INSERT INTO skill_usage(session_id, skill_name, phase, loaded_at)"
                " VALUES (?, ?, ?, ?)",
                (session_id, skill_name, phase, _utcnow()),
            )

        self._with_busy_retry(op, ROUTINE_BUDGET_S)

    def get_skill_usage(self, session_id: Optional[str] = None) -> list[dict]:
        sql = "SELECT * FROM skill_usage"
        params: tuple = ()
        if session_id:
            sql += " WHERE session_id=?"
            params = (session_id,)
        rows = self._with_busy_retry(
            lambda: self._conn.execute(sql, params).fetchall(), ROUTINE_BUDGET_S
        )
        return [dict(r) for r in rows]

    # -- session_search 支撑 ----------------------------------------------------

    def fts_search(self, query: str, limit: int = 20) -> list[dict]:
        """FTS5 检索:返回 session_id/rowid/片段。

        查询逐词加前缀通配("word"*):FTS5 默认分词器不切 CJK,
        中文长串是单 token,不加 * 无法匹配子串。
        """
        terms = [t for t in query.replace('"', " ").split() if t]
        if not terms:
            return []
        match = " ".join(f'"{t}"*' for t in terms)

        def op():
            return self._conn.execute(
                "SELECT m.session_id, m.id, m.role,"
                " snippet(messages_fts, 0, '[', ']', '…', 32) AS snip"
                " FROM messages_fts JOIN messages m ON m.id = messages_fts.rowid"
                " WHERE messages_fts MATCH ? ORDER BY rank LIMIT ?",
                (match, limit),
            ).fetchall()

        rows = self._with_busy_retry(op, ROUTINE_BUDGET_S)
        return [{"session_id": r["session_id"], "rowid": r["id"],
                 "role": r["role"], "snippet": r["snip"]} for r in rows]

    def scroll_messages(self, rowid: int, before: int = 2, after: int = 2) -> list[dict]:
        rows = self._with_busy_retry(
            lambda: self._conn.execute(
                "SELECT * FROM messages WHERE id BETWEEN ? AND ? ORDER BY rowid",
                (rowid - before, rowid + after),
            ).fetchall(),
            ROUTINE_BUDGET_S,
        )
        return [self._row_to_msg(r) for r in rows]

    def recent_sessions(self, limit: int = 10) -> list[dict]:
        rows = self._with_busy_retry(
            lambda: self._conn.execute(
                "SELECT id, source, model, started_at, ended_at, message_count"
                " FROM sessions ORDER BY started_at DESC LIMIT ?",
                (limit,),
            ).fetchall(),
            ROUTINE_BUDGET_S,
        )
        return [dict(r) for r in rows]

    # -- 基础设施 -----------------------------------------------------------

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "SessionDB":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def _with_busy_retry(self, fn, budget_s: float):
        """sqlite busy/locked 重试:指数退避 + jitter,预算耗尽抛 BusyTimeoutError。"""
        deadline = time.monotonic() + budget_s
        delay = 0.01
        while True:
            try:
                return fn()
            except sqlite3.OperationalError as e:
                if "locked" not in str(e).lower() and "busy" not in str(e).lower():
                    raise
                if time.monotonic() >= deadline:
                    raise BusyTimeoutError(
                        f"sqlite busy 超过预算 {budget_s}s:{e}"
                    ) from e
                time.sleep(delay + random.uniform(0, delay))
                delay = min(delay * 2, 1.0)

    # -- sessions -------------------------------------------------------------

    def create_session(
        self,
        session_id: Optional[str] = None,
        *,
        source: str = "cli",
        model: Optional[str] = None,
        system_prompt: Optional[str] = None,
        cwd: Optional[str] = None,
        title: Optional[str] = None,
    ) -> str:
        sid = session_id or uuid.uuid4().hex[:16]

        def op():
            self._conn.execute(
                "INSERT OR IGNORE INTO sessions"
                "(id, source, model, system_prompt, started_at, cwd, title)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (sid, source, model, system_prompt, _utcnow(), cwd, title),
            )

        self._with_busy_retry(op, ROUTINE_BUDGET_S)
        return sid

    def end_session(self, session_id: str, end_reason: str) -> None:
        def op():
            self._conn.execute(
                "UPDATE sessions SET ended_at=?, end_reason=? WHERE id=?",
                (_utcnow(), end_reason, session_id),
            )

        self._with_busy_retry(op, ROUTINE_BUDGET_S)

    def add_token_usage(self, session_id: str, input_tokens: int, output_tokens: int) -> None:
        def op():
            self._conn.execute(
                "UPDATE sessions SET input_tokens=input_tokens+?,"
                " output_tokens=output_tokens+? WHERE id=?",
                (input_tokens, output_tokens, session_id),
            )

        self._with_busy_retry(op, ROUTINE_BUDGET_S)

    # -- messages(append-only)-----------------------------------------------

    def append_message(self, session_id: str, msg: dict) -> int:
        """追加一行消息,返回 rowid。调用方负责去重(见 persistence.flush)。"""
        row = (
            session_id,
            msg.get("role"),
            msg.get("content"),
            msg.get("tool_call_id"),
            json.dumps(msg["tool_calls"], ensure_ascii=False) if msg.get("tool_calls") else None,
            msg.get("name") or msg.get("tool_name"),
            _utcnow(),
            msg.get("token_count"),
            msg.get("finish_reason"),
            msg.get("reasoning"),
            1,
            0,
            json.dumps(
                {k: v for k, v in msg.items() if not k.startswith("_")},
                ensure_ascii=False,
            ),
        )

        def op():
            cur = self._conn.execute(
                "INSERT INTO messages"
                "(session_id, role, content, tool_call_id, tool_calls, tool_name,"
                " timestamp, token_count, finish_reason, reasoning, active, compacted,"
                " api_content) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                row,
            )
            self._conn.execute(
                "UPDATE sessions SET message_count=message_count+1 WHERE id=?",
                (session_id,),
            )
            return cur.lastrowid

        return self._with_busy_retry(op, TRANSCRIPT_BUDGET_S)

    def get_messages(self, session_id: str, *, active_only: bool = True,
                     include_compacted: bool = False) -> list[dict]:
        """按 rowid 顺序重读(绝不用 timestamp 排序)。

        active_only 过滤 active=1;compacted=1 的行默认也排除(压缩后不进
        上下文),include_compacted=True 时返回全部(审计/混沌测试用)。
        """
        sql = "SELECT * FROM messages WHERE session_id=?"
        if active_only:
            sql += " AND active=1"
        if not include_compacted:
            sql += " AND compacted=0"
        sql += " ORDER BY rowid"
        rows = self._with_busy_retry(
            lambda: self._conn.execute(sql, (session_id,)).fetchall(), ROUTINE_BUDGET_S
        )
        return self._logical_order([self._row_to_msg(r) for r in rows])

    # 摘要消息的逻辑位置:插在被覆盖中段的最后一个 rowid 之后(物理 rowid 在末尾,
    # 直接按 rowid 排序会把摘要排到尾部之后,破坏 head→summary→tail 语义)
    _SUMMARY_COVERS = re.compile(r"^\[COMPACTION SUMMARY v\d+ covers_to=(\d+)\]")

    @classmethod
    def _logical_order(cls, msgs: list[dict]) -> list[dict]:
        def key(m: dict) -> float:
            match = cls._SUMMARY_COVERS.match(m.get("content") or "")
            if match:
                return int(match.group(1)) + 0.5
            return float(m["_rowid"])

        return sorted(msgs, key=key)

    @staticmethod
    def _row_to_msg(row: sqlite3.Row) -> dict:
        msg: dict = {
            "_rowid": row["id"],
            "_db_persisted": True,
            "_compacted": row["compacted"],
            "role": row["role"],
            "content": row["content"],
        }
        if row["tool_calls"]:
            msg["tool_calls"] = json.loads(row["tool_calls"])
        if row["tool_call_id"]:
            msg["tool_call_id"] = row["tool_call_id"]
        if row["tool_name"]:
            msg["name"] = row["tool_name"]
        if row["finish_reason"]:
            msg["finish_reason"] = row["finish_reason"]
        return msg

    # -- state_meta -----------------------------------------------------------

    def meta_set(self, key: str, value: str) -> None:
        self._with_busy_retry(
            lambda: self._conn.execute(
                "INSERT INTO state_meta(key, value) VALUES (?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            ),
            ROUTINE_BUDGET_S,
        )

    def meta_get(self, key: str) -> Optional[str]:
        row = self._with_busy_retry(
            lambda: self._conn.execute(
                "SELECT value FROM state_meta WHERE key=?", (key,)
            ).fetchone(),
            ROUTINE_BUDGET_S,
        )
        return row["value"] if row else None

    def meta_delete_prefix(self, prefix: str) -> int:
        def op():
            cur = self._conn.execute("DELETE FROM state_meta WHERE key LIKE ?", (prefix + "%",))
            return cur.rowcount

        return self._with_busy_retry(op, ROUTINE_BUDGET_S)

    # -- compression_locks 租约 ------------------------------------------------

    def acquire_compression_lock(self, session_id: str, ttl_s: float = 300.0) -> Optional[str]:
        """尝试获取租约。成功返回 holder 字符串;被活跃持有方占用返回 None。

        holder 格式 `<pid>@<host>:<nonce>`;回收条件:租约过期,或持有方
        PID 已死(崩溃残留)。
        """
        holder = f"{os.getpid()}@{socket.gethostname()}:{uuid.uuid4().hex[:8]}"
        now = datetime.now(timezone.utc)
        expires = now + timedelta(seconds=ttl_s)

        def op():
            row = self._conn.execute(
                "SELECT holder, expires_at FROM compression_locks WHERE session_id=?",
                (session_id,),
            ).fetchone()
            if row is not None:
                expired = datetime.fromisoformat(row["expires_at"]) <= now
                pid_m = re.match(r"(\d+)", row["holder"])
                holder_pid = int(pid_m.group(1)) if pid_m else -1
                if not expired and _pid_alive(holder_pid):
                    return None  # 活跃持有方占用
                self._conn.execute(
                    "DELETE FROM compression_locks WHERE session_id=?", (session_id,)
                )
            self._conn.execute(
                "INSERT INTO compression_locks(session_id, holder, acquired_at, expires_at)"
                " VALUES (?, ?, ?, ?)",
                (session_id, holder, now.isoformat(), expires.isoformat()),
            )
            return holder

        return self._with_busy_retry(op, ROUTINE_BUDGET_S)

    def release_compression_lock(self, session_id: str, holder: str) -> bool:
        """释放租约(仅持有方本人)。返回是否成功释放。"""

        def op():
            cur = self._conn.execute(
                "DELETE FROM compression_locks WHERE session_id=? AND holder=?",
                (session_id, holder),
            )
            return cur.rowcount > 0

        return self._with_busy_retry(op, ROUTINE_BUDGET_S)

    # -- workflow_runs / workflow_checkpoints(M3,E10)-------------------------

    def create_workflow_run(
        self,
        run_id: str,
        *,
        session_id: str,
        workflow_name: str,
        task: dict,
        workdir: str,
    ) -> None:
        def op():
            self._conn.execute(
                "INSERT INTO workflow_runs"
                "(run_id, session_id, workflow_name, current_phase,"
                " phase_states_json, status, task_json, workdir, started_at)"
                " VALUES (?, ?, ?, NULL, '{}', 'IN_PROGRESS', ?, ?, ?)",
                (run_id, session_id, workflow_name,
                 json.dumps(task, ensure_ascii=False), workdir, _utcnow()),
            )

        self._with_busy_retry(op, ROUTINE_BUDGET_S)

    def update_workflow_run(self, run_id: str, **fields) -> None:
        """更新 run 行;fields 限 current_phase/phase_states/status/ended_at。"""
        allowed = {"current_phase", "phase_states", "status", "ended_at"}
        bad = set(fields) - allowed
        if bad:
            raise ValueError(f"非法 workflow_run 字段:{bad}")
        cols, vals = [], []
        for k, v in fields.items():
            col = "phase_states_json" if k == "phase_states" else k
            cols.append(f"{col}=?")
            vals.append(json.dumps(v, ensure_ascii=False) if k == "phase_states" else v)
        vals.append(run_id)

        def op():
            self._conn.execute(
                f"UPDATE workflow_runs SET {', '.join(cols)} WHERE run_id=?", vals
            )

        self._with_busy_retry(op, ROUTINE_BUDGET_S)

    def get_workflow_run(self, run_id: str) -> Optional[dict]:
        row = self._with_busy_retry(
            lambda: self._conn.execute(
                "SELECT * FROM workflow_runs WHERE run_id=?", (run_id,)
            ).fetchone(),
            ROUTINE_BUDGET_S,
        )
        if row is None:
            return None
        return {
            "run_id": row["run_id"],
            "session_id": row["session_id"],
            "workflow_name": row["workflow_name"],
            "current_phase": row["current_phase"],
            "phase_states": json.loads(row["phase_states_json"]),
            "status": row["status"],
            "task": json.loads(row["task_json"]) if row["task_json"] else {},
            "workdir": row["workdir"],
            "started_at": row["started_at"],
            "ended_at": row["ended_at"],
        }

    def add_workflow_checkpoint(
        self,
        run_id: str,
        phase_id: str,
        artifact_paths: list[str],
        gate_result: Optional[dict] = None,
    ) -> None:
        def op():
            self._conn.execute(
                "INSERT INTO workflow_checkpoints"
                "(run_id, phase_id, artifact_paths_json, gate_result_json, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (run_id, phase_id,
                 json.dumps(artifact_paths, ensure_ascii=False),
                 json.dumps(gate_result, ensure_ascii=False) if gate_result else None,
                 _utcnow()),
            )

        self._with_busy_retry(op, ROUTINE_BUDGET_S)

    def get_workflow_checkpoints(self, run_id: str) -> list[dict]:
        rows = self._with_busy_retry(
            lambda: self._conn.execute(
                "SELECT * FROM workflow_checkpoints WHERE run_id=? ORDER BY id",
                (run_id,),
            ).fetchall(),
            ROUTINE_BUDGET_S,
        )
        return [
            {
                "phase_id": r["phase_id"],
                "artifact_paths": json.loads(r["artifact_paths_json"]),
                "gate_result": json.loads(r["gate_result_json"]) if r["gate_result_json"] else None,
                "created_at": r["created_at"],
            }
            for r in rows
        ]

    # -- 压缩(M4,不变量 5)------------------------------------------------------

    def apply_compaction(
        self,
        session_id: str,
        middle_rowids: list[int],
        summary_msg: dict,
        hook: Any = None,
    ) -> int:
        """单事务:mark middle compacted=1 + 插入 summary 行。返回 summary rowid。

        崩溃安全:事务前死 = 无变化;事务中死 = sqlite 回滚。
        调用方必须持有 compression_locks 租约(不变量 5)。
        hook: 仅测试用,在 UPDATE 之后 INSERT 之前调用(混沌杀点)。
        """

        def op() -> int:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                self._conn.executemany(
                    "UPDATE messages SET compacted=1 WHERE id=? AND session_id=?",
                    [(r, session_id) for r in middle_rowids],
                )
                if hook is not None:
                    hook()
                cur = self._conn.execute(
                    "INSERT INTO messages"
                    "(session_id, role, content, timestamp, active, compacted, api_content)"
                    " VALUES (?, ?, ?, ?, 1, 0, ?)",
                    (
                        session_id,
                        summary_msg.get("role"),
                        summary_msg.get("content"),
                        _utcnow(),
                        json.dumps(summary_msg, ensure_ascii=False),
                    ),
                )
                self._conn.execute(
                    "UPDATE sessions SET message_count=message_count+1 WHERE id=?",
                    (session_id,),
                )
                self._conn.execute("COMMIT")
                return cur.lastrowid
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise

        return self._with_busy_retry(op, TRANSCRIPT_BUDGET_S)

    def count_summaries(self, session_id: str) -> int:
        row = self._with_busy_retry(
            lambda: self._conn.execute(
                "SELECT COUNT(*) c FROM messages"
                " WHERE session_id=? AND content LIKE '[COMPACTION SUMMARY%'",
                (session_id,),
            ).fetchone(),
            ROUTINE_BUDGET_S,
        )
        return row["c"]

    # -- experiment_runs(M6,PLAN §4)-------------------------------------------

    def add_experiment_run(self, record: dict) -> None:
        """写入一条实验记录。record 键对应 §4 experiment_runs 列。"""
        cols = ("id", "task_id", "arm", "session_id", "skill_lib_commit",
                "no_skills_flag", "gates_passed", "gate_retries_json",
                "waiver_count", "input_tokens", "output_tokens", "aux_tokens",
                "cost_usd", "llm_judge_json", "started_at", "ended_at")
        vals = [record.get(c) for c in cols]
        for json_col, key in (("gate_retries_json", "gate_retries_json"),
                              ("llm_judge_json", "llm_judge_json")):
            if isinstance(record.get(key), (dict, list)):
                vals[cols.index(json_col)] = json.dumps(record[key], ensure_ascii=False)

        def op():
            self._conn.execute(
                f"INSERT OR REPLACE INTO experiment_runs({', '.join(cols)})"
                f" VALUES ({', '.join('?' * len(cols))})",
                vals,
            )

        self._with_busy_retry(op, ROUTINE_BUDGET_S)

    def get_experiment_runs(self, arm: Optional[str] = None) -> list[dict]:
        sql = "SELECT * FROM experiment_runs"
        params: tuple = ()
        if arm:
            sql += " WHERE arm=?"
            params = (arm,)
        rows = self._with_busy_retry(
            lambda: self._conn.execute(sql + " ORDER BY started_at", params).fetchall(),
            ROUTINE_BUDGET_S,
        )
        return [dict(r) for r in rows]
