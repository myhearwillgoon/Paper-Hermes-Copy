# mini-hermes 工程实施计划

**版本**: 1.1(经测试工程师评审修订)
**日期**: 2026-08-01
**事实来源**: `~/.hermes/hermes-agent`(Nous Research Hermes Agent 源码)
**上游项目**: `~/.agents/skills/academic-paper-loop`(6-Phase + 4-Gate 论文生成 loop)

**v1.1 修订记录**: 交叉验证测试工程师评审后修订——M6 升级为配对设计对照实验(§10);新增测试金字塔(§11);故障注入矩阵扩展(E11);各 milestone 增加测试交付物;新增 fixture/可观测性策略。

---

## 1. 研究目标

构建一个小型但**真正**具备 Hermes Agent 核心特征的 agent harness,以学术研究论文生成 loop 作为其第一个工作负载,复刻 Hermes 的**自改进闭环**(skill 创建 → 使用 → 证据验证 → 改进/淘汰),并用**对照实验**验证学习效果。

**研究问题**:一个 agent 的经验沉淀机制(skill 生命周期)能否在真实工作负载上产生可测量的性能提升?

**可证伪判据**(预注册,先于实验执行):

| 指标类型 | 指标 | 定义 |
|----------|------|------|
| **主指标** | 4-Gate 全过率(配对) | 同一任务在 treatment(有 skill 库)/ control(`--no-skills`)两臂的 G1-G4 全过差异,McNemar 检验 |
| 次要指标 | 每 Gate 平均 retry 数 | 配对 t 检验或 Wilcoxon 符号秩 |
| 次要指标 | LLM-judge 分数 | 固定辅模型 + 固定 rubric(源自 GATE_MATRIX.yaml 质量条款),盲评(不知臂别) |
| 次要指标 | token 消耗 / 成本 | 主辅模型分别统计 |
| 护栏指标 | waiver 次数 | skill 不应靠"用户更宽容"获胜 |

**效应声明**:治疗臂相对对照臂的全过率提升 ≥15 个百分点,或配对次要指标中至少两项显著改善且方向一致,判定"产生可测量提升";否则判定为**未证伪失败(阴性结果也是有效研究产出)**。n=8-10 的样本量只能检测大效应——这是诚实声明,小效应需要更多运行,不夸大统计功效。

## 2. 设计决策总表

