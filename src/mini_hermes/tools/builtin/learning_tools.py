"""学习相关四工具(PLAN §5):skill_view / skill_manage / memory / session_search。

no_skills 双重防线(隔离硬要求):
1. CLI/装配层:no_skills=true 时这四个工具根本不注册
2. 工具层:每次执行检查 ctx.config.no_skills,fail-closed 返回 Error

skill_view 每次全文加载写 skill_usage 行(不变量 3 的数据路径)。
skill_manage deprecate 走两步:工具只写 PENDING_DEPRECATION 到 state_meta,
由 CLI `skills approve-deprecation <name>` 真正生效(不变量 7:留人)。
"""

from __future__ import annotations

import json
from pathlib import Path

from ...skills_lib import SkillLibrary, SkillValidationError
from ..registry import Registry, ToolContext

NO_SKILLS_ERROR = "Error: skill/记忆工具已被 --no-skills 禁用(实验隔离)"


def _blocked(ctx: ToolContext) -> bool:
    return bool(getattr(ctx.config, "no_skills", False))


def _lib(ctx: ToolContext) -> SkillLibrary:
    config = ctx.config
    remote = getattr(config, "skill_lib_remote", None)
    return SkillLibrary(config.paths.skill_lib_dir, remote=remote)


# ------------------------------------------------------------------ skill_view


def _skill_view(args: dict, ctx: ToolContext) -> str:
    if _blocked(ctx):
        return NO_SKILLS_ERROR
    name = args.get("name", "")
    try:
        skill = _lib(ctx).load(name)
    except KeyError:
        return f"Error: skill 不存在:{name!r}"
    except SkillValidationError as e:
        return f"Error: {e}"
    if ctx.db is not None and ctx.session_id:
        ctx.db.add_skill_usage(ctx.session_id, skill.name, phase=ctx.phase)  # 不变量 3
    return f"# {skill.name}[{skill.status}]\n\n{skill.body}"


# ---------------------------------------------------------------- skill_manage


def _skill_manage(args: dict, ctx: ToolContext) -> str:
    if _blocked(ctx):
        return NO_SKILLS_ERROR
    action = args.get("action")
    name = args.get("name", "")
    lib = _lib(ctx)
    try:
        if action == "create":
            # 新创建一律进 probation(Q7:试用期代替审批 gate)
            skill = lib.create(
                name,
                args.get("description", ""),
                args.get("body", ""),
                tags=args.get("tags"),
                status="probation",
                origin="learned",
            )
            return f"created {skill.name}(status=probation)"
        if action == "update":
            changes = {}
            for key in ("description", "tags", "body", "status"):
                if key in args:
                    changes[key] = args[key]
            if "evidence_delta" in args:
                changes["evidence_delta"] = args["evidence_delta"]
            skill = lib.update(name, **changes)
            return f"updated {skill.name}(status={skill.status}, evidence={skill.evidence})"
        if action == "deprecate":
            # 不变量 7:淘汰不可逆所以留人 —— 只写待批记录,不直接生效
            if not args.get("confirm"):
                return ("Error: deprecate 需要 confirm: true;且只登记待批,"
                        "由人运行 `mini-hermes skills approve-deprecation` 生效")
            lib.load(name)  # 确认存在
            ctx.db.meta_set(
                f"pending_deprecation:{name}",
                json.dumps({"name": name, "reason": args.get("reason", "")},
                           ensure_ascii=False),
            )
            return f"PENDING_DEPRECATION recorded for {name}(等待人工批准)"
        return f"Error: 未知 action {action!r}(create/update/deprecate)"
    except (KeyError, SkillValidationError) as e:
        return f"Error: {e}"


# --------------------------------------------------------------------- memory


def _mem_path(ctx: ToolContext, which: str) -> Path:
    base = Path(ctx.config.paths.data_dir) / "memories"
    base.mkdir(parents=True, exist_ok=True)
    return base / ("USER.md" if which == "user" else "MEMORY.md")


def _mem_read(path: Path) -> list[str]:
    if not path.is_file():
        return []
    text = path.read_text(encoding="utf-8")
    return [e[2:].strip() for e in text.splitlines()
            if e.startswith("§ ") and e[2:].strip()]


def _mem_write(path: Path, entries: list[str]) -> None:
    path.write_text("".join(f"§ {e}\n" for e in entries), encoding="utf-8")


