"""Review fork 单元测试:触发规则、串行队列、畸形决策丢弃、白名单、
证据关联数学、promotion/demotion 阈值边界、backfill(mock aux)。
"""

from __future__ import annotations

import json

import pytest

from mini_hermes.review_fork import (
    DecisionParseError,
    ReviewFork,
    apply_decision,
    apply_gate_evidence,
    check_transitions,
    parse_decision,
    run_backfill,
)
from mini_hermes.skills_lib import SkillLibrary
from mini_hermes.state_db import SessionDB


@pytest.fixture()
def env(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    db.create_session("s1")
    lib = SkillLibrary(tmp_path / "skill-lib")
    lib.create("existing-skill", "已有技能", "旧正文",
               status="probation", origin="learned")
    yield db, lib, tmp_path
    db.close()


def _fork(db, lib, tmp_path, raw_decision):
    calls = []
    fork = ReviewFork(db, lib, lambda p: calls.append(p) or raw_decision,
                      data_dir=tmp_path)
    return fork, calls


# ------------------------------------------------------------- 决策解析


def test_parse_decision_ok():
    actions = parse_decision('{"actions": [{"type": "none"}]}')
    assert actions == [{"type": "none"}]


def test_parse_decision_tolerates_fence():
    actions = parse_decision('```json\n{"actions": [{"type": "none"}]}\n```')
    assert actions[0]["type"] == "none"


def test_parse_decision_rejects_malformed():
    with pytest.raises(DecisionParseError):
        parse_decision("这不是 JSON")
    with pytest.raises(DecisionParseError):
        parse_decision('{"no_actions": []}')
    with pytest.raises(DecisionParseError):
        parse_decision('{"actions": [{"type": "delete_everything"}]}')


# ------------------------------------------------------------- 白名单 applier


def test_applier_create_lands_probation(env, tmp_path):
    _, lib, _ = env
    applied = apply_decision(
        [{"type": "create_skill", "name": "learned-one",
          "description": "复盘沉淀", "content": "正文", "rationale": "G1 反复失败"}],
        lib, tmp_path,
    )
    skill = lib.load("learned-one")
    assert skill.status == "probation"
    assert skill.origin == "learned"
    assert applied == ["create_skill:learned-one"]
    # rationale 进 commit body
    import subprocess

    body = subprocess.run(["git", "log", "-1", "--format=%B"], cwd=lib.root,
                          capture_output=True, text=True).stdout
    assert "G1 反复失败" in body


def test_applier_refuses_out_of_scope(env, tmp_path):
    """deprecate/删除类动作在 review 路径不可达(parse 层拦截)。"""
    _, lib, _ = env
    with pytest.raises(DecisionParseError):
        parse_decision('{"actions": [{"type": "deprecate_skill", '
                       '"name": "existing-skill"}]}')
    assert lib.load("existing-skill").status == "probation"  # 未被碰


def test_applier_update_and_memory(env, tmp_path):
    _, lib, _ = env
    apply_decision(
        [{"type": "update_skill", "name": "existing-skill", "content": "新正文"},
         {"type": "add_memory", "content": "用户讨厌冗长输出"}],
        lib, tmp_path,
    )
    assert "新正文" in lib.load("existing-skill").body
    mem = (tmp_path / "memories" / "MEMORY.md").read_text(encoding="utf-8")
    assert "§ 用户讨厌冗长输出" in mem


# ------------------------------------------------------------- 队列与触发


def test_queue_serialized_fifo(env, tmp_path):
    db, lib, _ = env
    decisions = iter([
        '{"actions": [{"type": "create_skill", "name": "first", "content": "1"}]}',
        '{"actions": [{"type": "create_skill", "name": "second", "content": "2"}]}',
    ])
    fork = ReviewFork(db, lib, lambda p: next(decisions), data_dir=tmp_path)
    fork.submit("phase_end", "s1", phase_id="P1")
    fork.submit("gate_failure", "s1", phase_id="P1")
    assert fork._queue.qsize() == 2
    fork.drain()  # 同步排空,顺序处理
    assert lib.load("first") and lib.load("second")
    assert fork._queue.empty()


def test_malformed_decision_dropped_safely(env, tmp_path):
    db, lib, _ = env
    fork, calls = _fork(db, lib, tmp_path, "LLM 胡言乱语")
    fork.submit("phase_end", "s1")
    fork.drain()  # 不抛、不改库
    assert len(calls) == 1
    assert lib.build_index().count("learned") == 0


def test_packet_contains_context(env, tmp_path):
    db, lib, _ = env
    db.append_message("s1", {"role": "user", "content": "上下文消息甲"})
    fork, calls = _fork(db, lib, tmp_path, '{"actions": [{"type": "none"}]}')
    fork.submit("gate_failure", "s1", phase_id="P3",
                gate_results=[{"gate_id": "G3", "status": "FAILED"}])
    fork.drain()
    packet = calls[0]
    assert "上下文消息甲" in packet
    assert "G3" in packet and "FAILED" in packet
    assert "existing-skill" in packet  # 当前 INDEX 进了包


# ------------------------------------------------------------- 证据关联数学


def _usage(db, session, skill, phase):
    db.add_skill_usage(session, skill, phase=phase)


def test_evidence_watermark_increments_once(env):
    db, lib, _ = env
    _usage(db, "s1", "existing-skill", "P1")
    wm, skills = apply_gate_evidence(db, lib, "s1", "P1", "positive", 0)
    assert skills == ["existing-skill"]
    ev = lib.load("existing-skill").evidence
    assert ev["uses"] == 1 and ev["positive"] == 1
    # 同 watermark 再调:不重复计数(resume 重放安全)
    wm2, skills2 = apply_gate_evidence(db, lib, "s1", "P1", "positive", wm)
    assert wm2 == wm and skills2 == []
    assert lib.load("existing-skill").evidence["uses"] == 1
    # 新 usage 行:只增量
    _usage(db, "s1", "existing-skill", "P1")
    apply_gate_evidence(db, lib, "s1", "P1", "negative", wm)
    ev = lib.load("existing-skill").evidence
    assert ev["uses"] == 2 and ev["negative"] == 1


# ---------------------------------------------------- promotion/demotion 边界


def test_promotion_threshold_boundary(env):
    db, lib, _ = env
    lib.update("existing-skill",
               evidence_delta={"uses": 2, "positive": 1, "negative": 1})
    # uses=2 < 3:不转
    assert check_transitions(db, lib, "existing-skill", promotion_uses=3) is None
    lib.update("existing-skill", evidence_delta={"uses": 1, "positive": 1})
    # uses=3, ratio 2/3 ≥ 0.6:转正
    assert check_transitions(db, lib, "existing-skill",
                             promotion_uses=3, promotion_ratio=0.6) == "promoted"
    assert lib.load("existing-skill").status == "active"


def test_demotion_writes_pending_not_deprecated(env):
    db, lib, _ = env
    lib.update("existing-skill",
               evidence_delta={"uses": 3, "positive": 1, "negative": 2})
    assert check_transitions(db, lib, "existing-skill") == "demotion_pending"
    # 只写 PENDING,status 不变(不变量 7)
    assert lib.load("existing-skill").status == "probation"
    record = db.meta_get("pending_deprecation:existing-skill")
    assert "证据恶化" in record


def test_no_demotion_when_tied(env):
    db, lib, _ = env
    lib.update("existing-skill",
               evidence_delta={"uses": 4, "positive": 2, "negative": 2})
    assert check_transitions(db, lib, "existing-skill") is None


# ------------------------------------------------------------------ backfill


def test_backfill_creates_backfilled_skills(env, tmp_path):
    _, lib, _ = env
    src = tmp_path / "historical"
    src.mkdir()
    (src / "gate_1_report.json").write_text('{"gate_id": "G1", "status": "PASSED"}')
    (src / "EXTENDED_REFERENCES.md").write_text("[1] X (2024). T. *NeurIPS*. arXiv:1\n")
    decision = json.dumps({"actions": [{
        "type": "create_skill", "name": "bf-skill",
        "description": "回填技能", "content": "正文",
        "rationale": "历史经验"}]})
    created = run_backfill(lib, lambda p: decision, [str(src)])
    assert created == ["bf-skill"]
    skill = lib.load("bf-skill")
    assert skill.status == "backfilled"
    assert skill.origin == "backfilled"


def test_backfill_missing_source_tolerated(env, tmp_path):
    _, lib, _ = env
    assert run_backfill(lib, lambda p: "{}", ["/nonexistent"]) == []