| # | 决策点 | 结论 | 理由 |
|---|--------|------|------|
| Q1 | 定位 | 研究实验台 | 观察"经验如何沉淀、被复用、被改进" |
| Q2 | 形态 | 真 harness(Q10 修正) | 混合形态会丢失 Hermes 的 harness DNA(独立 loop/session/鲁棒性/工具系统) |
| Q3 | 学习对象 | Skill 层(SKILL.md 库) | 归因干净,最贴近 Hermes 原味;不碰 meta 层(gate/prompt 调优) |
| Q4 | 与 skill-evolver | 独立平行 | 它是 Node/transcript 驱动/人审;我们是 Python/gate 驱动/自治 |
| Q5 | 反馈信号 | Gate JSON(结构化) + REVIEW_REPORT(LLM 提炼) | 结构化为主、质性为辅,均落盘可复查 |
| Q6 | 冷启动 | 人工种子 3-5 个(`origin: seeded`) + 2 次历史运行回填(`origin: backfilled`,低置信度) | 最大化利用已有数据,backfill 验证本身是首个学习任务 |
| Q7 | 自治度 | 分级自治:改进全自动;新创建进 `probation` 试用期;淘汰需人确认 | 用试用期代替审批 gate,不可逆操作留人 |
| Q8 | Skill 集成 | 独立 skill 库 + INDEX.md 渐进披露 + git/GitHub 版本化(每次改进一 commit 并 push) | 改动历史可追溯;INDEX 注入 harness 自己的 system prompt 构建器 |
| Q9 | 成功指标 | 配对对照实验:同一任务有/无 skill 库各跑一遍,比较 gate 表现(详见 §10) | 配对设计消掉任务难度方差,功效高于独立双臂 |
| E1 | 构建顺序 | 地基优先 | 先立持久化骨架,再做功能 |
| E2 | 地基范围 | Hermes 骨架全 10 组件 | 含外部 supervisor(systemd unit)+ 看门狗 + exit-75 契约 |
| E3 | 项目形态 | 独立 Python 项目,`openai` SDK + `pydantic` + stdlib SQLite | 务实与透明度的平衡 |
| E4 | Agent loop | 流式 + think 清洗 + IterationBudget;中断只做 Ctrl-C;指数退避重试(429/5xx/网络) | 流式是研究观察刚需;steer/redirect 缓建 |
| E5 | 工具集 | 全量:terminal / read_file / write_file / search_files / todo / web_search(Tavily) / skill_view / skill_manage / memory / session_search | 一次性到位 |
| E5b | web_search 后端 | Tavily(需 `TAVILY_API_KEY`) | 质量优先 |
| E6 | 模型配置 | 主 + 辅双配置(`model` / `aux_model`,各自独立 endpoint/key) | 辅模型跑 review fork / 压缩 / gate 机械检查 / LLM-judge,控成本、控实验变量 |
| E7 | Session schema | 6 张精简表 + 自研 `skill_usage` 归因表 + `experiment_runs` 实验记录表 | 见 §4 |
| E8 | 上下文压缩 | 简化版:头保护(system+首轮)+ 尾保护(最近 N 轮)+ 辅模型中段摘要;摘要作为消息入 transcript;compression_locks 租约生效 | 长运行前置依赖,~300 行 |
| E9 | Review fork 触发 | 每 Phase 结束触发 + gate 失败/waiver 立即触发(失败即学习机会) | 与 gate 节奏对齐,失败归因不丢失 |
| E10 | 论文 loop 编排 | Workflow 引擎内建:YAML 定义 Phase(prompt 模板 + 工具集 + 产出契约),gate checker 注册为工具,编排状态走落盘链 | 编排状态可持久恢复 + 实验控制硬边界 |
| E11 | 测试策略 | 测试金字塔(§11):单元/集成/不变量/工作流/混沌五层;故障注入矩阵覆盖崩溃类(kill -9、SIGTERM)+ 网络类(超时/429/500/流式中断)+ 依赖类(Tavily 失败、辅模型压缩失败);假 OpenAI 端点 | 核心卖点"杀不死"必须分层可证 |

## 3. Hermes 骨架(地基,全 10 组件)

源码依据与最小实现量:

| # | 组件 | Hermes 源码依据 | 最小实现 |
|---|------|----------------|----------|
| 1 | Session DB writer(SQLite WAL) | `hermes_state.py:5613` append-only 行写入,busy-retry + jitter + 时间预算 | ~120 行 |
| 2 | Crash-persist hook(pre-API-call) | `agent/turn_context.py:1219` 用户消息在首次 API 调用前落盘,失败吞掉 | ~20 行 |
| 3 | Pre-side-effect tool-call persist | `agent/conversation_loop.py:6141` tool-call 在工具执行前落盘,**失败即中止本轮** | ~25 行 |
| 4 | 增量去重 flush | `run_agent.py:1955` `_db_persisted` 标记,任何落盘点幂等只写新行 | ~40 行 |
| 5 | Turn-finalize persist | `agent/turn_finalizer.py:410` 剥掉临时脚手架后落盘,所有退出路径都调用 | ~30 行 |
| 6 | Resume loader | `hermes_state.py:6495` 按 rowid 重读重建,计数器从历史重新水合 | ~50 行 |
| 7 | 主循环 + 中断标志 | `agent/conversation_loop.py:1306` while 循环,每轮错误隔离 | ~80 行 |
| 8 | 后台 daemon 线程 | `cron/scheduler_provider.py:176` `except BaseException` 永不死,启动时恢复中断任务 | ~40 行 |
| 9 | 心跳 + 硬退看门狗 | `gateway/shutdown_watchdog.py` 冻结超时 → dump 栈 → `os._exit()`(只杀不救) | ~60 行 |
| 10 | Supervisor 契约 | `gateway/restart.py` exit 75 = 请重启我;systemd `Restart=` unit | ~15 行 + unit 文件 |

