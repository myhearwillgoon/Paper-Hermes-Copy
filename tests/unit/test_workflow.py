"""workflow 引擎单元测试(PLAN §6 M3:test_workflow.py)。

状态机:advance / gate-fail 重试 / waiver / 异常隔离 / checkpoint 写序。
用 StubRuntime(不写文件由脚本控制)+ 真 SessionDB + 真 Registry。
"""

from __future__ import annotations

import json

import pytest
import yaml

from mini_hermes.state_db import SessionDB
from mini_hermes.tools.registry import Registry
from mini_hermes.workflow import engine as eng
from mini_hermes.workflow.definition import load_workflow
from mini_hermes.workflow.engine import WorkflowEngine


class StubRuntime:
    """按脚本行动:每个元素是 None(只记录)| str(写到该文件名)| Exception。"""

    def __init__(self, workdir, script):
        self.session_id = "s1"
        self.tools = None
        self.workdir = workdir
        self.script = list(script)
        self.prompts: list[str] = []
        self.needs_continuation = False

    def run_turn(self, prompt: str) -> str:
        self.prompts.append(prompt)
        action = self.script.pop(0) if self.script else None
        if isinstance(action, Exception):
            raise action
        if isinstance(action, str):
            (self.workdir / action).write_text(f"content of {action}", encoding="utf-8")
        return "done"

    def resume(self):
        return False

    def continue_run(self):
        return ""


WF = {
    "name": "mini",
    "phases": [
        {"id": "A", "name": "A", "prompt_template": "pA.md",
         "toolset": ["echo"], "output_contract": {"path": "A.md"}, "gate": ["GX"]},
        {"id": "B", "name": "B", "prompt_template": "pB.md",
         "toolset": ["echo"], "output_contract": {"path": "B.md"}},
    ],
    "gates": {"GX": {"tool": "fake_gate", "escalation": "BLOCK", "max_retries": 2}},
}


@pytest.fixture()
def env(tmp_path):
    (tmp_path / "pA.md").write_text("do A", encoding="utf-8")
    (tmp_path / "pB.md").write_text("do B", encoding="utf-8")
    wf_path = tmp_path / "wf.yaml"
    wf_path.write_text(yaml.safe_dump(WF), encoding="utf-8")
    definition, def_dir = load_workflow(wf_path)
    db = SessionDB(tmp_path / "state.db")
    db.create_session("s1")
    workdir = tmp_path / "work"
    workdir.mkdir()
    yield db, definition, def_dir, workdir
    db.close()


def _registry(gate_results: list[str]) -> Registry:
    reg = Registry()
    reg.register("echo", "e", {"type": "object"}, lambda a, c: "ok")
    results = list(gate_results)

    def fake_gate(args, ctx):
        status = results.pop(0) if results else "PASSED"
        return json.dumps({"gate_id": "GX", "gate_name": "FAKE", "status": status,
                           "criteria": [{"name": "c", "threshold": "t",
                                         "actual": "a", "passed": status == "PASSED"}],
                           "violations": []})

    reg.register("fake_gate", "g", {"type": "object"}, fake_gate)
    return reg


def _engine(db, definition, def_dir, workdir, script, registry, **kw):
    rt = StubRuntime(workdir, script)
    engine = WorkflowEngine.start(db, definition, def_dir, rt, registry, workdir,
                                  {"section": "t"}, run_id="r1", **kw)
    return engine, rt


def test_advance_and_checkpoints(env):
    db, definition, def_dir, workdir = env
    engine, rt = _engine(db, definition, def_dir, workdir,
                         ["A.md", "B.md"], _registry(["PASSED"]))
    assert engine.run() == eng.COMPLETED
    cps = db.get_workflow_checkpoints("r1")
    assert [c["phase_id"] for c in cps] == ["A", "B"]
    assert cps[0]["gate_result"]["GX"]["status"] == "PASSED"
    assert cps[1]["gate_result"] is None
    row = db.get_workflow_run("r1")
    assert row["phase_states"]["A"] == "passed"
    assert row["phase_states"]["B"] == "passed"
    assert row["ended_at"] is not None


def test_checkpoint_written_before_phase_turn(env):
    """写序:phase turn 执行时,run 行已是 running(checkpoint 先于执行)。"""
    db, definition, def_dir, workdir = env
    engine, rt = _engine(db, definition, def_dir, workdir,
                         ["A.md", "B.md"], _registry(["PASSED"]))
    seen = {}
    orig_turn = rt.run_turn

    def spy(prompt):
        row = db.get_workflow_run("r1")
        seen.setdefault("during_first_turn", (row["current_phase"],
                                              row["phase_states"].get("A")))
        return orig_turn(prompt)

    rt.run_turn = spy
    engine.run()
    assert seen["during_first_turn"] == ("A", "running")


