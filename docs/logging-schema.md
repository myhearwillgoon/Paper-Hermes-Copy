# mini-hermes 日志与指标 Schema(M0 基线)

本文档是日志事件与运行指标的**契约**:后续 milestone 新增事件时必须同步更新本文档。
两条输出通道:

1. **结构化日志**:JSON-lines,每行一个 JSON 对象(`mini_hermes/logging.py`)
2. **运行指标**:JSON-lines,每行一个 `RunMetrics`(`mini_hermes/metrics.py`),M1+ 落库到 `experiment_runs` 表(PLAN §4)

---

## 1. 结构化日志

### 1.1 公共字段(每行必有)

| 字段 | 类型 | 说明 |
|------|------|------|
| `ts` | ISO-8601 字符串(UTC) | 事件发生时间 |
| `level` | 字符串 | `DEBUG` / `INFO` / `WARNING` / `ERROR` / `CRITICAL` |
| `logger` | 字符串 | logger 名(默认 `mini_hermes`) |
| `event` | 字符串 | 事件类型,见 §1.3 |
| `session_id` | 字符串,可选 | 会话 ID(M1+ 有 session DB 后必带) |
| `run_id` | 字符串,可选 | 实验运行 ID(对照实验 M6 使用) |
| `exception` | 字符串,可选 | 异常 traceback(`exc_info=True` 时) |

事件特定字段平铺在顶层(与公共字段并列),不嵌套。

### 1.2 配置

由 `config.yaml` 的 `logging` 节配置(`mini_hermes/logging.py: configure_from_config`):

- `level`:最低输出级别,默认 `INFO`
- `dir`:JSONL 文件目录,默认 `~/.mini-hermes/logs`,文件名为 `mini-hermes.jsonl`;同时输出 stderr

### 1.3 事件类型

M0 实际代码路径有限,以下分为「M0 已生效」与「预留(M1+ 落地,字段先冻结)」两类。
预留事件的意义:让 schema 先于实现稳定,避免 M1+ 返工改日志消费方。

#### M0 已生效

| event | level | 事件字段 | 触发点 |
|-------|-------|----------|--------|
| `config_loaded` | INFO | `config_path`, `no_skills` | 配置加载成功 |
| `cassette_record` | INFO | `cassette_key`, `method`, `url`, `status` | cassette record 模式落盘一条交互 |
| `cassette_replay` | DEBUG | `cassette_key`, `method`, `url` | replay 命中 |
| `cassette_miss` | ERROR | `cassette_key`, `method`, `url` | replay 未命中(确定性违规,测试应立即失败) |

#### M1 已生效

