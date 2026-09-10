"""Workflow 定义:YAML 加载 + pydantic 模型。

一个 workflow = 有序 phases;每 phase:
{id, name, prompt_template, toolset, output_contract, gate?}
gate 定义:{tool, escalation(BLOCK|USER_DECISION), max_retries, args}
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field

PACKAGE_WORKFLOWS = Path(__file__).parent.parent / "workflows"


class OutputContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str  # 相对 workdir 的产出文件路径
    min_bytes: int = 1


class GateDef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool: str  # 注册表里的 gate 工具名(E10:gate checker 注册为工具)
    escalation: str = "BLOCK"  # BLOCK | USER_DECISION
    max_retries: int = 2
    args: dict = Field(default_factory=dict)  # 传给 checker 的固定参数(阈值等)


class PhaseDef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    prompt_template: str  # 相对 workflow YAML 所在目录
    toolset: list[str]
    output_contract: OutputContract
    gate: list[str] = Field(default_factory=list)  # gates 表的键,按序执行


class WorkflowDef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    phases: list[PhaseDef]
    gates: dict[str, GateDef] = Field(default_factory=dict)
    targets: dict = Field(default_factory=dict)

    def phase(self, phase_id: str) -> PhaseDef:
        for p in self.phases:
            if p.id == phase_id:
                return p
        raise KeyError(f"未知 phase:{phase_id}")


def load_workflow(path: str | Path) -> tuple[WorkflowDef, Path]:
    """加载 workflow YAML,返回 (定义, YAML 所在目录)。"""
    path = Path(path)
    with path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    return WorkflowDef.model_validate(raw), path.parent


def load_builtin_workflow(name: str = "academic_paper") -> tuple[WorkflowDef, Path]:
    return load_workflow(PACKAGE_WORKFLOWS / f"{name}.yaml")
