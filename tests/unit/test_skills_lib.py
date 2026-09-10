"""SkillLibrary 单元测试:CRUD、frontmatter round-trip、索引缓存、校验、git 自动提交。"""

from __future__ import annotations

import subprocess

import pytest

from mini_hermes.skills_lib import SkillLibrary, SkillValidationError


@pytest.fixture()
def lib(tmp_path):
    return SkillLibrary(tmp_path / "skill-lib")


def _git_log(root):
    return subprocess.run(["git", "log", "--format=%s"], cwd=root,
                          capture_output=True, text=True).stdout.splitlines()


def test_create_and_load_round_trip(lib):
    lib.create("my-skill", "测试技能", "# 正文\n\n步骤 1。", tags=["a", "b"],
               status="active", origin="seeded")
    skill = lib.load("my-skill")
    assert skill.name == "my-skill"
    assert skill.description == "测试技能"
    assert skill.tags == ["a", "b"]
    assert skill.status == "active"
    assert skill.origin == "seeded"
    assert skill.evidence == {"uses": 0, "positive": 0, "negative": 0}
    assert "步骤 1。" in skill.body
    assert skill.created_at and skill.updated_at


def test_create_duplicate_rejected(lib):
    lib.create("dup", "第一个", "body")
    with pytest.raises(SkillValidationError, match="已存在"):
        lib.create("dup", "第二个", "body")


def test_validation_rules(lib):
    with pytest.raises(SkillValidationError):
        lib.create("Bad Name", "x", "b")  # 大写/空格不是 slug
    with pytest.raises(SkillValidationError):
        lib.create("ok-name", "超" * 61, "b")  # description > 60
    with pytest.raises(SkillValidationError):
        lib.create("ok-name", "", "b")


def test_update_evidence_and_unknown_fields_preserved(lib):
    path = lib.root / "custom" / "SKILL.md"
    lib.root.mkdir(parents=True, exist_ok=True)
    path.parent.mkdir(parents=True)
    path.write_text(
        "---\nname: custom\ndescription: 自定义\nmy_field: keepme\n"
        "status: probation\norigin: learned\n---\n正文\n",
        encoding="utf-8",
    )
    skill = lib.update("custom", evidence_delta={"uses": 1, "positive": 1},
                       body="新正文")
    assert skill.evidence["uses"] == 1
    assert skill.evidence["positive"] == 1
    reloaded = lib.load("custom")
    assert reloaded.extra["my_field"] == "keepme"  # 未知字段 round-trip
    assert "新正文" in reloaded.body


def test_index_build_and_cache(lib):
    assert lib.build_index() == ""  # 空库
    lib.create("alpha", "甲技能", "b", status="active", origin="seeded")
    lib.create("beta", "乙技能", "b", status="probation", origin="learned")
    index = lib.build_index()
    assert "alpha [active]: 甲技能" in index
    assert "beta [probation]: 乙技能" in index
    assert lib.build_index() == index  # 缓存
    lib.update("alpha", description="甲技能改")
    assert "甲技能改" in lib.build_index()  # 变更后缓存失效


def test_deprecated_excluded_from_index(lib):
    lib.create("old", "旧技能", "b", status="deprecated", origin="learned")
    assert lib.build_index() == ""
    assert lib.load("old").status == "deprecated"
    assert len(lib.list_skills(include_deprecated=True)) == 1


def test_git_auto_commit(lib):
    lib.create("gc", "提交测试", "b")
    lib.update("gc", body="b2")
    log = _git_log(lib.root)
    assert any("skill: init library" in m for m in log)
    assert "skill: gc create (probation)" in log
    assert "skill: gc update (probation)" in log


def test_push_failure_tolerated(lib, tmp_path):
    lib.remote = str(tmp_path / "nonexistent-remote.git")  # 推不上去
    skill = lib.create("pushfail", "推送失败容忍", "b")  # 不抛
    assert skill.name == "pushfail"
