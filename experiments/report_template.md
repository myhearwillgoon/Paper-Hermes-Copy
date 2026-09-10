# 对照实验报告(mini-hermes M6)

**生成时间**: {generated_at}
**任务集**: experiments/tasks.yaml(冻结,seed={seed})
**运行数**: treatment {n_treatment} / control {n_control}

## 判定(PLAN §1 可证伪判据)

**可测量提升: {verdict}**

判据:治疗臂相对对照臂 4-Gate 全过率提升 ≥15 个百分点,或配对次要指标中
至少两项显著改善且方向一致。

- 主指标(4-Gate 全过率,配对):{primary_line}
- 次要指标(每 Gate 平均 retry 数,配对 Wilcoxon):{retry_line}
- 次要指标(LLM-judge 盲评分,配对 Wilcoxon):{judge_line}
- 护栏指标(waiver 次数):treatment {waivers_treatment} / control {waivers_control}
- token 消耗(主/辅):{tokens_line}

阴性结果也是有效研究产出:若判定 NO,报告"未检测到可测量提升"并分析归因
(skill 质量 / 使用率 / 任务适配度)。

## 明细

{runs_table}

---

原始数据:{raw_data_path}(JSONL,experiment_runs 全字段)