**关键认知**:Hermes 的"进程不死"不是应用内 supervisor,而是三层:外部 supervisor(s6/systemd)+ 进程内看门狗负责杀死卡死的自己 + 长命线程吞异常。工作的持久性完全来自 state.db 落盘链,而非进程存活。

## 4. Session Schema(精简自 `hermes_state_common.py`)

```sql
schema_version(version)
sessions(id, source, model, system_prompt, started_at, ended_at,
         end_reason, message_count, input_tokens, output_tokens,
         cwd, title, archived)              -- 砍掉 billing/gateway/handoff 列
messages(id AUTOINCREMENT, session_id, role, content, tool_call_id,
         tool_calls, tool_name, timestamp, token_count, finish_reason,
         reasoning, active, compacted, api_content)
messages_fts                               -- FTS5 external-content on messages(content)
compression_locks(session_id, holder, acquired_at, expires_at)  -- 原样保留
state_meta(key, value)
skill_usage(id, session_id, skill_name, phase, loaded_at,
            gate_results_json)             -- 自研:归因表(Hermes 无,对标 learning_graph)
experiment_runs(id, task_id, arm,          -- 自研:实验记录表(arm ∈ {treatment, control})
            session_id, skill_lib_commit, no_skills_flag,
            gates_passed, gate_retries_json, waiver_count,
            input_tokens, output_tokens, aux_tokens, cost_usd,
            llm_judge_json, started_at, ended_at)
```

## 5. 工具注册表

机制照抄 `tools/registry.py`:模块导入时自注册 `{name, schema(OpenAI function-calling), handler}`。
v1 注册:terminal / read_file / write_file / search_files / todo / web_search(Tavily) / skill_view / skill_manage / memory / session_search。
M2 先实现前 5 个,skill/memory/session_search 随各自子系统落地,schema 预留。

**实验隔离要求**:`--no-skills` 启动标志必须同时做到:① system prompt 不注入 INDEX.md;② 不注册 skill_view / skill_manage 工具;③ review fork 不触发。control 臂不得有任何 skill 泄漏路径。

## 6. Milestone 序列(含测试交付物)

