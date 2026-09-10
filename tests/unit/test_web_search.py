"""web_search(Tavily)单元测试 + POST cassette round-trip。

Tavily cassette 为手工构造(M0 Cassette 的确切 JSON 格式,key 用生产代码
cassette_key 计算)—— 无 live Tavily key,属有意为之。
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from pathlib import Path

import httpx
import pytest

from mini_hermes.cassettes import (
    Cassette,
    CassetteMissError,
    CassetteMode,
    cassette_key,
    http_request,
)
from mini_hermes.tools.builtin.web_search import TAVILY_URL
from mini_hermes.tools.registry import Registry, ToolContext, discover_builtin_tools

QUERY = "hermes agent harness"
TAVILY_BODY = {"query": QUERY, "max_results": 5}
TAVILY_RESPONSE = {
    "results": [
        {"title": "Hermes Agent", "url": "https://example.com/hermes",
         "content": "Hermes is an agent harness..."},
        {"title": "mini-hermes", "url": "https://example.com/mini",
         "content": "A small research testbed..."},
    ]
}


def _body_bytes(d: dict) -> bytes:
    return json.dumps(d, sort_keys=True, ensure_ascii=False).encode("utf-8")


def _write_tavily_cassette(directory: Path, status: int = 200) -> None:
    """手工构造 Tavily cassette(M0 格式)。"""
    key = cassette_key("POST", TAVILY_URL, _body_bytes(TAVILY_BODY))
    record = {
        "request": {"method": "POST", "url": TAVILY_URL,
                    "body": json.dumps(TAVILY_BODY, sort_keys=True)},
        "response": {
            "status": status,
            "headers": {"content-type": "application/json"},
            "body_b64": None,
            "body": json.dumps(TAVILY_RESPONSE) if status == 200 else '{"error":"x"}',
        },
    }
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{key}.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _ctx(tmp_path, *, api_key="tvly-fake", mode="cassette"):
    config = SimpleNamespace(
        tavily_api_key=api_key,
        workflows=SimpleNamespace(gate_external_mode=mode),
        paths=SimpleNamespace(data_dir=tmp_path),
    )
    return ToolContext(cwd=tmp_path, session_id="s1", config=config)


@pytest.fixture()
def registry():
    return discover_builtin_tools(Registry())


# ------------------------------------------------------------- 工具行为


def test_web_search_cassette_replay(registry, tmp_path):
    _write_tavily_cassette(tmp_path / "cassettes")
    result = registry.execute(
        "web_search", {"query": QUERY, "max_results": 5}, _ctx(tmp_path)
    )
    assert "Hermes Agent" in result
    assert "https://example.com/mini" in result
    assert result.startswith("1. ")


def test_web_search_missing_key(registry, tmp_path):
    result = registry.execute("web_search", {"query": QUERY}, _ctx(tmp_path, api_key=None))
    assert result.startswith("Error: TAVILY_API_KEY not configured")


def test_web_search_http_error(registry, tmp_path):
    _write_tavily_cassette(tmp_path / "cassettes", status=500)
    result = registry.execute("web_search", {"query": QUERY}, _ctx(tmp_path))
    assert result.startswith("Error: Tavily returned HTTP 500")


def test_web_search_cassette_miss_is_error_not_raise(registry, tmp_path):
    (tmp_path / "cassettes").mkdir()  # 空 cassette 目录
    result = registry.execute("web_search", {"query": "never recorded"}, _ctx(tmp_path))
    assert result.startswith("Error: cassette miss")


# ------------------------------------------------------------- POST cassette round-trip


def test_post_cassette_round_trip(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        return httpx.Response(200, content=json.dumps(TAVILY_RESPONSE).encode(),
                              headers={"Content-Type": "application/json"})

    cassette_dir = tmp_path / "c"
    record = Cassette(cassette_dir, CassetteMode.RECORD)
    resp = http_request("POST", TAVILY_URL, json_body=TAVILY_BODY,
                        cassette=record, client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert resp.status_code == 200
    assert record.record_count() == 1

    def explode(request):
        raise AssertionError("replay 不得外呼")

    replay = Cassette(cassette_dir, CassetteMode.REPLAY)
    resp2 = http_request("POST", TAVILY_URL, json_body=TAVILY_BODY,
                         cassette=replay,
                         client=httpx.Client(transport=httpx.MockTransport(explode)))
    assert resp2.json()["results"][0]["title"] == "Hermes Agent"


def test_post_key_covers_normalized_body(tmp_path):
    """body 参与 key 且 JSON 键序无关。"""
    k1 = cassette_key("POST", TAVILY_URL, _body_bytes({"a": 1, "b": 2}))
    k2 = cassette_key("POST", TAVILY_URL, _body_bytes({"b": 2, "a": 1}))
    assert k1 == k2
    k3 = cassette_key("POST", TAVILY_URL, _body_bytes({"a": 1}))
    assert k1 != k3
    # 不同 body 的 replay miss
    cassette_dir = tmp_path / "c"
    _write_tavily_cassette(cassette_dir)
    replay = Cassette(cassette_dir, CassetteMode.REPLAY)
    with pytest.raises(CassetteMissError):
        http_request("POST", TAVILY_URL, json_body={"query": "other", "max_results": 5},
                     cassette=replay)