| event | level | 事件字段 | 触发点 |
|-------|-------|----------|--------|
| `session_start` | INFO | `model`, `source` | CLI 会话创建 |
| `session_end` | INFO | `end_reason`, `message_count` | 会话结束(含中断路径,`end_reason` ∈ completed/interrupted) |
| `llm_response` | DEBUG | `model`, `aux`, `finish_reason`, `input_tokens`, `output_tokens`, `latency_ms` | LLM 响应完成 |
| `llm_error` | WARNING | `model`, `error_type`, `attempt` | API 调用失败(M1 无重试,M2 加 attempt/retry_in_s) |
| `turn_persist` | DEBUG | `point`, `rows_written` | flush 落盘(不变量 2:幂等,只写新行) |
| `crash_persist` | DEBUG/WARNING | `ok`, 失败时 `error` | 组件 #2:用户消息在首次 API 调用前落盘,失败吞掉 |
| `pre_side_effect_persist` | DEBUG/ERROR | `ok`, 失败时 `error` | 组件 #3:工具执行前落盘,失败抛 `session_persistence_failed`(不变量 1) |
| `finalize_persist` | DEBUG/WARNING | `ok`, 失败时 `error` | 组件 #5:轮次结束(含所有错误退出路径)落盘 |
| `turn_error` | ERROR | `error`, `exception` | 每轮错误隔离:异常被捕获、落盘、上浮 TurnError(#7) |
| `resume` | INFO | `rows_hydrated`, `synthetic_tool_results` | 崩溃后恢复(不丢不重;合成行数) |
| `watchdog_kill` | CRITICAL | `frozen_for_s` | 看门狗杀死卡死进程(不变量 4:只杀不救) |
| `daemon_thread_error` | ERROR | `thread`, `error`, recovery 阶段带 `phase` | 长命 daemon 线程吞异常(#8) |
| `barrier_wait` | DEBUG/WARNING | `point`, 超时带 `timed_out` | **仅测试**:MINI_HERMES_TEST_BARRIER_DIR 冻结点 |

#### M2 已生效

| event | level | 事件字段 | 触发点 |
|-------|-------|----------|--------|
| `tool_call` | INFO | `tool_name`, `arguments_preview` | 工具调用落盘后、执行前(不变量 1) |
| `tool_result` | INFO | `tool_name`, `ok`, `duration_ms` | 工具执行完成 |
| `budget_exhausted` | WARNING | `limit` | IterationBudget 耗尽,优雅结束并落盘通知 |

#### M3 已生效

| event | level | 事件字段 | 触发点 |
|-------|-------|----------|--------|
| `workflow_start` | INFO | `run_id`, `workflow` | workflow run 创建 |
| `workflow_resume` | INFO | `run_id`, `current_phase`, `status` | 从 checkpoint 恢复 run |
| `workflow_phase` | INFO | `run_id`, `phase`, `state` | phase 契约验证通过(contracted) |
| `workflow_phase_error` | WARNING/ERROR | `run_id`, `phase`, `attempt`, `error` | 契约缺失/turn 异常,重试前记录 |
| `workflow_checkpoint` | INFO | `run_id`, `phase`, `next_phase` | checkpoint 落盘,phase passed |
| `workflow_completed` | INFO | `run_id` | run 终态 COMPLETED |
| `gate_result` | INFO | `run_id`, `gate`, `phase`, `passed`, `retries`, `waived` | gate 判定(G1-G4) |
| `gate_waived` | INFO | `run_id`, `gate`, `reason` | 用户豁免落盘(waive CLI) |

#### M4 已生效

| event | level | 事件字段 | 触发点 |
|-------|-------|----------|--------|
| `compression_applied` | INFO | `version`, `compacted_rows` | 压缩事务提交(不变量 5:持租约 + 单事务) |
| `compression_skipped` | DEBUG/WARNING | `reason`(empty middle / lease held by live peer) | 压缩跳过(中段为空或租约被占) |

#### M5a 已生效

| event | level | 事件字段 | 触发点 |
|-------|-------|----------|--------|
| `skill_event` | INFO | `skill_name`, `action`(create/update/seed/push_failed), `origin` | skill 生命周期变更(git commit 后);push 失败降级为 WARNING,不崩 run |

#### 预留(M5b+ 落地)

| event | level | 事件字段 | 触发点 |
|-------|-------|----------|--------|
| `llm_request` | DEBUG | `model`, `aux`(bool), `message_count` | 发起 LLM API 调用 |

## 2. 运行指标(RunMetrics)

`mini_hermes/metrics.py: RunMetrics`,pydantic v2 模型;`MetricsWriter`/`MetricsReader`
以 JSONL 追加写 / 逐行读。M1+ 由同一份数据填充 `experiment_runs` 表。

| 字段 | 类型 | 说明 | 对应 experiment_runs 列 |
|------|------|------|------------------------|
| `session_id` | str | 会话 ID | `session_id` |
| `run_id` | str?,默认 None | 实验运行 ID | `id`(映射细节 M6 定) |
| `arm` | str?,默认 None | `treatment` / `control`;非实验运行为 None | `arm` |
| `main_tokens.input_tokens` | int,默认 0 | 主模型输入 token | `input_tokens` |
| `main_tokens.output_tokens` | int,默认 0 | 主模型输出 token | `output_tokens` |
| `aux_tokens.input_tokens` | int,默认 0 | 辅模型输入 token(PLAN E6 主辅分列) | `aux_tokens`(合计) |
| `aux_tokens.output_tokens` | int,默认 0 | 辅模型输出 token | 同上 |
| `tool_call_count` | int,默认 0 | 工具调用总次数 | (实验分析用,表外) |
| `started_at` | datetime(UTC) | 运行开始 | `started_at` |
| `ended_at` | datetime?,默认 None | 运行结束;崩溃运行为 None,resume 后补 | `ended_at` |

说明:

- token 主辅分列是 PLAN §1「token 消耗 / 成本」次要指标的硬要求,辅模型合计后落 `aux_tokens` 列。
- gate 相关指标(`gates_passed` / `gate_retries_json` / `waiver_count` / `llm_judge_json` / `cost_usd`)
  属 M3+ 概念,不进 M0 的 RunMetrics;M6 前扩展本 schema 并更新本文档。
- JSONL 中 datetime 序列化为 ISO-8601(UTC);round-trip 由 `tests/unit/test_metrics.py` 保证。
