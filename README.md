# mini-hermes

一个小型 Hermes-Agent 风格的 agent harness 研究实验台(见 `PLAN.md`)。
当前处于 **M0 骨架** 阶段:项目结构、配置系统、假 OpenAI 兼容端点、
外部 API cassette 录制/回放机制、日志/metrics 基线。

## 环境准备

需要 Python ≥ 3.11。

```bash
python3 -m venv .venv
# 若系统缺 python3-venv(ensurepip 不可用):
# python3 -m venv --without-pip .venv
# curl -sSL https://bootstrap.pypa.io/get-pip.py | .venv/bin/python

.venv/bin/python -m pip install -e ".[dev]"
```

## 配置

```bash
cp config.yaml.example config.yaml   # 填入真实的 api_key
```

配置加载顺序:`config.yaml` ← 环境变量覆盖(优先级更高):

| 配置项 | 环境变量 |
|--------|----------|
| `model.api_key` | `MINI_HERMES_API_KEY` |
| `model.base_url` | `MINI_HERMES_BASE_URL` |
| `model.name` | `MINI_HERMES_MODEL` |
| `aux_model.api_key` | `MINI_HERMES_AUX_API_KEY` |
| `aux_model.base_url` | `MINI_HERMES_AUX_BASE_URL` |
| `aux_model.name` | `MINI_HERMES_AUX_MODEL` |
| `tavily_api_key` | `TAVILY_API_KEY` |

`aux_model` 缺省时回退为 `model`(PLAN E6)。

## 运行测试

```bash
.venv/bin/python -m pytest tests/ -x -q
```

注:项目在 `/mnt/d`(WSL DrvFS)上,每个 Python 进程首次导入 openai/httpx
依赖链有 ~30s 的一次性文件读取延迟(非网络问题);pip 安装也明显更慢,
属正常现象。

测试金字塔(PLAN §11):`tests/unit/`(单元)、`tests/integration/`(E2E)、
`tests/invariants/`(不变量)、`tests/chaos/`(混沌)、
`tests/fakes/`(假 OpenAI 端点)、`tests/fixtures/cassettes/`(外部 API 录制)。

## 目录结构

```
src/mini_hermes/     包源码(config / cassettes / logging / metrics)
tests/               测试金字塔 + 假端点 + cassettes
docs/                logging-schema.md 等文档
```
