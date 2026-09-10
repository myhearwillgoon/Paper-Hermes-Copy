# P2 Planning —— 扩展计划(对应 ars-plan)

输入:$workdir/EXTENDED_REFERENCES.md(先用 read_file 读入)。

任务:为章节「$section」写详细扩展计划,写到 $workdir/PLAN.md。

计划必须包含:
- 小节划分(每个小节标题 + 要点)
- 引用分配:每个小节计划用哪些 [N] 引用(每个小节 ≥ 3 个,G3 会查)
- 论证流:Theory → Empirical → Mitigation
- 目标参数:$targets

用 write_file 产出 PLAN.md,完成后一句话总结。
