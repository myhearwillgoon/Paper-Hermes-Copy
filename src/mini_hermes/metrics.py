"""运行指标 schema + JSONL 读写(PLAN §4 experiment_runs 的前置基线)。

M0 只交付 schema 与 JSONL round-trip;落库到 experiment_runs 表是 M1+ 的事。
字段与 §4 experiment_runs 对齐的命名:arm / tokens 主辅分列 / started_at / ended_at。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

from pydantic import BaseModel, ConfigDict, Field


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class TokenUsage(BaseModel):
    """单模型 token 统计。"""

    model_config = ConfigDict(extra="forbid")

    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens


class RunMetrics(BaseModel):
    """一次运行的指标记录(后续喂给 experiment_runs 表,PLAN §4)。

    - arm: 实验臂(treatment / control),非实验运行为 None
    - main_tokens / aux_tokens: 主辅模型分列(PLAN E6,成本归因)
    """

    model_config = ConfigDict(extra="forbid")

    session_id: str
    run_id: Optional[str] = None
    arm: Optional[str] = None  # "treatment" | "control" | None
    main_tokens: TokenUsage = Field(default_factory=TokenUsage)
    aux_tokens: TokenUsage = Field(default_factory=TokenUsage)
    tool_call_count: int = 0
    started_at: datetime = Field(default_factory=utcnow)
    ended_at: Optional[datetime] = None


class MetricsWriter:
    """JSONL append 写入(每行一个 RunMetrics JSON)。"""

    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, metrics: RunMetrics) -> None:
        with self.path.open("a", encoding="utf-8") as f:
            f.write(metrics.model_dump_json() + "\n")


class MetricsReader:
    """JSONL 读取,逐行解析回 RunMetrics。"""

    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser()

    def read_all(self) -> list[RunMetrics]:
        return list(self.iter())

    def iter(self) -> Iterator[RunMetrics]:
        if not self.path.exists():
            return
        with self.path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    yield RunMetrics.model_validate(json.loads(line))
