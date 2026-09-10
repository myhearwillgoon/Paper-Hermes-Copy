"""论文 loop 6-Phase cassette 端到端(PLAN §6 M3:test_paper_loop_cassette.py)。

假 LLM(脚本化 write_file 调用)+ cassette 回放外部 API → 全确定性。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from mini_hermes.runtime import AgentRuntime
from mini_hermes.state_db import SessionDB
from mini_hermes.tools.registry import Registry, discover_builtin_tools
from mini_hermes.workflow import engine as eng
from mini_hermes.workflow.definition import load_builtin_workflow
from mini_hermes.workflow.engine import WorkflowEngine
from tests.fakes.openai_server import text_scenario, tool_call_scenario

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
CASSETTES = str(FIXTURES / "cassettes")

REFS = """# Extended References - 1.1 Attention Mechanisms

## References

[1] Vaswani, A., et al. (2017). Attention Is All You Need. *NeurIPS*. arXiv:1706.03762

[2] Devlin, J., et al. (2019). BERT: Pre-training of Deep Bidirectional Transformers. *NAACL*. arXiv:1810.04805
"""

DRAFT = """## 背景

Self-attention 机制 [1] 完全摒弃了循环结构,把序列建模转化为并行矩阵运算。
后续预训练工作 [2] 在此基础上验证了双向表示的有效性。
工程报告了 40% overhead reduction [2] 的结果。

## 方法

我们沿用缩放点积注意力 [1],并按双向掩码策略扩展 [2]。
训练目标遵循掩码语言建模 [2],推理复杂度保持 O(n²) [1]。
"""

FIGURES_MD = """# Figures

![Figure 1](figures/figure1.svg)
Data points reconstructed from [4]; exact values are illustrative.
"""

FINAL = """## 1.1 Attention Mechanisms

Self-attention 机制 [1] 把序列建模转化为并行矩阵运算 [2]。

## References

[1] Vaswani, A., et al. (2017). Attention Is All You Need. *NeurIPS*. arXiv:1706.03762
[2] Devlin, J., et al. (2019). BERT: Pre-training of Deep Bidirectional Transformers. *NAACL*. arXiv:1810.04805
"""


def write_call(path: str, content: str, call_id: str):
    return tool_call_scenario([{
        "id": call_id, "name": "write_file",
        "arguments": json.dumps({"path": path, "content": content},
                                ensure_ascii=False),
    }])


def queue_full_run(fake_openai):
    """排好整轮 6-phase 的 13 个场景。"""
    fake_openai.queue_scenario(write_call("EXTENDED_REFERENCES.md", REFS, "c1"))
    fake_openai.queue_scenario(text_scenario("P1 完成"))
    fake_openai.queue_scenario(write_call(
        "PLAN.md",
        "# Plan\n\n## 小节划分\n- 背景:机制动机 [1]\n- 方法:缩放点积与掩码 [2]\n\n"
        "## 引用分配\n- 背景小节:[1] [2]\n- 方法小节:[1] [2]\n\n论证流:Theory → Empirical → Mitigation\n",
        "c2"))
    fake_openai.queue_scenario(text_scenario("P2 完成"))
    fake_openai.queue_scenario(write_call("DRAFT.md", DRAFT, "c3"))
    fake_openai.queue_scenario(text_scenario("P3 完成"))
    fake_openai.queue_scenario(write_call(
        "REVIEW_REPORT.md",
        "# Review\n\n## Summary\nSolid draft.\n\n## Strengths\n- 论证完整 [1]\n\n"
        "## Weaknesses\n- 图缺失\n\n## Overall\n8/10,修改后可达顶会水准。\n",
        "c4"))
    fake_openai.queue_scenario(text_scenario("P4 完成"))
    fake_openai.queue_scenario(write_call("FIGURES.md", FIGURES_MD, "c5"))
    fake_openai.queue_scenario(write_call("figures/figure1.svg", "<svg/>\n", "c6"))
    fake_openai.queue_scenario(text_scenario("P5 完成"))
    fake_openai.queue_scenario(write_call("FINAL.md", FINAL, "c7"))
    fake_openai.queue_scenario(text_scenario("P6 完成"))


def _start_engine(tmp_path, fake_openai):
    db = SessionDB(tmp_path / "state.db")
    db.create_session("s1", source="workflow")
    workdir = tmp_path / "work"
    registry = discover_builtin_tools(Registry())
    runtime = AgentRuntime(
        db, "s1",
        base_url=fake_openai.base_url, api_key="fake", model="fake-model",
        tools=registry, cwd=workdir,
    )
    task = yaml.safe_load(
        (FIXTURES / "paper_tasks" / "task_attention.yaml").read_text(encoding="utf-8")
    )
    definition, def_dir = load_builtin_workflow("academic_paper")
    engine = WorkflowEngine.start(
        db, definition, def_dir, runtime, registry, workdir, task,
        run_id="run1",
        gate_args_extra={"external_mode": "cassette", "cassette_dir": CASSETTES},
    )
    return db, engine, workdir


def test_full_six_phase_run_cassette(tmp_path, fake_openai):
    queue_full_run(fake_openai)
    db, engine, workdir = _start_engine(tmp_path, fake_openai)

    assert engine.run() == eng.COMPLETED

    # 全部契约产出
    for artifact in ["EXTENDED_REFERENCES.md", "PLAN.md", "DRAFT.md",
                     "REVIEW_REPORT.md", "FIGURES.md", "FINAL.md",
                     "figures/figure1.svg"]:
        assert (workdir / artifact).is_file(), artifact

    # 6 个 checkpoint;gate 结果结构对齐 gate_1_report.json
    cps = db.get_workflow_checkpoints("run1")
    assert [c["phase_id"] for c in cps] == ["P1", "P2", "P3", "P4", "P5", "P6"]
    g1 = cps[0]["gate_result"]["G1"]
    assert g1["gate_id"] == "G1" and g1["status"] == "PASSED"
    assert {"gate_id", "gate_name", "status", "criteria", "violations"} <= set(g1)
    assert all({"name", "threshold", "actual", "passed"} <= set(c)
               for c in g1["criteria"])
    assert cps[2]["gate_result"]["G2"]["status"] == "PASSED"
    assert cps[2]["gate_result"]["G3"]["status"] == "PASSED"
    assert cps[4]["gate_result"]["G4"]["status"] == "PASSED"

    # run 行终态
    row = db.get_workflow_run("run1")
    assert row["status"] == "COMPLETED"
    assert all(row["phase_states"][p] == "passed"
               for p in ["P1", "P2", "P3", "P4", "P5", "P6"])

    # 恰好 13 次 API 调用(每 phase 的 turn 数确定),整个 run 同一 session transcript
    assert len(fake_openai.requests) == 13
    rows = db.get_messages("s1")
    assert rows[0]["role"] == "user"
    assert "1.1 Attention Mechanisms" in rows[0]["content"]  # P1 任务 prompt
    assert any(m["role"] == "tool" and "EXTENDED_REFERENCES" in (m["content"] or "")
               for m in rows)
    db.close()
