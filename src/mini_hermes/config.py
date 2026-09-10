"""mini-hermes 配置系统(PLAN E6 / E5b)。

加载顺序:config.yaml ← 环境变量覆盖(环境变量优先)。
见 README「配置」一节的环境变量对照表。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator


class ModelConfig(BaseModel):
    """单个模型端点配置(主模型与辅模型同构,PLAN E6)。"""

    model_config = ConfigDict(extra="forbid")

    name: str
    base_url: str
    api_key: str
    temperature: Optional[float] = None

    @field_validator("name", "base_url", "api_key")
    @classmethod
    def _non_empty(cls, v: str, info) -> str:
        if not v or not v.strip():
            raise ValueError(f"{info.field_name} 不能为空")
        return v


class PathsConfig(BaseModel):
    """路径配置。validate_default=True 让默认值的 ~ 也被展开。"""

    model_config = ConfigDict(extra="forbid", validate_default=True)

    data_dir: Path = Path("~/.mini-hermes")
    skill_lib_dir: Path = Path("~/.mini-hermes/skill-lib")

    @field_validator("data_dir", "skill_lib_dir", mode="before")
    @classmethod
    def _expand(cls, v) -> Path:
        return Path(v).expanduser()


class LoggingConfig(BaseModel):
    """日志配置(均有默认值,见 logging.py)。"""

    model_config = ConfigDict(extra="forbid", validate_default=True)
    level: str = "INFO"
    dir: Path = Path("~/.mini-hermes/logs")

    @field_validator("dir", mode="before")
    @classmethod
    def _expand(cls, v) -> Path:
        return Path(v).expanduser()

    @field_validator("level")
    @classmethod
    def _valid_level(cls, v: str) -> str:
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        upper = v.upper()
        if upper not in allowed:
            raise ValueError(f"非法日志级别 {v!r},可选:{sorted(allowed)}")
        return upper


class WorkflowsConfig(BaseModel):
    """workflow 引擎配置(M3)。"""

    model_config = ConfigDict(extra="forbid")

    gate_external_mode: str = "cassette"  # live(录制) | cassette(回放)

    @field_validator("gate_external_mode")
    @classmethod
    def _valid_mode(cls, v: str) -> str:
        if v not in ("live", "cassette"):
            raise ValueError(f"gate_external_mode 只能是 live|cassette,得到 {v!r}")
        return v


class CompressionConfig(BaseModel):
    """上下文压缩配置(M4,E8 简化版)。"""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    threshold: int = 60000       # 估算 token 超过则压缩
    tail_messages: int = 20      # 尾保护条数


class LearningConfig(BaseModel):
    """学习闭环参数(M5b,Q7 分级自治)。"""

    model_config = ConfigDict(extra="forbid")

    review_enabled: bool = True    # review fork 开关(确定性测试可关)
    promotion_uses: int = 3        # probation 转正所需最小 uses
    promotion_ratio: float = 0.6   # positive/(positive+negative) 转正阈值


class Config(BaseModel):
    """顶层配置。aux_model 缺省时回退为 model(PLAN E6)。"""

    model_config = ConfigDict(extra="forbid")

    model: ModelConfig
    aux_model: Optional[ModelConfig] = None
    tavily_api_key: Optional[str] = None
    paths: PathsConfig = Field(default_factory=PathsConfig)
    no_skills: bool = False
    skill_lib_remote: Optional[str] = None  # Q8:skill 库 git remote(可选,push 失败只警告)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    workflows: WorkflowsConfig = Field(default_factory=WorkflowsConfig)
    compression: CompressionConfig = Field(default_factory=CompressionConfig)
    learning: LearningConfig = Field(default_factory=LearningConfig)

    @property
    def effective_aux_model(self) -> ModelConfig:
        """辅模型缺省时回退主模型。"""
        return self.aux_model if self.aux_model is not None else self.model


# 环境变量 → 配置路径(README 中有对照表)
_ENV_OVERRIDES = {
    "MINI_HERMES_MODEL": ("model", "name"),
    "MINI_HERMES_BASE_URL": ("model", "base_url"),
    "MINI_HERMES_API_KEY": ("model", "api_key"),
    "MINI_HERMES_AUX_MODEL": ("aux_model", "name"),
    "MINI_HERMES_AUX_BASE_URL": ("aux_model", "base_url"),
    "MINI_HERMES_AUX_API_KEY": ("aux_model", "api_key"),
}


def load_config(path: str | Path = "config.yaml") -> Config:
    """从 YAML 文件加载配置并应用环境变量覆盖。

    环境变量优先级高于文件;TAVILY_API_KEY 在文件未给出时兜底。
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(
            f"配置文件不存在:{path}(请从 config.yaml.example 复制)"
        )
    with path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"配置文件格式错误:{path} 顶层必须是 mapping")

    raw = dict(raw)  # 不污染调用方

    for env, (section, key) in _ENV_OVERRIDES.items():
        value = os.environ.get(env)
        if value is None:
            continue
        bucket = raw.setdefault(section, {})
        if not isinstance(bucket, dict):
            raise ValueError(f"配置项 {section} 必须是 mapping,无法应用 {env}")
        bucket[key] = value

    # aux_model 只被部分覆盖时,以主模型为底再覆盖
    if "aux_model" in raw and raw["aux_model"] is not None and "model" in raw:
        merged = dict(raw["model"])
        merged.update(raw["aux_model"])
        raw["aux_model"] = merged

    if raw.get("tavily_api_key") is None:
        raw["tavily_api_key"] = os.environ.get("TAVILY_API_KEY")

    config = Config.model_validate(raw)

    import logging as _logging

    from .logging import log_event

    log_event(
        _logging.getLogger("mini_hermes"), _logging.INFO, "config_loaded",
        config_path=str(path), no_skills=config.no_skills,
    )
    return config
