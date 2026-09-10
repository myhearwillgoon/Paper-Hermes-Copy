---
name: citation-validation-checklist
description: G1 引用验证程序:年份/venue/可访问性/存在性四道闸
tags: [citations, gate, g1]
status: seeded
origin: seeded
evidence: {uses: 0, positive: 0, negative: 0}
created_at: "2026-08-01T00:00:00+00:00"
updated_at: "2026-08-01T00:00:00+00:00"
---

# Citation Validation Checklist

写 EXTENDED_REFERENCES.md 之前,逐条过这个程序(来自 G1 的机械检查口径):

1. **Year Gate**:每条引用年份 ≥ 任务 year_threshold,STRICT,无例外。
2. **Venue Gate**:venue 在许可列表(arXiv/NeurIPS/ICML/ICLR/ACL/EMNLP/AAMAS/
   IEEE/ACM/Nature/Science/JMLR/COLM/COLING)。注意:arXiv ID 不算 venue
   证据,venue 字段必须显式出现。
3. **Access Gate**:每条必须有 arXiv ID、DOI 或公开 URL 之一。
4. **Existence Gate**:有 arXiv ID 的用 arXiv API 验;只有标题的用
   Semantic Scholar 标题搜索验。至少一个库能查到才算存在。
5. 编号列表 [1] [2] ... 必须与正文引用标记一一对应。

常见失败:凭记忆编 arXiv ID(存在性验证会抓);年份写错(四舍五入到
"近期");venue 只写 "preprint"(不在许可列表)。发现不确定的引用,宁可
少收一条,不要赌 G1。