| Milestone | 内容 | 验收标准 | 测试交付物 |
|-----------|------|----------|-----------|
| **M0 骨架** | 项目结构、config(主/辅模型、Tavily key)、假 OpenAI 兼容端点(脚本化响应 + 错误注入:超时/429/500/流式截断)、外部 API cassette 录制机制、日志/metrics 基线格式 | 假端点可被 openai SDK 正常调用;错误注入可触发 | `tests/fakes/openai_server.py`、`tests/fixtures/cassettes/`、日志 schema 文档 |
| **M1 地基** | §3 全部 10 组件 + §4 schema | **混沌测试全绿**:kill -9 × 4 个落盘点 × 崩溃 → 重启 → 消息不丢不重、tool-call 不重复执行 | `tests/unit/test_persistence.py`(幂等/去重/水合)、`tests/integration/test_resume.py`(WAL 崩溃一致性)、`tests/chaos/test_kill9.py`、`tests/chaos/test_sigterm.py` |
| **M2 能干活** | 工具注册表 + 5 核心工具 + 流式循环(think 清洗)+ IterationBudget | CLI 可对话、可调工具、Ctrl-C 优雅退出、杀进程可恢复 | `tests/unit/test_tools.py`、`tests/integration/test_loop_e2e.py`(假端点脚本化多轮 tool-calling)、不变量 1/2/4 的断言测试 |
| **M3 Workflow** | YAML workflow 引擎 + 论文 loop 6-Phase/4-Gate 移植;G1 引用验证照抄 `GATE_MATRIX.yaml`(arXiv/Semantic Scholar/CrossRef);测试 fixture 集(3-5 个最小论文任务 + 外部 API cassettes) | 论文 loop 单次运行端到端完成;中途 kill -9 后 resume 从断点 Phase 继续;fixture 任务在 cassette 模式下确定性通过 | `tests/unit/test_workflow.py`(Phase 状态迁移:gate 失败/waiver/异常)、`tests/integration/test_paper_loop_cassette.py` |
| **M4 压缩** | 简化版上下文压缩(头尾保护 + 辅模型摘要 + 租约) | 长运行不爆上下文;压缩后 resume 正常;压缩中 kill -9 不破坏 transcript | `tests/unit/test_compressor.py`(头尾保护边界)、`tests/chaos/test_compress_crash.py`、不变量 5 断言测试 |
| **M5 学习闭环** | skill 系统(库 + INDEX.md 渐进披露)+ review fork(E9 触发 + 白名单)+ probation 生命周期 + GitHub 同步 + 冷启动(种子 + 回填) | 一次论文 loop 运行后 skill 库有变化且有 commit;**对抗性测试:故意注入错误 skill,后续运行证据恶化后被执行淘汰流程(停在人审 gate)**;`--no-skills` 隔离有效 | `tests/unit/test_skill_lifecycle.py`(probation 转正/淘汰状态机)、`tests/integration/test_review_fork.py`、`tests/integration/test_no_skills_isolation.py`、不变量 3/6/7 断言测试 |
| **M6 对照实验** | experiment runner(§10 设计):任务集 + 配对双跑 + 随机化 + 隔离 + 指标导出 + 报告生成 | 按 §10 完成预实验(基线方差)+ 正式实验,产出含置信区间的报告;**阴性结果也算验收通过** | `experiments/` runner + 报告模板 + 原始数据归档 |

## 7. 核心不变量(违反即 bug)

1. **副作用工具绝不从未落盘的状态执行** — pre-side-effect persist 失败 = 中止本轮,不重试不降级
2. **任何落盘点幂等** — `_db_persisted` 标记保证重复调用只写新行
3. **Skill 使用必须记 `skill_usage` 表** — 归因链断裂 = 实验数据作废
4. **看门狗只杀不救** — 复活是外部 supervisor 的职责,进程内不做自重启
5. **压缩是唯一合法的历史改写** — 且必须持有 compression_locks 租约;摘要作为消息存 transcript,resume 无需单独恢复压缩状态
6. **渐进披露** — system prompt 只注入 skill 索引(name+description+状态),全文按需 skill_view 加载
7. **淘汰不可逆所以留人** — 创建/改进全自动(probation 机制兜底),删除必须人审

每条不变量对应 §6 中的显式断言测试;不变量测试独立于功能测试存在,功能重构后不变量测试必须仍然通过。

## 8. 目录与仓库

- Harness: `~/mini-hermes/`(本项目)
- Skill 库: 独立 git repo,每次改动 commit + push GitHub(地址待定,M5 前确定)
- 实验用 skill 库:**双目录隔离**——`skill-lib/treatment/`(学习闭环写入)与 `skill-lib/control-seed/`(仅种子,或空);两臂各自独立 git 分支,`experiment_runs.skill_lib_commit` 记录每次运行的库版本
- 论文 loop 源定义: 移植自 `~/.agents/skills/academic-paper-loop/`(LOOP_PLAN.md / GATE_MATRIX.yaml / STATE_SCHEMA.md)

## 9. 风险与开放项

