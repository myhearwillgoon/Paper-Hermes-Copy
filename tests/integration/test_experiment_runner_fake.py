"""M6 假模式配对迷你实验(2 任务 × 2 臂)。

确定性:假 LLM + cassette;review fork 由 runner 在 run 结束后统一 drain。
断言:隔离(control 无 skills 块/不消费复盘场景)、experiment_runs 完整配对、
skill_lib_commit、盲评(judge 请求无臂标识)、报告与原始 JSONL。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from mini_hermes.config import load_config
from mini_hermes.experiments.analysis import render_report
from mini_hermes.experiments.runner import ExperimentRunner, load_tasks
from mini_hermes.state_db import SessionDB
from tests.fakes.openai_server import text_scenario
from tests.integration.test_paper_loop_cassette import queue_full_run

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
REPO_EXP = Path(__file__).resolve().parents[2] / "experiments"

DECISION_NONE = '{"actions": [{"type": "none"}]}'
JUDGE_SCORE = ('{"scores": {"academic_tone": 8, "argument_flow": 8, '
               '"disclosure": 9}, "overall": 8}')

TASKS = [
    {
        "id": "x01-attention",
        "section": "1.1 Attention Mechanisms",
        "title": "注意力机制",
        "year_threshold": 2017,
        "seed_papers": [{"title": "Attention Is All You Need", "year": 2017,
                         "source": "arXiv:1706.03762"}],
        "targets": {"words_range": [60, 600], "density_min": 0.5,
                    "subsection_min_citations": 1},
        "cassette_dir": str(FIXTURES / "cassettes"),
    },
    {
        "id": "x02-bert",
        "section": "2.1 Pre-training Objectives",
        "title": "预训练目标",
        "year_threshold": 2017,
        "seed_papers": [{"title": "BERT", "year": 2018,
                         "source": "arXiv:1810.04805"}],
        "targets": {"words_range": [60, 600], "density_min": 0.5,
                    "subsection_min_citations": 1},
        "cassette_dir": str(FIXTURES / "cassettes"),
    },
]


def _queue_run(fake_openai, arm: str):
    queue_full_run(fake_openai)
    if arm == "treatment":  # 6 个 phase_end 复盘决策(drain 消耗)
        for _ in range(6):
            fake_openai.queue_scenario(text_scenario(DECISION_NONE))
    fake_openai.queue_scenario(text_scenario(JUDGE_SCORE))


def _config(tmp_path, fake_openai):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(f"""model:
  name: fake-model
  base_url: {fake_openai.base_url}
  api_key: fake
paths:
  data_dir: {tmp_path}/data
workflows:
  gate_external_mode: cassette
""", encoding="utf-8")
    return load_config(cfg)


def test_fake_mode_paired_mini_experiment(tmp_path, fake_openai):
    config = _config(tmp_path, fake_openai)
    out_dir = tmp_path / "exp"
    runner = ExperimentRunner(config, out_dir, seed=42,
                              rubric_path=REPO_EXP / "rubric.md")

    for arm in ("treatment", "control"):
        for _task in TASKS:
            _queue_run(fake_openai, arm)
        runner.run(TASKS, arm)

    # -- experiment_runs:4 行,配对,字段完整 -------------------------------
    runs = runner.exp_db.get_experiment_runs()
    assert len(runs) == 4
    by_arm = {"treatment": [r for r in runs if r["arm"] == "treatment"],
              "control": [r for r in runs if r["arm"] == "control"]}
    assert {r["task_id"] for r in by_arm["treatment"]} == {"x01-attention", "x02-bert"}
    assert {r["task_id"] for r in by_arm["control"]} == {"x01-attention", "x02-bert"}
    for r in runs:
        assert r["gates_passed"] == 4
        assert r["session_id"].startswith("exp-")
        assert r["ended_at"] and r["started_at"]
        assert json.loads(r["llm_judge_json"])["overall"] == 8
    assert all(r["skill_lib_commit"] for r in by_arm["treatment"])  # git HEAD 入库
    assert all(r["no_skills_flag"] == 0 for r in by_arm["treatment"])
    assert all(r["no_skills_flag"] == 1 for r in by_arm["control"])
    # aux token:治疗臂(复盘+评审)与对照臂(评审)都 > 0
    assert all(r["aux_tokens"] > 0 for r in runs)

    # -- 隔离(§5):请求级证据 ----------------------------------------------
    n_treat = 2 * (13 + 6 + 1)  # 治疗臂每 run 20 个请求
    n_ctrl = 2 * (13 + 1)       # 对照臂每 run 14 个请求(零复盘消耗)
    assert len(fake_openai.requests) == n_treat + n_ctrl
    treat_first = json.loads(fake_openai.requests[0]["body"])
    ctrl_first = json.loads(fake_openai.requests[n_treat]["body"])
    assert "## Available Skills" in treat_first["messages"][0]["content"]
    assert all("## Available Skills" not in m["content"]
               for m in ctrl_first["messages"] if m["role"] == "system")

    # -- 盲评:每 run 最后一个请求是 judge,无臂标识 -------------------------
    for idx in (19, 39, 53, 67):
        judge_req = json.loads(fake_openai.requests[idx]["body"])
        judge_text = judge_req["messages"][0]["content"]
        assert "treatment" not in judge_text and "control" not in judge_text
        assert "rubric" in judge_text or "academic_tone" in judge_text

    # -- 报告与原始数据 ------------------------------------------------------
    report = tmp_path / "report.md"
    result = render_report(REPO_EXP / "report_template.md", runs, report, seed=42)
    text = report.read_text(encoding="utf-8")
    assert "可测量提升: " in text
    assert result["n_pairs"] == 2
    raw = report.with_suffix(".runs.jsonl")
    assert raw.exists()
    assert len(raw.read_text(encoding="utf-8").splitlines()) == 4