def _memory(args: dict, ctx: ToolContext) -> str:
    if _blocked(ctx):
        return NO_SKILLS_ERROR
    action = args.get("action")
    which = args.get("file", "memory")
    if which not in ("memory", "user"):
        return f"Error: file 只能是 memory|user"
    path = _mem_path(ctx, which)
    entries = _mem_read(path)

    if action == "add":
        text = (args.get("text") or "").strip()
        if not text:
            return "Error: add 需要 text"
        entries.append(text)
        _mem_write(path, entries)
        return f"added memory #{len(entries)}"
    if action == "list":
        if not entries:
            return "(empty)"
        return "\n".join(f"§{i + 1} {e}" for i, e in enumerate(entries))
    if action in ("replace", "remove"):
        idx = args.get("index")
        if not isinstance(idx, int) or not (1 <= idx <= len(entries)):
            return f"Error: 非法 index {idx!r}(共 {len(entries)} 条)"
        if action == "replace":
            text = (args.get("text") or "").strip()
            if not text:
                return "Error: replace 需要 text"
            entries[idx - 1] = text
        else:
            entries.pop(idx - 1)
        _mem_write(path, entries)
        return f"{action} ok"
    return f"Error: 未知 action {action!r}(add/replace/remove/list)"


# ------------------------------------------------------------ session_search


def _session_search(args: dict, ctx: ToolContext) -> str:
    if _blocked(ctx):
        return NO_SKILLS_ERROR
    if ctx.db is None:
        return "Error: session_search 需要 db"
    mode = args.get("mode", "search")

    if mode == "search":
        query = (args.get("query") or "").strip()
        if not query:
            return "Error: search 需要 query"
        try:
            hits = ctx.db.fts_search(query, limit=int(args.get("limit", 20)))
        except Exception as e:
            return f"Error: FTS 查询失败:{e}"
        if not hits:
            return "(no matches)"
        return "\n".join(
            f"[{h['session_id']}#{h['rowid']}] ({h['role']}) {h['snippet']}"
            for h in hits
        )
    if mode == "scroll":
        rowid = args.get("rowid")
        if not isinstance(rowid, int):
            return "Error: scroll 需要整数 rowid"
        rows = ctx.db.scroll_messages(
            rowid, before=int(args.get("before", 2)), after=int(args.get("after", 2))
        )
        return "\n".join(
            f"#{m['_rowid']} <{m['role']}> {(m.get('content') or '')[:200]}"
            for m in rows
        ) or "(empty)"
    if mode == "browse":
        sessions = ctx.db.recent_sessions(limit=int(args.get("limit", 10)))
        return "\n".join(
            f"{s['id']} [{s['source']}] {s['started_at']} msgs={s['message_count']}"
            for s in sessions
        ) or "(no sessions)"
    return f"Error: 未知 mode {mode!r}(search/scroll/browse)"


def register_tools(registry: Registry) -> None:
    registry.register(
        "skill_view",
        "按名加载 skill 的完整 SKILL.md 内容(渐进披露:索引之外的按需加载)。",
        {"type": "object", "properties": {"name": {"type": "string"}},
         "required": ["name"]},
        _skill_view,
    )
    registry.register(
        "skill_manage",
        "skill 生命周期:create(probation)/update/deprecate(两步,需人工批准)。",
        {"type": "object",
         "properties": {
             "action": {"type": "string", "enum": ["create", "update", "deprecate"]},
             "name": {"type": "string"},
             "description": {"type": "string"},
             "body": {"type": "string"},
             "tags": {"type": "array", "items": {"type": "string"}},
             "status": {"type": "string"},
             "evidence_delta": {"type": "object"},
             "confirm": {"type": "boolean"},
             "reason": {"type": "string"},
         },
         "required": ["action", "name"]},
        _skill_manage,
    )
    registry.register(
        "memory",
        "持久记忆(MEMORY.md/USER.md,§ 条目):add/replace/remove/list。",
        {"type": "object",
         "properties": {
             "action": {"type": "string", "enum": ["add", "replace", "remove", "list"]},
             "file": {"type": "string", "enum": ["memory", "user"]},
             "text": {"type": "string"},
             "index": {"type": "integer"},
         },
         "required": ["action"]},
        _memory,
    )
    registry.register(
        "session_search",
        "历史会话检索(FTS5,零 LLM 成本):search/scroll/browse。",
        {"type": "object",
         "properties": {
             "mode": {"type": "string", "enum": ["search", "scroll", "browse"]},
             "query": {"type": "string"},
             "rowid": {"type": "integer"},
             "before": {"type": "integer"},
             "after": {"type": "integer"},
             "limit": {"type": "integer"},
         }},
        _session_search,
    )
