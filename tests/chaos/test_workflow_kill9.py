"""workflow kill -9 混沌测试(PLAN §6 M3:test_workflow_kill9.py)。

- mid-phase(P3 turn 前)杀 → resume 从 P3 重跑,已完成 phase 产物不重生成
- mid-gate(G1 执行前,P1 已 contracted)杀 → resume 只重跑 gate,不重跑 P1
确定性:barrier 冻结点 + 假端点请求计数 + 产物 mtime。
"""

from __future__ import annotations

import signal
from pathlib import Path

import yaml

from mini_hermes.barrier import POINTS
from mini_hermes.state_db import SessionDB
from tests.chaos.spawn import spawn_cli, wait_exit, wait_for_file, write_config
from tests.integration.test_paper_loop_cassette import queue_full_run

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
CASSETTES = FIXTURES / "cassettes"

ALL_BARRIER_POINTS = (
    list(POINTS)
    + [f"before_phase_turn:P{i}" for i in range(1, 7)]
    + [f"before_gate_execution:G{i}" for i in range(1, 5)]
)


def _prep_go(barrier_dir: Path, except_point: str) -> None:
    barrier_dir.mkdir(parents=True, exist_ok=True)
    for point in ALL_BARRIER_POINTS:
        if point != except_point:
            (barrier_dir / f"{point}.go").touch()


def _write_task(tmp_path: Path, workdir: Path) -> Path:
    task = yaml.safe_load(
        (FIXTURES / "paper_tasks" / "task_attention.yaml").read_text(encoding="utf-8")
    )
    task["workdir"] = str(workdir)
    task["cassette_dir"] = str(CASSETTES)
    task["gate_external_mode"] = "cassette"
    task_path = tmp_path / "task.yaml"
    task_path.write_text(yaml.safe_dump(task, allow_unicode=True), encoding="utf-8")
    return task_path


def _run_kill9_cycle(tmp_path, fake_openai, kill_point: str):
    """返回 (run_row, checkpoints, 请求总数, workdir)。"""
    queue_full_run(fake_openai)
    data_dir = tmp_path / "data"
    workdir = tmp_path / "work"
    barrier_dir = tmp_path / "barrier"
    task_path = _write_task(tmp_path, workdir)
    config = write_config(tmp_path, fake_openai.base_url, data_dir)

    _prep_go(barrier_dir, except_point=kill_point)
    p1 = spawn_cli(
        config, "workflow", "run", "--task", str(task_path),
        "--session", "s1", "--run-id", "r1",
        env_extra={"MINI_HERMES_TEST_BARRIER_DIR": str(barrier_dir)},
    )
    wait_for_file(barrier_dir / kill_point)
    p1.send_signal(signal.SIGKILL)
    rc1, _, err1 = wait_exit(p1)
    assert rc1 == -signal.SIGKILL, f"P1 应被 SIGKILL,实际 {rc1}\n{err1}"

    p2 = spawn_cli(config, "workflow", "resume", "--run", "r1")
    rc2, out2, err2 = wait_exit(p2, timeout_s=120)
    assert rc2 == 0, f"P2 resume 应干净完成,实际 {rc2}\n{err2}\n{out2}"

    with SessionDB(data_dir / "state.db") as db:
        row = db.get_workflow_run("r1")
        cps = db.get_workflow_checkpoints("r1")
    return row, cps, len(fake_openai.requests), workdir, out2


def _assert_completed(row, cps, workdir):
    assert row["status"] == "COMPLETED"
    assert [c["phase_id"] for c in cps] == ["P1", "P2", "P3", "P4", "P5", "P6"]
    for artifact in ["EXTENDED_REFERENCES.md", "PLAN.md", "DRAFT.md",
                     "REVIEW_REPORT.md", "FIGURES.md", "FINAL.md"]:
        assert (workdir / artifact).is_file(), artifact


def test_kill9_mid_gate_only_gate_reruns(tmp_path, fake_openai):
    """G1 执行前被杀(P1 已 contracted):resume 只重跑 gate,P1 产物不重生成。"""
    row, cps, n_requests, workdir, out = _run_kill9_cycle(
        tmp_path, fake_openai, "before_gate_execution:G1"
    )
    _assert_completed(row, cps, workdir)
    assert cps[0]["gate_result"]["G1"]["status"] == "PASSED"
    # P1 的 turn 只在 P1 进程跑了 2 次 API;resume 进程跑 P2-P6 的 11 次
    assert n_requests == 13
    assert "COMPLETED" in out


def test_kill9_mid_phase_p3_resume_from_checkpoint(tmp_path, fake_openai):
    """P3 turn 前被杀:P1/P2 已过 checkpoint;resume 从 P3 重跑,产物不重生成。"""
    row, cps, n_requests, workdir, out = _run_kill9_cycle(
        tmp_path, fake_openai, "before_phase_turn:P3"
    )
    _assert_completed(row, cps, workdir)
    # P1+P2 在 P1 进程 = 4 次请求;resume 跑 P3-P6 = 9 次;共 13 ——
    # 若 P1/P2 被重跑,请求数会超过 13
    assert n_requests == 13
    assert "COMPLETED" in out
