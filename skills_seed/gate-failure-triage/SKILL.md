---
name: gate-failure-triage
description: G1/G3 gate 失败时的排查顺序与修复策略
tags: [gate, triage, debug]
status: seeded
origin: seeded
evidence: {uses: 0, positive: 0, negative: 0}
created_at: "2026-08-01T00:00:00+00:00"
updated_at: "2026-08-01T00:00:00+00:00"
---

# Gate Failure Triage

Gate 失败不要慌着重跑,按这个顺序排查:

## G1(Citation Quality,BLOCK)

1. 看 violations 里是 Year、Venue、Access 还是 Existence。
2. Existence 失败 = 文献可能是编的:从列表移除或换真实文献,不要
   "手动标记 verified"。
3. Year/Venue 失败 = 逐条对照许可列表修,不要试图放宽阈值。

## G3(Citation Density,USER_DECISION)

1. 先算缺多少:(density_min × words/100) − 现有引用数。
2. 密度不足 → 定向给引用最少的小节补引用(不要全文平均撒)。
3. 字数不足 → 扩写证据最充分的小节,不要灌水。
4. 只有在质量确实优先于数量时才考虑豁免,并把理由写清楚
   (豁免记录会进实验数据)。

## 通用

- 连续两次同一 gate 失败,停下来读报告,不要第三次重跑同一策略。