- Tavily API key 需在运行环境配置(`TAVILY_API_KEY`);CI 环境无 key 时 web_search 工具标记 skip,用 cassette
- 历史回填仅 2 次真实运行(section 4.2 / 5.3),证据弱 → backfill skill 全部低置信度起步
- 对照实验任务集(M6 前定义,见 §10)——任务选择是最大的人为偏差源,需预注册
- systemd unit 依赖 Linux 用户级 systemd(`systemctl --user`);CI 不依赖 systemd(supervisor 契约测试只断言 exit 75,重启循环由测试脚本模拟),非 Linux 环境降级为手动验证
- 外部检索(arXiv/Semantic Scholar/Tavily)在正式实验中用 live API 保生态效度,靠任务集冻结 + 臂间顺序随机化摊薄时间漂移;原始响应全部归档以便事后审计
- LLM-judge 有自评偏差风险 → 用固定辅模型 + 固定 rubric + 盲评(不告知臂别),并做人评抽查校准

## 10. 对照实验设计(M6 详案,预注册)

### 10.1 设计

**配对被试内设计**(paired within-subject):每个任务在两臂各跑一次——treatment(skill 库 = 当前学习成果)与 control(`--no-skills`,见 §5 隔离要求)。配对消掉任务难度这一最大方差源,是小样本下唯一现实的统计杠杆。

### 10.2 任务集

- **n ≥ 8 个章节任务**,来源:学术 survey 常见主题,难度分层(3 易 / 3 中 / 2 难,"难"= 需要 20+ 引用或多轮 gate retry)
- 任务集在实验开始前冻结并预注册(写入 `experiments/tasks.yaml`,commit 留证);实验中不得增删
- 另有 2 个任务专用于**预实验**(见 10.4),不进正式分析

### 10.3 程序

1. 每任务的臂顺序**随机化**(抛 coin,记录随机种子);允许的情况下两臂交错执行以摊薄外部 API 时间漂移
2. 每 run 全新 session DB;`experiment_runs` 表记录全部指标(见 §4)
3. 外部检索原始响应归档;gate JSON / REVIEW_REPORT / 最终章节全部归档
4. LLM-judge:固定辅模型、固定 rubric、盲评;20% 样本人评抽查校准

### 10.4 预实验(基线方差验证)

正式实验前,用 2 个预实验任务在 **control 臂重复跑 3 次**:测基线方差(全过率是否稳定、retry 数波动范围)。若 control 自身波动已接近 ±15 个百分点,说明主指标噪声过大,需改用 retry 数/LLM-judge 分等连续指标为主指标,并修订本节后 re-commit(修订历史留在 git)。

### 10.5 分析

- 主指标:McNemar 检验(discordant pairs);报告效应量(差值 + 95% CI),不只看 p 值
- 次要指标:配对 t 检验或 Wilcoxon;多重比较不做校正但如实报告(探索性)
- **阴性结果是有效产出**:若未达 §1 可证伪判据,报告"未检测到可测量提升"并分析归因(skill 质量/使用率/任务适配度)

## 11. 测试策略(金字塔)

```
        ╱ 混沌层 ╲        tests/chaos/     kill-9/SIGTERM × 4 落盘点、压缩中崩溃、
       ╱  (少而狠)╲                         workflow 中断恢复、supervisor exit-75 契约
      ╱  E2E 层    ╲      tests/integration/  假端点多轮 tool-calling、论文 loop cassette 回放、
     ╱  工作流层    ╲     tests/unit/test_workflow.py  Phase 状态迁移(gate 失败/waiver/异常)
    ╱  不变量层     ╲    tests/invariants/   §7 七条不变量的独立断言测试
   ╱  单元/集成层   ╲   tests/unit/          幂等、去重、水合、注册表、schema 迁移、压缩边界
  ─────────────────────
  基座:假 OpenAI 端点(错误注入)+ 外部 API cassettes + fixture 任务集
```

**外部依赖策略**:CI 全量跑 cassette 模式(确定性、零成本、零 key);live 冒烟测试手动触发;正式实验用 live API(§9 风险条目)。

**CI**:纯 pytest,不依赖 systemd;`--no-skills` 隔离测试、不变量测试、混沌子集(kill -9 快速组)为必过门禁。