def test_contract_repair_retry(env):
    """第一次没写契约文件 → repair prompt 重试 → 成功。"""
    db, definition, def_dir, workdir = env
    engine, rt = _engine(db, definition, def_dir, workdir,
                         [None, "A.md", "B.md"], _registry(["PASSED"]))
    assert engine.run() == eng.COMPLETED
    assert len(rt.prompts) == 3  # A 初次 + A repair + B
    assert "产出缺失" in rt.prompts[1]


def test_contract_permanently_missing_paused_error(env):
    db, definition, def_dir, workdir = env
    engine, _ = _engine(db, definition, def_dir, workdir, [], _registry([]))
    assert engine.run() == eng.PAUSED_ERROR
    row = db.get_workflow_run("r1")
    assert "error:A" in row["phase_states"]


def test_gate_fail_block_retries_phase(env):
    """BLOCK gate 第一次 FAIL → 重跑 phase → 第二次 PASS → 完成。"""
    db, definition, def_dir, workdir = env
    engine, rt = _engine(db, definition, def_dir, workdir,
                         ["A.md", "A.md", "B.md"], _registry(["FAILED", "PASSED"]))
    assert engine.run() == eng.COMPLETED
    assert len(rt.prompts) == 3  # A 跑了两次
    row = db.get_workflow_run("r1")
    assert row["phase_states"]["gate_attempts:A:GX"] == 1


def test_gate_block_exhausts_retries(env):
    db, definition, def_dir, workdir = env
    script = ["A.md", "A.md", "A.md", "B.md"]
    engine, _ = _engine(db, definition, def_dir, workdir, script,
                        _registry(["FAILED", "FAILED", "FAILED"]))
    assert engine.run() == eng.PAUSED_GATE_BLOCK
    row = db.get_workflow_run("r1")
    assert row["phase_states"]["gate_failure:GX"]["status"] == "FAILED"


def test_user_decision_pause_then_waive_resume(env, tmp_path):
    """USER_DECISION gate 失败:无决策人 → 暂停;waive 后 resume 自动跳过。"""
    db, definition, def_dir, workdir = env
    wf = dict(WF)
    wf["gates"] = {"GX": {"tool": "fake_gate", "escalation": "USER_DECISION"}}
    (tmp_path / "wf.yaml").write_text(yaml.safe_dump(wf), encoding="utf-8")
    definition, def_dir = load_workflow(tmp_path / "wf.yaml")

    engine, _ = _engine(db, definition, def_dir, workdir, ["A.md"],
                        _registry(["FAILED"]))
    assert engine.run() == eng.PAUSED_USER_DECISION

    # 用户豁免(waive CLI 的等价物)
    row = db.get_workflow_run("r1")
    states = row["phase_states"]
    states.setdefault("waivers", {})["GX"] = {"reason": "质量优先",
                                              "decided_by": "user"}
    db.update_workflow_run("r1", phase_states=states)

    # resume:gate 重跑仍 FAILED,但豁免生效 → WAIVED → 推进
    rt2 = StubRuntime(workdir, ["B.md"])
    engine2 = WorkflowEngine.resume(db, "r1", definition, def_dir, rt2,
                                    _registry(["FAILED"]))
    assert engine2.run() == eng.COMPLETED
    cps = db.get_workflow_checkpoints("r1")
    assert cps[0]["gate_result"]["GX"]["status"] == "WAIVED"
    assert cps[0]["gate_result"]["GX"]["waiver"]["reason"] == "质量优先"


def test_waiver_decider_inline(env, tmp_path):
    db, definition, def_dir, workdir = env
    wf = dict(WF)
    wf["gates"] = {"GX": {"tool": "fake_gate", "escalation": "USER_DECISION"}}
    (tmp_path / "wf.yaml").write_text(yaml.safe_dump(wf), encoding="utf-8")
    definition, def_dir = load_workflow(tmp_path / "wf.yaml")

    engine, _ = _engine(db, definition, def_dir, workdir, ["A.md", "B.md"],
                        _registry(["FAILED"]),
                        waiver_decider=lambda gate, result: "接受当前密度")
    assert engine.run() == eng.COMPLETED
    row = db.get_workflow_run("r1")
    assert row["phase_states"]["waivers"]["GX"]["reason"] == "接受当前密度"


def test_turn_exception_contained(env):
    """phase turn 抛异常:重试后 PAUSED_ERROR,进程不死。"""
    db, definition, def_dir, workdir = env
    engine, _ = _engine(db, definition, def_dir, workdir,
                        [RuntimeError("boom")] * 5, _registry([]))
    assert engine.run() == eng.PAUSED_ERROR
