"""四个学习工具单元测试:skill_view / skill_manage / memory / session_search。

覆盖:deprecate 两步(工具只登记,CLI 批准)、§ 条目记忆、FTS 三模式、
no_skills fail-closed、skill_usage 行(不变量 3 数据路径)。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from mini_hermes.skills_lib import SkillLibrary
from mini_hermes.state_db import SessionDB
from mini_hermes.tools.registry import Registry, ToolContext, discover_builtin_tools


@pytest.fixture()
def env(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    db.create_session("s1")
    config = SimpleNamespace(
        no_skills=False,
        skill_lib_remote=None,
        paths=SimpleNamespace(data_dir=tmp_path, skill_lib_dir=tmp_path / "skill-lib"),
    )
    ctx = ToolContext(cwd=tmp_path, session_id="s1", config=config, db=db)
    registry = discover_builtin_tools(Registry())
    lib = SkillLibrary(config.paths.skill_lib_dir)
    lib.create("demo-skill", "演示技能", "# Demo\n\n完整正文内容。",
               status="active", origin="seeded")
    yield db, ctx, registry, config
    db.close()


# --------------------------------------------------------------- skill_view


def test_skill_view_loads_full_text_and_records_usage(env):
    db, ctx, registry, _ = env
    result = registry.execute("skill_view", {"name": "demo-skill"}, ctx)
    assert "完整正文内容" in result
    usage = db.get_skill_usage("s1")
    assert len(usage) == 1
    assert usage[0]["skill_name"] == "demo-skill"
    assert usage[0]["session_id"] == "s1"
    assert usage[0]["phase"] is None  # M5b 才接 workflow phase


def test_skill_view_unknown(env):
    _, ctx, registry, _ = env
    assert registry.execute("skill_view", {"name": "nope"}, ctx).startswith("Error:")


# ------------------------------------------------------------- skill_manage


def test_skill_manage_create_lands_probation(env):
    _, ctx, registry, config = env
    result = registry.execute("skill_manage", {
        "action": "create", "name": "new-one", "description": "新技能",
        "body": "正文"}, ctx)
    assert "probation" in result
    skill = SkillLibrary(config.paths.skill_lib_dir).load("new-one")
    assert skill.status == "probation"
    assert skill.origin == "learned"


def test_skill_manage_update_evidence(env):
    _, ctx, registry, config = env
    result = registry.execute("skill_manage", {
        "action": "update", "name": "demo-skill",
        "evidence_delta": {"uses": 3, "positive": 2}}, ctx)
    assert "uses': 3" in result or "'uses': 3" in result
    skill = SkillLibrary(config.paths.skill_lib_dir).load("demo-skill")
    assert skill.evidence["uses"] == 3


def test_deprecate_two_step(env):
    """工具只登记 PENDING;批准由 CLI 路径(lib.update + 清记录)完成。"""
    db, ctx, registry, config = env
    # 无 confirm → 拒绝
    r1 = registry.execute("skill_manage",
                          {"action": "deprecate", "name": "demo-skill"}, ctx)
    assert r1.startswith("Error:")
    # confirm → 只写 PENDING,不生效
    r2 = registry.execute("skill_manage", {
        "action": "deprecate", "name": "demo-skill",
        "confirm": True, "reason": "证据恶化"}, ctx)
    assert "PENDING_DEPRECATION" in r2
    record = db.meta_get("pending_deprecation:demo-skill")
    assert json.loads(record)["reason"] == "证据恶化"
    skill = SkillLibrary(config.paths.skill_lib_dir).load("demo-skill")
    assert skill.status == "active"  # 还没生效!
    # 模拟 CLI approve:lib.update + 清记录
    lib = SkillLibrary(config.paths.skill_lib_dir)
    lib.update("demo-skill", status="deprecated")
    db._conn.execute("DELETE FROM state_meta WHERE key=?",
                     ("pending_deprecation:demo-skill",))
    assert lib.load("demo-skill").status == "deprecated"
    assert db.meta_get("pending_deprecation:demo-skill") is None


# --------------------------------------------------------------------- memory


def test_memory_add_list_replace_remove(env):
    _, ctx, registry, _ = env
    registry.execute("memory", {"action": "add", "text": "用户偏好中文"}, ctx)
    registry.execute("memory", {"action": "add", "text": "项目:X 论文"}, ctx)
    out = registry.execute("memory", {"action": "list"}, ctx)
    assert "§1 用户偏好中文" in out and "§2 项目:X 论文" in out
    registry.execute("memory", {"action": "replace", "index": 2, "text": "项目:Y 论文"}, ctx)
    out = registry.execute("memory", {"action": "list"}, ctx)
    assert "项目:Y 论文" in out and "X 论文" not in out
    registry.execute("memory", {"action": "remove", "index": 1}, ctx)
    out = registry.execute("memory", {"action": "list"}, ctx)
    assert "用户偏好中文" not in out


def test_memory_files_separate(env):
    _, ctx, registry, _ = env
    registry.execute("memory", {"action": "add", "file": "user", "text": "用户档案"}, ctx)
    registry.execute("memory", {"action": "add", "file": "memory", "text": "工作记忆"}, ctx)
    user_out = registry.execute("memory", {"action": "list", "file": "user"}, ctx)
    assert "用户档案" in user_out and "工作记忆" not in user_out


# ------------------------------------------------------------ session_search


@pytest.fixture()
def populated_db(env):
    db, ctx, registry, _ = env
    db.append_message("s1", {"role": "user", "content": "丘斯特洛夫斯基的滤波器"})
    db.append_message("s1", {"role": "assistant", "content": "丘脑闸门控制注意"})
    db.create_session("s2")
    db.append_message("s2", {"role": "user", "content": "另一个会话的内容"})
    return db, ctx, registry


def test_session_search_search_mode(populated_db):
    db, ctx, registry = populated_db
    out = registry.execute("session_search",
                           {"mode": "search", "query": "丘斯特洛夫斯基"}, ctx)
    assert "s1#" in out and "滤波器" in out


def test_session_search_scroll_and_browse(populated_db):
    db, ctx, registry = populated_db
    out = registry.execute("session_search",
                           {"mode": "scroll", "rowid": 1, "after": 1}, ctx)
    assert "#1" in out and "#2" in out
    out = registry.execute("session_search", {"mode": "browse"}, ctx)
    assert "s1" in out and "s2" in out


def test_session_search_no_match(populated_db):
    _, ctx, registry = populated_db
    assert registry.execute("session_search",
                            {"mode": "search", "query": "不存在的词xyz"}, ctx) == "(no matches)"


# --------------------------------------------------------------- no_skills


def test_no_skills_fail_closed(env):
    db, ctx, registry, config = env
    config.no_skills = True
    for tool, args in [
        ("skill_view", {"name": "demo-skill"}),
        ("skill_manage", {"action": "create", "name": "x", "description": "y"}),
        ("memory", {"action": "list"}),
        ("session_search", {"mode": "browse"}),
    ]:
        result = registry.execute(tool, args, ctx)
        assert result.startswith("Error:") and "no-skills" in result, tool


def test_no_skills_tools_not_registered():
    """装配层:no_skills=true 时四工具不注册(双重防线第一层)。"""
    registry = discover_builtin_tools(Registry(), include_learning=False)
    for tool in ("skill_view", "skill_manage", "memory", "session_search"):
        assert tool not in registry.names()
