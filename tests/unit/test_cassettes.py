"""cassettes.py 单元测试:record→replay round-trip、replay miss、off 透传。

外部目标用 httpx.MockTransport 假扮(不打真实网络)。
"""

from __future__ import annotations

import httpx
import pytest

from mini_hermes.cassettes import (
    Cassette,
    CassetteMissError,
    CassetteMode,
    cassette_key,
    http_get,
)

URL = "https://api.example.test/search"


def _mock_client(payload: bytes = b'{"results": [1, 2, 3]}', status: int = 200) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, content=payload, headers={"Content-Type": "application/json"})

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_record_then_replay_round_trip(tmp_path):
    cassette_dir = tmp_path / "cassettes"

    # 1. record:透传 mock 并落盘
    record_cassette = Cassette(cassette_dir, mode=CassetteMode.RECORD)
    resp = http_get(URL, cassette=record_cassette, client=_mock_client())
    assert resp.status_code == 200
    assert resp.json() == {"results": [1, 2, 3]}
    assert record_cassette.record_count() == 1

    # 2. replay:client 换成「一旦外呼就爆炸」的 transport,证明完全走文件
    def explode(request: httpx.Request) -> httpx.Response:
        raise AssertionError("replay 模式不得发起真实请求")

    replay_cassette = Cassette(cassette_dir, mode=CassetteMode.REPLAY)
    replayed = http_get(
        URL,
        cassette=replay_cassette,
        client=httpx.Client(transport=httpx.MockTransport(explode)),
    )
    assert replayed.status_code == 200
    assert replayed.json() == {"results": [1, 2, 3]}
    assert replayed.headers["content-type"] == "application/json"


def test_replay_miss_raises(tmp_path):
    cassette = Cassette(tmp_path / "cassettes", mode=CassetteMode.REPLAY)
    with pytest.raises(CassetteMissError, match="replay miss"):
        http_get(URL, cassette=cassette, client=_mock_client())


def test_replay_miss_on_different_params(tmp_path):
    """键含 query string:参数不同 = 不同的 cassette。"""
    cassette_dir = tmp_path / "cassettes"
    record = Cassette(cassette_dir, mode=CassetteMode.RECORD)
    http_get(URL, params={"q": "a"}, cassette=record, client=_mock_client())

    replay = Cassette(cassette_dir, mode=CassetteMode.REPLAY)
    with pytest.raises(CassetteMissError):
        http_get(URL, params={"q": "b"}, cassette=replay, client=_mock_client())

    # 同参数可回放
    resp = http_get(URL, params={"q": "a"}, cassette=replay, client=_mock_client())
    assert resp.status_code == 200


def test_off_mode_passes_through(tmp_path):
    cassette = Cassette(tmp_path / "cassettes", mode=CassetteMode.OFF)
    resp = http_get(URL, cassette=cassette, client=_mock_client())
    assert resp.status_code == 200
    assert cassette.record_count() == 0  # off 不落盘


def test_no_cassette_passes_through():
    resp = http_get(URL, client=_mock_client())
    assert resp.status_code == 200


def test_key_stable_and_normalized():
    k1 = cassette_key("get", URL, b'{"b": 2, "a": 1}')
    k2 = cassette_key("GET", URL, b'{"a": 1, "b": 2}')  # 方法大小写、JSON 键序无关
    assert k1 == k2
    assert cassette_key("GET", URL) != cassette_key("GET", URL + "/other")


def test_recorded_file_is_json(tmp_path):
    import json

    cassette = Cassette(tmp_path / "c", mode=CassetteMode.RECORD)
    http_get(URL, cassette=cassette, client=_mock_client())
    files = list((tmp_path / "c").glob("*.json"))
    assert len(files) == 1
    record = json.loads(files[0].read_text(encoding="utf-8"))
    assert record["request"]["method"] == "GET"
    assert record["request"]["url"] == URL
    assert record["response"]["status"] == 200
