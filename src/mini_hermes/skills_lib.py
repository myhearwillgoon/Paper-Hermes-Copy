"""Skill 库(Q3/Q7/Q8):`<lib>/<skill-name>/SKILL.md` + YAML frontmatter。

frontmatter 模式(Hermes 布局 + 我们的生命周期扩展):
    name: slug(必填)
    description: ≤60 字符(必填)
    tags: [...]
    status: seeded | backfilled | probation | active | deprecated   # 生命周期
    origin: seeded | backfilled | learned
    evidence: {uses: N, positive: N, negative: N}
    created_at / updated_at: ISO 时间

库是 git repo(Q8):首次使用时 git init;每次 create/update 自动 commit,
消息格式 `skill: <name> <action> (<status>)`;配置了 skill_lib_remote 时
push 失败只记警告,绝不让一次 run 崩溃。

渐进披露(不变量 6):build_index() 只产 name+description+status;
全文由 skill_view 按需加载。索引缓存,变更时失效。
"""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml

from .logging import log_event

_logger = logging.getLogger("mini_hermes")

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
MAX_DESCRIPTION = 60
STATUSES = {"seeded", "backfilled", "probation", "active", "deprecated"}
ORIGINS = {"seeded", "backfilled", "learned"}


class SkillValidationError(ValueError):
    pass


@dataclass
class Skill:
    name: str
    description: str
    body: str
    tags: list[str] = field(default_factory=list)
    status: str = "probation"
    origin: str = "learned"
    evidence: dict = field(default_factory=lambda: {"uses": 0, "positive": 0, "negative": 0})
    created_at: str = ""
    updated_at: str = ""
    extra: dict = field(default_factory=dict)  # 未知 frontmatter 字段,round-trip 保留


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def validate_name(name: str) -> None:
    if not NAME_RE.match(name or ""):
        raise SkillValidationError(
            f"非法 skill 名 {name!r}:须匹配 {NAME_RE.pattern}(slug)"
        )


def validate_description(description: str) -> None:
    if not description or not description.strip():
        raise SkillValidationError("description 不能为空")
    if len(description) > MAX_DESCRIPTION:
        raise SkillValidationError(
            f"description 超长:{len(description)} > {MAX_DESCRIPTION} 字符"
        )


def _parse(text: str, source: str) -> Skill:
    if not text.startswith("---\n"):
        raise SkillValidationError(f"{source}: 缺 YAML frontmatter")
    end = text.find("\n---\n", 4)
    if end == -1:
        raise SkillValidationError(f"{source}: frontmatter 未闭合")
    meta = yaml.safe_load(text[4:end]) or {}
    body = text[end + 5:]
    name = meta.get("name", "")
    description = meta.get("description", "")
    validate_name(name)
    validate_description(description)
    status = meta.get("status", "probation")
    origin = meta.get("origin", "learned")
    if status not in STATUSES:
        raise SkillValidationError(f"{source}: 非法 status {status!r}")
    if origin not in ORIGINS:
        raise SkillValidationError(f"{source}: 非法 origin {origin!r}")
    known = {"name", "description", "tags", "status", "origin", "evidence",
             "created_at", "updated_at"}
    return Skill(
        name=name,
        description=description,
        body=body,
        tags=list(meta.get("tags") or []),
        status=status,
        origin=origin,
        evidence={"uses": 0, "positive": 0, "negative": 0,
                  **(meta.get("evidence") or {})},
        created_at=meta.get("created_at", ""),
        updated_at=meta.get("updated_at", ""),
        extra={k: v for k, v in meta.items() if k not in known},
    )


def _serialize(skill: Skill) -> str:
    meta = {
        "name": skill.name,
        "description": skill.description,
        "tags": skill.tags,
        "status": skill.status,
        "origin": skill.origin,
        "evidence": skill.evidence,
        "created_at": skill.created_at,
        "updated_at": skill.updated_at,
        **skill.extra,
    }
    return "---\n" + yaml.safe_dump(meta, allow_unicode=True, sort_keys=False) \
        + "---\n" + skill.body


