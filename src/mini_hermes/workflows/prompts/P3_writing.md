# P3 Writing —— 扩展草稿(对应 ars-revision)

输入:$workdir/PLAN.md 与 $workdir/EXTENDED_REFERENCES.md(先 read_file)。

任务:写章节「$section」的扩展草稿,用 write_file 产出 $workdir/DRAFT.md。

## 硬要求(G2/G3 机械检查)

1. 每个事实性断言后跟 [N] 引用标记,与 EXTENDED_REFERENCES.md 编号对应。
2. **强断言必须有引用**(G2,ZERO_VIOLATIONS):任何「数字×」「N%」「Npp」
   「O(...)」「n=A-B」「N agents/systems/papers」形式的断言,同句必须带 [N]。
3. 引用密度 ≥ 目标(G3:density = 引用数/(字数/100));每个小节 ≥ 3 个引用。
4. 学术语调;论证流按计划。

目标参数:$targets
完成后一句话总结(字数 / 引用数 / 密度)。
