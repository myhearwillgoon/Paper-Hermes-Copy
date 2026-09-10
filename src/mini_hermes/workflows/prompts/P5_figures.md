# P5 Figures —— 图表与披露(G4 会机械检查)

输入:$workdir/DRAFT.md 与 $workdir/REVIEW_REPORT.md。

任务:为章节设计图表。用 write_file 在 $workdir/figures/ 下创建图文件
(占位 SVG/文本描述即可),并用 write_file 产出 $workdir/FIGURES.md。

## 硬要求(G4:100% 披露,BLOCK)

FIGURES.md 中每个图引用形如 `![Figure N](figures/figureN.svg)`,
其 caption(同行或下一行)**必须**带披露标签,说明数据性质:

- reconstructed:基于文献描述重建("Data points reconstructed from [4]; exact values are illustrative")
- theoretical:理论推导/数学函数("Curves are mathematical functions plotted from Section X formulas; not empirical measurements")
- simulated:基于趋势的模拟("Simulated based on qualitative trends reported in literature; not original experimental data")
- conceptual:概念性评估("Qualitative assessments based on framework design principles; not benchmark results")
- illustrative:说明性模型("Mathematical models for conceptual illustration; not empirical measurements")

引用的图文件必须真实存在于 figures/ 目录。完成后一句话总结图数。
