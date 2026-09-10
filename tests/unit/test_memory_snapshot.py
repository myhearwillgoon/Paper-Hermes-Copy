"""memory 快照注入 build_system_prompt(M5a 遗留的一行接线)。"""

from __future__ import annotations

from types import SimpleNamespace

from mini_hermes.skills_lib import build_system_prompt


def _config(tmp_path, no_skills=False):
    return SimpleNamespace(
        no_skills=no_skills,
        skill_lib_remote=None,
        paths=SimpleNamespace(data_dir=tmp_path,
                              skill_lib_dir=tmp_path / "skill-lib"),
    )


def test_memory_block_injected(tmp_path):
    mem_dir = tmp_path / "memories"
    mem_dir.mkdir()
    (mem_dir / "MEMORY.md").write_text("§ 用户偏好中文回复\n§ 项目是 X 论文\n")
    (mem_dir / "USER.md").write_text("§ 高校研究者\n")
    prompt = build_system_prompt(_config(tmp_path))
    assert "## Memory" in prompt
    assert "用户偏好中文回复" in prompt
    assert "高校研究者" in prompt


def test_memory_absent_when_empty(tmp_path):
    assert "## Memory" not in build_system_prompt(_config(tmp_path))


def test_memory_blocked_by_no_skills(tmp_path):
    mem_dir = tmp_path / "memories"
    mem_dir.mkdir()
    (mem_dir / "MEMORY.md").write_text("§ 秘密记忆\n")
    prompt = build_system_prompt(_config(tmp_path, no_skills=True))
    assert "秘密记忆" not in prompt
