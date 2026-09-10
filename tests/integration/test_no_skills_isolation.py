"""--no-skills 实验隔离测试(PLAN §5 硬要求)。

三重断言:system prompt 无 skills 块、四工具不进工具定义、
直接 registry execute 也被拒绝(fail-closed)。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from mini_hermes.runtime import AgentRuntime
from mini_hermes.skills_lib import SkillLibrary, build_system_prompt
from mini_hermes.state_db import SessionDB
from mini_hermes.tools.registry import Registry, ToolContext, discover_builtin_tools
from tests.fakes.openai_server import text_scenario


def test_no_skills_isolation(tmp_path, fake_openai):
    # 库里有 skill,但 no_skills=true → 任何路径都看不到
    lib = SkillLibrary(tmp_path / "skill-lib")
    lib.create("some-skill", "某个技能", "正文", status="active", origin="seeded")
    config = SimpleNamespace(
        no_skills=True, skill_lib_remote=None,
        paths=SimpleNamespace(data_dir=tmp_path,
                              skill_lib_dir=tmp_path / "skill-lib"),
    )

    # 1. system prompt 无 skills 块
    system_prompt = build_system_prompt(config)
    assert "Available Skills" not in system_prompt
    assert "some-skill" not in system_prompt

    # 2. 四工具不注册 → 工具定义里没有
    registry = discover_builtin_tools(Registry(), include_learning=False)
    schema_names = {s["function"]["name"] for s in registry.schemas()}
    for tool in ("skill_view", "skill_manage", "memory", "session_search"):
        assert tool not in schema_names

    # 3. 即使意外拿到带学习工具的 registry,执行也 fail-closed
    full_registry = discover_builtin_tools(Registry())
    ctx = ToolContext(cwd=tmp_path, session_id="s1", config=config)
    result = full_registry.execute(
        "skill_manage", {"action": "create", "name": "x", "description": "y"}, ctx)
    assert result.startswith("Error:") and "no-skills" in result

    # 4. 端到端:system prompt 里没有 skills 块真的发给了端点
    db = SessionDB(tmp_path / "state.db")
    db.create_session("s1")
    fake_openai.queue_scenario(text_scenario("ok"))
    rt = AgentRuntime(
        db, "s1",
        base_url=fake_openai.base_url, api_key="fake", model="fake-model",
        tools=registry, config=config, system_prompt=system_prompt,
    )
    rt.run_turn("hi")
    sent = json.loads(fake_openai.requests[0]["body"])
    system_msgs = [m for m in sent["messages"] if m["role"] == "system"]
    assert all("Available Skills" not in m["content"] for m in system_msgs)
    db.close()
