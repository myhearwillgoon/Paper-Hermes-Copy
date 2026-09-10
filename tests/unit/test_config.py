"""config.py 单元测试:加载示例配置 / 环境变量覆盖 / aux 回退主模型。"""

from __future__ import annotations

import pytest
import yaml

from mini_hermes.config import Config, load_config

MINIMAL = {
    "model": {"name": "m", "base_url": "http://x/v1", "api_key": "k"},
}


def _write(tmp_path, data) -> str:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return str(path)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in (
        "MINI_HERMES_MODEL", "MINI_HERMES_BASE_URL", "MINI_HERMES_API_KEY",
        "MINI_HERMES_AUX_MODEL", "MINI_HERMES_AUX_BASE_URL", "MINI_HERMES_AUX_API_KEY",
        "TAVILY_API_KEY",
    ):
        monkeypatch.delenv(var, raising=False)


def test_load_example_config():
    """config.yaml.example 必须本身可加载(它示例了全部字段)。"""
    config = load_config("config.yaml.example")
    assert config.model.name == "gpt-4o-mini"
    assert config.aux_model is not None
    assert config.paths.data_dir.name == ".mini-hermes"
    assert config.no_skills is False
    assert config.logging.level == "INFO"


def test_aux_defaults_to_main(tmp_path):
    """aux_model 缺省时回退主模型(PLAN E6)。"""
    config = load_config(_write(tmp_path, MINIMAL))
    assert config.aux_model is None
    assert config.effective_aux_model is config.model


def test_env_override_model(tmp_path, monkeypatch):
    monkeypatch.setenv("MINI_HERMES_API_KEY", "env-key")
    monkeypatch.setenv("MINI_HERMES_BASE_URL", "http://env/v1")
    config = load_config(_write(tmp_path, MINIMAL))
    assert config.model.api_key == "env-key"
    assert config.model.base_url == "http://env/v1"
    assert config.model.name == "m"  # 未覆盖的保持文件值


def test_env_override_aux_partial_merges_on_main(tmp_path, monkeypatch):
    """只给 aux 的 key:以主模型为底合并,不丢 name/base_url。"""
    monkeypatch.setenv("MINI_HERMES_AUX_API_KEY", "aux-env-key")
    config = load_config(_write(tmp_path, MINIMAL))
    assert config.aux_model is not None
    assert config.aux_model.api_key == "aux-env-key"
    assert config.aux_model.name == "m"
    assert config.aux_model.base_url == "http://x/v1"


def test_tavily_key_from_config(tmp_path):
    data = {**MINIMAL, "tavily_api_key": "tvly-file"}
    config = load_config(_write(tmp_path, data))
    assert config.tavily_api_key == "tvly-file"


def test_tavily_key_from_env(tmp_path, monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-env")
    config = load_config(_write(tmp_path, MINIMAL))
    assert config.tavily_api_key == "tvly-env"


def test_no_skills_flag(tmp_path):
    config = load_config(_write(tmp_path, {**MINIMAL, "no_skills": True}))
    assert config.no_skills is True


def test_paths_expanduser(tmp_path):
    config = load_config(_write(tmp_path, MINIMAL))
    assert "~" not in str(config.paths.data_dir)
    assert config.paths.data_dir.is_absolute()


def test_missing_file_raises():
    with pytest.raises(FileNotFoundError, match="config.yaml"):
        load_config("/nonexistent/config.yaml")


def test_invalid_missing_model_key(tmp_path):
    data = {"model": {"name": "m", "base_url": "http://x/v1"}}  # 缺 api_key
    with pytest.raises(Exception, match="api_key"):
        load_config(_write(tmp_path, data))


def test_empty_field_rejected(tmp_path):
    data = {"model": {"name": "  ", "base_url": "http://x/v1", "api_key": "k"}}
    with pytest.raises(Exception, match="不能为空"):
        load_config(_write(tmp_path, data))


def test_extra_field_rejected(tmp_path):
    data = {**MINIMAL, "typo_field": 1}
    with pytest.raises(Exception, match="typo_field"):
        load_config(_write(tmp_path, data))