class SkillLibrary:
    def __init__(self, root: str | Path, remote: Optional[str] = None):
        self.root = Path(root).expanduser()
        self.remote = remote
        self._index_cache: Optional[str] = None

    # -- 读取 -----------------------------------------------------------------

    def _path(self, name: str) -> Path:
        return self.root / name / "SKILL.md"

    def list_skills(self, include_deprecated: bool = False) -> list[Skill]:
        if not self.root.is_dir():
            return []
        skills = []
        for child in sorted(self.root.iterdir()):
            path = child / "SKILL.md"
            if child.is_dir() and path.is_file():
                skill = _parse(path.read_text(encoding="utf-8"), str(path))
                if include_deprecated or skill.status != "deprecated":
                    skills.append(skill)
        return skills

    def load(self, name: str) -> Skill:
        path = self._path(name)
        if not path.is_file():
            raise KeyError(f"skill 不存在:{name!r}")
        return _parse(path.read_text(encoding="utf-8"), str(path))

    def build_index(self) -> str:
        """INDEX 文本(每行一个 name/description/status)。空库返回 ''。"""
        if self._index_cache is not None:
            return self._index_cache
        skills = self.list_skills()
        lines = [f"- {s.name} [{s.status}]: {s.description}" for s in skills]
        self._index_cache = "\n".join(lines)
        return self._index_cache

    # -- 变更(自动 commit)------------------------------------------------------

    def create(
        self,
        name: str,
        description: str,
        body: str,
        *,
        tags: Optional[list[str]] = None,
        status: str = "probation",
        origin: str = "learned",
        commit_body: Optional[str] = None,
    ) -> Skill:
        validate_name(name)
        validate_description(description)
        if status not in STATUSES or origin not in ORIGINS:
            raise SkillValidationError(f"非法 status/origin:{status}/{origin}")
        if self._path(name).exists():
            raise SkillValidationError(f"skill 已存在:{name!r}")
        now = _now()
        skill = Skill(name=name, description=description, body=body,
                      tags=tags or [], status=status, origin=origin,
                      created_at=now, updated_at=now)
        self._write(skill, action="create", commit_body=commit_body)
        return skill

    def update(self, name: str, commit_body: Optional[str] = None, **changes) -> Skill:
        skill = self.load(name)
        if "description" in changes:
            validate_description(changes["description"])
            skill.description = changes["description"]
        if "tags" in changes:
            skill.tags = list(changes["tags"])
        if "body" in changes:
            skill.body = changes["body"]
        if "status" in changes:
            if changes["status"] not in STATUSES:
                raise SkillValidationError(f"非法 status:{changes['status']!r}")
            skill.status = changes["status"]
        if "evidence_delta" in changes:
            for key, delta in changes["evidence_delta"].items():
                skill.evidence[key] = skill.evidence.get(key, 0) + int(delta)
        skill.updated_at = _now()
        self._write(skill, action="update")
        return skill

    def seed_from_directory(self, seed_dir: str | Path) -> list[str]:
        """从目录批量导入种子(Q6 冷启动):幂等,全部种子一个 commit。"""
        seed_dir = Path(seed_dir)
        added: list[str] = []
        self._ensure_repo()
        for child in sorted(seed_dir.iterdir()):
            path = child / "SKILL.md"
            if not child.is_dir() or not path.is_file():
                continue
            if self._path(child.name).exists():
                continue  # 幂等
            skill = _parse(path.read_text(encoding="utf-8"), str(path))
            if skill.status != "seeded" or skill.origin != "seeded":
                raise SkillValidationError(
                    f"种子 {child.name} 必须是 status/origin=seeded"
                )
            dest = self._path(skill.name)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(_serialize(skill), encoding="utf-8")
            added.append(skill.name)
        if added:
            self._index_cache = None
            self._git("add", "-A")
            self._git("-c", "user.email=mini-hermes@local",
                      "-c", "user.name=mini-hermes",
                      "commit", "-m", f"skill: seed {len(added)} skills (seeded)")
            self._maybe_push()
            for name in added:
                log_event(_logger, logging.INFO, "skill_event",
                          skill_name=name, action="seed", origin="seeded")
        return added

    def _write(self, skill: Skill, action: str,
               commit_body: Optional[str] = None) -> None:
        self._ensure_repo()
        path = self._path(skill.name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_serialize(skill), encoding="utf-8")
        self._index_cache = None
        self._commit(skill.name, action, skill.status, body=commit_body)
        log_event(_logger, logging.INFO, "skill_event",
                  skill_name=skill.name, action=action, origin=skill.origin)

    # -- git(Q8)----------------------------------------------------------------

    def _git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", *args], cwd=self.root, capture_output=True, text=True,
            check=check,
        )

    def _ensure_repo(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        if not (self.root / ".git").is_dir():
            self._git("init")
            self._git("add", "-A")
            self._git("-c", "user.email=mini-hermes@local",
                      "-c", "user.name=mini-hermes",
                      "commit", "--allow-empty", "-m", "skill: init library")

    def _commit(self, name: str, action: str, status: str,
                body: Optional[str] = None) -> None:
        self._git("add", "-A")
        msg_args = ["-m", f"skill: {name} {action} ({status})"]
        if body:
            msg_args += ["-m", body]  # review 决策的 rationale 进 commit body
        self._git("-c", "user.email=mini-hermes@local",
                  "-c", "user.name=mini-hermes",
                  "commit", *msg_args)
        self._maybe_push()

    def _maybe_push(self) -> None:
        if not self.remote:
            return
        try:
            self._git("push", self.remote, "HEAD")
        except Exception as e:
            log_event(_logger, logging.WARNING, "skill_event",
                      skill_name="-", action="push_failed", error=str(e))


BASE_SYSTEM_PROMPT = "你是 mini-hermes,一个研究用 agent harness。"

# 种子目录(repo 根 skills_seed/);cli 与实验 runner 共用
SEED_DIR = Path(__file__).resolve().parents[2] / "skills_seed"


def read_memory_entries(data_dir: str | Path) -> list[str]:
    """读 MEMORY.md + USER.md 的 § 条目(memory 工具与快照注入共用)。"""
    entries: list[str] = []
    for name in ("MEMORY.md", "USER.md"):
        path = Path(data_dir) / "memories" / name
        if not path.is_file():
            continue
        entries += [
            line[2:].strip()
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.startswith("§ ") and line[2:].strip()
        ]
    return entries


def build_system_prompt(config) -> str:
    """每会话构建一次(冻结快照,Hermes 前缀缓存纪律):
    会话中新建的 skill/记忆下个会话才进快照。

    no_skills=true(PLAN §5 隔离):不注入 INDEX,也不注入 Memory。
    """
    if getattr(config, "no_skills", False):
        return BASE_SYSTEM_PROMPT
    prompt = BASE_SYSTEM_PROMPT

    lib = SkillLibrary(config.paths.skill_lib_dir,
                       remote=getattr(config, "skill_lib_remote", None))
    index = lib.build_index()
    if index:
        prompt += (
            "\n\n## Available Skills\n"
            + index
            + "\n\n以上只是索引(name+description+status);"
              "需要用某个 skill 时,调用 skill_view 工具加载完整内容。"
        )

    memories = read_memory_entries(config.paths.data_dir)
    if memories:
        prompt += "\n\n## Memory\n" + "\n".join(f"- {m}" for m in memories)
    return prompt
