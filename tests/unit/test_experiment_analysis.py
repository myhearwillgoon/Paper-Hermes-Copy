"""实验分析单元测试:McNemar、CI、Wilcoxon、verdict 四象限、pilot 警报。"""

from __future__ import annotations

import json

from mini_hermes.experiments.analysis import (
    analyze,
    mcnemar_exact,
    pilot_baseline,
    proportion_diff_ci,
    wilcoxon_signed_rank,
)


def _run(task, arm, gates=4, retries=None, judge=None, waivers=0):
    return {
        "task_id": task, "arm": arm, "gates_passed": gates,
        "gate_retries_json": json.dumps(retries or {}),
        "waiver_count": waivers,
        "llm_judge_json": json.dumps({"overall": judge}) if judge is not None else "{}",
        "input_tokens": 0, "output_tokens": 0,
    }


# ------------------------------------------------------------------ McNemar


def test_mcnemar_known_values():
    assert mcnemar_exact(0, 0) == 1.0
    # b=9, c=1:n=10,P(X≤1)=11/1024 → two-sided ≈ 0.0215
    assert abs(mcnemar_exact(9, 1) - 2 * 11 / 1024) < 1e-9
    # b=3, c=3:完全对称 → p=1
    assert mcnemar_exact(3, 3) == 1.0


def test_proportion_diff_ci():
    diff, lo, hi = proportion_diff_ci(8, 10, 4, 10)
    assert abs(diff - 0.4) < 1e-9
    assert lo < diff < hi
    assert proportion_diff_ci(0, 0, 0, 0) == (0.0, 0.0, 0.0)


# ------------------------------------------------------------------ Wilcoxon


def test_wilcoxon_all_positive_is_significant():
    # 6 对全为正:W=0,p = 2/2^6 = 0.03125 < 0.05(精确枚举)
    _, p = wilcoxon_signed_rank([2, 1, 3, 1, 2, 1])
    assert abs(p - 2 / 64) < 1e-9


def test_wilcoxon_symmetric_not_significant():
    _, p = wilcoxon_signed_rank([1, -1, 2, -2, 3, -3])
    assert p > 0.5


def test_wilcoxon_zeros_dropped():
    _, p = wilcoxon_signed_rank([0, 0, 0])
    assert p == 1.0


# ---------------------------------------------------------------- verdict 四象限


def _paired(n, t_gates, c_gates, t_judge, c_judge, t_retry, c_retry):
    runs = []
    for i in range(n):
        runs.append(_run(f"t{i}", "treatment", t_gates,
                         {"P": t_retry}, t_judge))
        runs.append(_run(f"t{i}", "control", c_gates,
                         {"P": c_retry}, c_judge))
    return runs


def test_verdict_yes_via_primary():
    # 8 对:treatment 全过,control 全不过 → diff=100% ≥ 15pp
    result = analyze(_paired(8, 4, 0, 7, 7, 2, 2))
    assert result["primary_met"] is True
    assert result["verdict"] == "YES"
    assert result["mcnemar_b"] == 8 and result["mcnemar_c"] == 0


def test_verdict_yes_via_two_secondary():
    # 主指标差 < 15pp(全平),但 retry 与 judge 都显著同向改善
    result = analyze(_paired(8, 4, 4, 9, 5, 1, 4))
    assert result["primary_met"] is False
    assert result["secondary_wins"] == 2
    assert result["verdict"] == "YES"


def test_verdict_no_one_secondary_only():
    # retry 显著改善但 judge 无差异 → 只有一项次要 → NO
    result = analyze(_paired(8, 4, 4, 7, 7, 1, 4))
    assert result["secondary_wins"] <= 1
    assert result["verdict"] == "NO"


def test_verdict_no_negative_result():
    # 两臂完全一致 → NO(阴性结果)
    result = analyze(_paired(8, 4, 4, 7, 7, 2, 2))
    assert result["verdict"] == "NO"
    assert result["diff"] == 0


# ---------------------------------------------------------------- pilot 警报


def test_pilot_warning_triggers_at_15pp():
    runs = (
        [_run("p1", "control", 4) for _ in range(3)]
        + [_run("p2", "control", 0) for _ in range(3)]
    )
    baseline = pilot_baseline(runs)
    assert baseline["spread"] == 1.0
    assert baseline["warning"] is True
    assert "主指标噪声过大" in baseline["warning_text"]


def test_pilot_stable_no_warning():
    runs = [_run("p1", "control", 4) for _ in range(3)] + \
           [_run("p2", "control", 4) for _ in range(3)]
    baseline = pilot_baseline(runs)
    assert baseline["spread"] == 0.0
    assert baseline["warning"] is False
