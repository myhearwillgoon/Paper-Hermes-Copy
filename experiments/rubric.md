# LLM-judge 评分量表(固定 rubric,盲评,源自 GATE_MATRIX 质量条款)

按 1-10 给三个维度打分,再给一个 overall(1-10)。只输出 JSON:
{"scores": {"academic_tone": N, "argument_flow": N, "disclosure": N}, "overall": N}

## academic_tone(学术语调)
- 语言正式、客观、无营销腔;术语使用一致;段落有学术节奏
- 扣分:口语化、绝对化断言无出处、堆砌 buzzword

## argument_flow(论证流)
- 结构为 Theory → Empirical → Mitigation(或合理的等价骨架)
- 每个强断言(数字/百分比/复杂度记号)有引用支撑
- 小节之间有过渡逻辑,不是并列罗列

## disclosure(披露)
- 非实证内容(重建数据/理论曲线/模拟/概念性评估)全部明确标注
- 图与 caption 的披露标签齐全;引用列表与正文标记一一对应
