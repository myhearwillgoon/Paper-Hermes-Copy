# P1 Research —— 文献检索与核验(对应 academic-paper-loop Phase 1)

任务:为章节「$section」建立扩展引用列表。

- 主题方向:$focus_areas
- 种子文献:$seed_papers
- 年份门槛:≥ $year_threshold(G1 Year Gate,STRICT,无例外)

## 硬要求(G1 会逐条机械检查)

1. 每条引用必须包含:作者、年份、标题、venue、arXiv ID 或 DOI/公开 URL(Access Gate)。
2. 年份必须 ≥ $year_threshold。
3. venue 必须在许可列表:arXiv / NeurIPS / ICML / ICLR / ACL / EMNLP / AAMAS / IEEE / ACM / Nature / Science / JMLR / COLM / COLING。
4. 引用必须真实存在 —— G1 会通过 arXiv API / Semantic Scholar 逐条验证,虚假文献 BLOCK。
5. 用编号列表 [1] [2] ...,与正文引用标记一一对应。

## 产出(契约)

用 write_file 把引用列表写到 $workdir/EXTENDED_REFERENCES.md,格式:

```
# Extended References - $section

## References

[1] Author, A., et al. (2024). Paper Title. *Venue Name*. arXiv:2401.12345
```

目标参数:$targets
完成后用一句话总结收录了多少条引用。
