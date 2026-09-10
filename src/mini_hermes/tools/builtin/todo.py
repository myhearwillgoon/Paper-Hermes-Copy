"""todo 工具:按会话持久化的待办列表(state_meta JSON),resume 后自然水合。"""

from __future__ import annotations

import json

from ..registry import Registry, ToolContext

_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["add", "update", "list", "clear"]},
        "text": {"type": "string", "description": "add 时的待办内容"},
        "id": {"type": "integer", "description": "update 时的待办 id"},
        "status": {"type": "string", "enum": ["pending", "done"],
                   "description": "update 时的新状态"},
    },
    "required": ["action"],
}


def _key(session_id: str) -> str:
    return f"todos:{session_id}"


def _load(ctx: ToolContext) -> list[dict]:
    raw = ctx.db.meta_get(_key(ctx.session_id))
    return json.loads(raw) if raw else []


def _save(ctx: ToolContext, todos: list[dict]) -> None:
    ctx.db.meta_set(_key(ctx.session_id), json.dumps(todos, ensure_ascii=False))


def _render(todos: list[dict]) -> str:
    if not todos:
        return "(todo list empty)"
    marks = {"pending": "○", "done": "●"}
    return "\n".join(
        f"{marks.get(t['status'], '?')} #{t['id']} {t['text']}" for t in todos
    )


def _todo(args: dict, ctx: ToolContext) -> str:
    if ctx.db is None or not ctx.session_id:
        return "Error: todo 需要 db 与 session_id(ToolContext 未提供)"
    action = args.get("action")
    todos = _load(ctx)

    if action == "add":
        text = (args.get("text") or "").strip()
        if not text:
            return "Error: add 需要 text"
        next_id = max((t["id"] for t in todos), default=0) + 1
        todos.append({"id": next_id, "text": text, "status": "pending"})
        _save(ctx, todos)
        return f"added #{next_id}\n" + _render(todos)
    if action == "update":
        tid = args.get("id")
        status = args.get("status")
        if status not in ("pending", "done"):
            return "Error: update 需要合法 status(pending/done)"
        for t in todos:
            if t["id"] == tid:
                t["status"] = status
                _save(ctx, todos)
                return _render(todos)
        return f"Error: 不存在 id={tid}"
    if action == "list":
        return _render(todos)
    if action == "clear":
        _save(ctx, [])
        return "(cleared)"
    return f"Error: 未知 action {action!r}"


def register_tools(registry: Registry) -> None:
    registry.register("todo",
                      "会话级待办列表:add/update/list/clear,落盘持久化,resume 后自动恢复。",
                      _SCHEMA, _todo)
