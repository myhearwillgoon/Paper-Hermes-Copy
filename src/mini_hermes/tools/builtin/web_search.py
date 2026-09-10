"""web_search 工具(E5b:Tavily)。全部外呼走 cassette 层(POST)。

key 来源:ctx.config.tavily_api_key(config.yaml 或 TAVILY_API_KEY 环境变量)。
模式:ctx.config.workflows.gate_external_mode —— live=record,cassette=replay。
无 key / 请求失败 / cassette miss → 返回 `Error: ...` 字符串,绝不抛出。
"""

from __future__ import annotations

from pathlib import Path

from ...cassettes import Cassette, CassetteMissError, CassetteMode, http_request
from ..registry import Registry, ToolContext

TAVILY_URL = "https://api.tavily.com/search"
MAX_RESULT_CHARS = 20_000

_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "description": "搜索查询"},
        "max_results": {"type": "integer", "description": "返回条数,默认 5"},
    },
    "required": ["query"],
}


def _web_search(args: dict, ctx: ToolContext) -> str:
    query = (args.get("query") or "").strip()
    if not query:
        return "Error: empty query"

    config = ctx.config
    api_key = getattr(config, "tavily_api_key", None) if config else None
    if not api_key:
        return "Error: TAVILY_API_KEY not configured"

    mode = "cassette"
    cassette_dir = None
    if config is not None:
        workflows = getattr(config, "workflows", None)
        mode = getattr(workflows, "gate_external_mode", "cassette")
        paths = getattr(config, "paths", None)
        if paths is not None:
            cassette_dir = str(Path(paths.data_dir) / "cassettes")
    cassette = Cassette(
        cassette_dir or ".cassettes",
        CassetteMode.RECORD if mode == "live" else CassetteMode.REPLAY,
    )

    try:
        resp = http_request(
            "POST",
            TAVILY_URL,
            json_body={"query": query,
                       "max_results": int(args.get("max_results", 5))},
            headers={"Authorization": f"Bearer {api_key}"},
            cassette=cassette,
            timeout=30,
        )
    except CassetteMissError as e:
        return f"Error: cassette miss(未录制的查询):{e}"
    except Exception as e:
        return f"Error: Tavily 请求失败: {e}"

    if resp.status_code != 200:
        return f"Error: Tavily returned HTTP {resp.status_code}"
    try:
        results = resp.json().get("results", [])
    except Exception as e:
        return f"Error: Tavily 响应解析失败: {e}"

    lines = []
    for i, r in enumerate(results, start=1):
        lines.append(f"{i}. {r.get('title', '(no title)')}")
        lines.append(f"   {r.get('url', '')}")
        snippet = (r.get("content") or "")[:300]
        lines.append(f"   {snippet}")
    return "\n".join(lines) if lines else "(no results)"


def register_tools(registry: Registry) -> None:
    registry.register(
        "web_search",
        "Tavily 网页搜索,返回编号结果列表(标题/URL/摘要)。",
        _SCHEMA,
        _web_search,
        max_result_chars=MAX_RESULT_CHARS,
    )
