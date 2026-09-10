"""渐进披露集成测试:INDEX 注入 system prompt、skill_view 全文加载、
skill_usage 落行、会话内冻结快照 / 新会话可见。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from mini_hermes.runtime import AgentRuntime
from mini_hermes.skills_lib import SkillLibrary, build_system_prompt
from mini_hermes.state_db import SessionDB
from mini_hermes.tools.registry import Registry, discover_builtin_tools
from tests.fakes.openai_server import text_scenario, tool_call_scenario


@pytest.fixture()
def env(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    db.create_session("s1")
    lib = SkillLibrary(tmp_path / "skill-lib")
    lib.create("citation-validation-checklist", "G1 引用验证程序", "# 完整程序\n\n步骤甲。",
               status="seeded", origin="seeded")
    config = SimpleNamespace(
        no_skills=False, skill_lib_remote=None,
        paths=SimpleNamespace(data_dir=tmp_path,
                              skill_lib_dir=tmp_path / "skill-lib"),
    )
    registry = discover_builtin_tools(Registry())
    yield db, lib, config, registry
    db.close()


def _runtime(db, config, registry, fake_openai, tmp_path, system_prompt=None):
    return AgentRuntime(
        db, "s1",
        base_url=fake_openai.base_url, api_key="fake", model="fake-model",
        tools=registry, config=config, cwd=tmp_path,
        system_prompt=system_prompt,
    )


def test_index_in_system_prompt_and_skill_view(env, fake_openai, tmp_path):
    db, lib, config, registry = env
    system_prompt = build_system_prompt(config)
    assert "## Available Skills" in system_prompt
    assert "citation-validation-checklist" in system_prompt
    assert "完整程序" not in system_prompt  # 只有索引,没有全文

    # LLM 决定调 skill_view → 拿到全文
    fake_openai.queue_scenario(tool_call_scenario(
        [{"id": "c1", "name": "skill_view",
          "arguments": '{"name": "citation-validation-checklist"}'}]))
    fake_openai.queue_scenario(text_scenario("我已加载该 skill"))

    rt = _runtime(db, config, registry, fake_openai, tmp_path, system_prompt)
    assert rt.run_turn("帮我查引用") == "我已加载该 skill"

    # system prompt 真的发给了端点(第一个请求的第一条消息)
    sent = json.loads(fake_openai.requests[0]["body"])
    assert sent["messages"][0]["role"] == "system"
    assert "## Available Skills" in sent["messages"][0]["content"]

    # 不变量 3:skill_view 全文加载写了 skill_usage 行
    usage = db.get_skill_usage("s1")
    assert [u["skill_name"] for u in usage] == ["citation-validation-checklist"]


def test_frozen_snapshot_per_session(env, fake_openai, tmp_path):
    """会话 A 进行中新建的 skill 不进 A 的 INDEX;新会话 B 能看到。"""
    db, lib, config, registry = env
    sp_a = build_system_prompt(config)  # 会话 A 启动时构建(冻结)

    # 会话 A 进行中:库新增一个 skill
    lib.create("mid-session-skill", "会话中新建", "body",
               status="probation", origin="learned")

    assert "mid-session-skill" not in sp_a  # 冻结:会话 A 的快照不变

    sp_b = build_system_prompt(config)  # 会话 B 启动:重新构建
    assert "mid-session-skill" in sp_b
