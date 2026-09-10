"""对照实验分析(PLAN §10.5)。纯 stdlib,不用 scipy。

数学选择(如实记录):
- McNemar:精确二项检验(two-sided),n=b+c 个 discordant pairs,p = 2·P(X≤min(b,c)),X~Bin(n,0.5),封顶 1。
- 效应量:差值 + 95% CI,**简单正态近似**(非 Newcombe;小样本下 CI 偏窄,如实标注)。
- Wilcoxon 符号秩:n≤25 用**精确枚举**(2^n 种符号分配,枚举精确 p);n>25 正态近似(本实验 n≤10,永远走精确路径)。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from math import comb, sqrt
from pathlib import Path
from typing import Optional

SIGNIFICANCE = 0.05
PRIMARY_THRESHOLD_PP = 0.15


# ---------------------------------------------------------------- McNemar


def mcnemar_exact(b: int, c: int) -> float:
    """discordant pairs b/c → two-sided 精确 p。"""
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    p = 2 * sum(comb(n, i) for i in range(k + 1)) * (0.5 ** n)
    return min(1.0, p)


def proportion_diff_ci(x1: int, n1: int, x2: int, n2: int) -> tuple[float, float, float]:
    """差值 + 95% CI(简单正态近似,小样本偏窄,文档已标注)。"""
    if n1 == 0 or n2 == 0:
        return 0.0, 0.0, 0.0
    p1, p2 = x1 / n1, x2 / n2
    diff = p1 - p2
    se = sqrt(p1 * (1 - p1) / n1 + p2 * (1 - p2) / n2)
    return diff, diff - 1.96 * se, diff + 1.96 * se


# ------------------------------------------------------------------ Wilcoxon


def wilcoxon_signed_rank(diffs: list[float]) -> tuple[float, float]:
    """配对符号秩检验。返回 (W, two-sided p)。零差剔除。

    n≤25 精确枚举;n>25 正态近似(带连续性修正)。
    """
    pairs = sorted((abs(d), d) for d in diffs if d != 0)
    n = len(pairs)
    if n == 0:
        return 0.0, 1.0
    # 秩(平均秩处理并列)
    ranks = []
    i = 0
    while i < n:
        j = i
        while j + 1 < n and pairs[j + 1][0] == pairs[i][0]:
            j += 1
        avg = (i + 1 + j + 1) / 2
        ranks.extend([avg] * (j - i + 1))
        i = j + 1
    w_plus = sum(r for r, (_, d) in zip(ranks, pairs) if d > 0)
    w_total = n * (n + 1) / 2
    w = min(w_plus, w_total - w_plus)

    if n <= 25:
        # 精确枚举:2^n 种符号分配下 W+ 的分布
        counts = {0.0: 1}
        for r in ranks:
            new = dict(counts)
            for value, cnt in counts.items():
                new[value + r] = new.get(value + r, 0) + cnt
            counts = new
        extreme = sum(cnt for value, cnt in counts.items()
                      if value <= w or value >= w_total - w)
        p = min(1.0, extreme / (2 ** n))
    else:
        mean = w_total / 2
        var = n * (n + 1) * (2 * n + 1) / 24
        z = (w - mean + 0.5) / sqrt(var)  # 连续性修正
        # 正态 CDF 近似(math.erf)
        from math import erf

        p = 2 * (1 - (1 + erf(abs(z) / sqrt(2))) / 2)
    return w, p


# ------------------------------------------------------------- 配对与判定


def pair_runs(runs: list[dict]) -> list[dict]:
    """按 task_id 配对 treatment/control(各取最新一条)。"""
    by_task: dict[str, dict[str, dict]] = {}
    for run in runs:
        by_task.setdefault(run["task_id"], {})[run["arm"]] = run
    pairs = []
    for task_id, arms in sorted(by_task.items()):
        if "treatment" in arms and "control" in arms:
            pairs.append({"task_id": task_id,
                          "treatment": arms["treatment"],
                          "control": arms["control"]})
    return pairs


def all_passed(run: dict) -> bool:
    return (run.get("gates_passed") or 0) >= 4


def _retries(run: dict) -> int:
    try:
        retries = json.loads(run.get("gate_retries_json") or "{}")
    except (TypeError, json.JSONDecodeError):
        return 0
    return sum(int(v) for v in retries.values())


def _judge(run: dict) -> Optional[float]:
    try:
        judge = json.loads(run.get("llm_judge_json") or "{}")
    except (TypeError, json.JSONDecodeError):
        return None
    overall = judge.get("overall")
    return float(overall) if overall is not None else None


def analyze(runs: list[dict]) -> dict:
    """全量分析:主指标 McNemar + 次要指标 Wilcoxon + 判定块。"""
    pairs = pair_runs(runs)
    n = len(pairs)
    t_pass = sum(1 for p in pairs if all_passed(p["treatment"]))
    c_pass = sum(1 for p in pairs if all_passed(p["control"]))
    b = sum(1 for p in pairs if all_passed(p["treatment"]) and not all_passed(p["control"]))
    c = sum(1 for p in pairs if not all_passed(p["treatment"]) and all_passed(p["control"]))
    diff, lo, hi = proportion_diff_ci(t_pass, n, c_pass, n)

    retry_diffs = [_retries(p["control"]) - _retries(p["treatment"]) for p in pairs]
    _, retry_p = wilcoxon_signed_rank(retry_diffs)
    retry_better = sum(1 for d in retry_diffs if d > 0) > sum(1 for d in retry_diffs if d < 0)

    judge_pairs = [(_judge(p["treatment"]), _judge(p["control"])) for p in pairs]
    judge_diffs = [t - c for t, c in judge_pairs if t is not None and c is not None]
    _, judge_p = wilcoxon_signed_rank(judge_diffs)
    judge_better = sum(1 for d in judge_diffs if d > 0) > sum(1 for d in judge_diffs if d < 0)

    secondary_wins = 0
    if retry_p < SIGNIFICANCE and retry_better:
        secondary_wins += 1
    if judge_p < SIGNIFICANCE and judge_better:
        secondary_wins += 1

    primary_met = diff >= PRIMARY_THRESHOLD_PP
    verdict = "YES" if (primary_met or secondary_wins >= 2) else "NO"
    return {
        "n_pairs": n,
        "treatment_pass": t_pass, "control_pass": c_pass,
        "mcnemar_b": b, "mcnemar_c": c, "mcnemar_p": mcnemar_exact(b, c),
        "diff": diff, "ci_lo": lo, "ci_hi": hi,
        "primary_met": primary_met,
        "retry_p": retry_p, "retry_better": retry_better,
        "judge_p": judge_p, "judge_better": judge_better,
        "secondary_wins": secondary_wins,
        "waivers": {
            "treatment": sum(int(p["treatment"].get("waiver_count") or 0) for p in pairs),
            "control": sum(int(p["control"].get("waiver_count") or 0) for p in pairs),
        },
        "verdict": verdict,
    }


def pilot_baseline(runs: list[dict]) -> dict:
    """§10.4 基线方差:每个 pilot 任务 control 臂重复跑的通过率离散度。

    spread = 各 pilot 任务通过率(重复均值)的 max-min;≥15pp 触发警报。
    """
    by_task: dict[str, list[bool]] = {}
    retries: list[int] = []
    for run in runs:
        by_task.setdefault(run["task_id"], []).append(all_passed(run))
        retries.append(_retries(run))
    rates = {t: sum(v) / len(v) for t, v in by_task.items()}
    spread = (max(rates.values()) - min(rates.values())) if rates else 0.0
    retry_mean = sum(retries) / len(retries) if retries else 0.0
    retry_var = (sum((r - retry_mean) ** 2 for r in retries) / len(retries)) if retries else 0.0
    warn = spread >= PRIMARY_THRESHOLD_PP
    return {
        "task_rates": rates, "spread": spread,
        "retry_mean": retry_mean, "retry_var": retry_var,
        "warning": warn,
        "warning_text": (
            "基线波动 ≥ ±15pp:主指标噪声过大,应按 PLAN §10.4 改用 retry 数/"
            "LLM-judge 分等连续指标为主指标,并修订预注册后 re-commit。"
            if warn else ""
        ),
    }


# ------------------------------------------------------------------ 报告


def render_report(template_path: str | Path, runs: list[dict],
                  out_path: str | Path, *, seed: int) -> dict:
    """按模板生成报告 + 原始数据 JSONL 归档。返回 analyze() 结果。"""
    result = analyze(runs)
    out_path = Path(out_path)
    raw_path = out_path.with_suffix(".runs.jsonl")
    with raw_path.open("w", encoding="utf-8") as f:
        for run in runs:
            f.write(json.dumps(run, ensure_ascii=False, default=str) + "\n")

    n = result["n_pairs"]
    primary = (
        f"treatment {result['treatment_pass']}/{n} vs control {result['control_pass']}/{n},"
        f" 差值 {result['diff']:+.1%} (95% CI [{result['ci_lo']:+.1%},"
        f" {result['ci_hi']:+.1%}], 简单正态近似),"
        f" McNemar b={result['mcnemar_b']} c={result['mcnemar_c']}"
        f" p={result['mcnemar_p']:.3f}"
    )
    retry = f"Wilcoxon p={result['retry_p']:.3f}(方向:{'治疗臂更优' if result['retry_better'] else '无优势'})"
    judge = f"Wilcoxon p={result['judge_p']:.3f}(方向:{'治疗臂更优' if result['judge_better'] else '无优势'})"
    tokens = (
        f"treatment 主 {sum(int(r.get('input_tokens') or 0) + int(r.get('output_tokens') or 0) for r in runs if r['arm'] == 'treatment')}"
        f" / control 主 {sum(int(r.get('input_tokens') or 0) + int(r.get('output_tokens') or 0) for r in runs if r['arm'] == 'control')};"
        f" 辅 token 见 JSONL"
    )
    rows = [
        "| task | arm | gates_passed | retries | waivers | judge |",
        "|------|-----|--------------|---------|---------|-------|",
    ]
    for r in runs:
        judge_overall = ""
        try:
            judge_overall = str(json.loads(r.get("llm_judge_json") or "{}").get("overall", ""))
        except (TypeError, json.JSONDecodeError):
            pass
        retries = ""
        try:
            retries = str(sum(int(v) for v in json.loads(r.get("gate_retries_json") or "{}").values()))
        except (TypeError, json.JSONDecodeError):
            pass
        rows.append(f"| {r['task_id']} | {r['arm']} | {r.get('gates_passed')}"
                    f" | {retries} | {r.get('waiver_count')} | {judge_overall} |")

    text = template_path and Path(template_path).read_text(encoding="utf-8")
    text = text.format(
        generated_at=datetime.now(timezone.utc).isoformat(),
        seed=seed,
        n_treatment=sum(1 for r in runs if r["arm"] == "treatment"),
        n_control=sum(1 for r in runs if r["arm"] == "control"),
        verdict=result["verdict"],
        primary_line=primary,
        retry_line=retry,
        judge_line=judge,
        waivers_treatment=result["waivers"]["treatment"],
        waivers_control=result["waivers"]["control"],
        tokens_line=tokens,
        runs_table="\n".join(rows),
        raw_data_path=str(raw_path),
    )
    out_path.write_text(text, encoding="utf-8")
    return result
