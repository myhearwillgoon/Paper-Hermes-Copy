"""外部 API cassette 录制/回放机制(PLAN §11 外部依赖策略)。

只用于外部 API(arXiv / Semantic Scholar / Tavily 等),不用于 LLM 端点。
键:(method, url, 归一化 body)→ JSON 文件。模式:
- record:透传真实请求并落盘
- replay:从文件回放,miss 即报错(确定性保证)
- off   :直接透传(等价无 cassette)
"""

from __future__ import annotations

import hashlib
import json
import logging as _logging
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Optional

import httpx

from .logging import log_event

_logger = _logging.getLogger("mini_hermes")

# 只保留对回放有意义的响应头
_HEADER_ALLOWLIST = {"content-type", "content-encoding", "etag", "last-modified"}


class CassetteMode(str, Enum):
    RECORD = "record"
    REPLAY = "replay"
    OFF = "off"


class CassetteMissError(RuntimeError):
    """replay 模式下请求未命中 cassette。"""


def _normalize_body(body: Optional[bytes]) -> str:
    """归一化请求体:JSON 按键排序重序列化,其余原样。"""
    if not body:
        return ""
    try:
        return json.dumps(json.loads(body), sort_keys=True, ensure_ascii=False)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return body.decode("utf-8", errors="replace")


def cassette_key(method: str, url: str, body: Optional[bytes] = None) -> str:
    """(method, url, 归一化 body) → 稳定的文件名 key。"""
    digest = hashlib.sha256(
        f"{method.upper()}\n{url}\n{_normalize_body(body)}".encode("utf-8")
    ).hexdigest()
    return digest[:32]


class Cassette:
    """一个 cassette 目录:每条交互一个 JSON 文件。"""

    def __init__(self, directory: str | Path, mode: CassetteMode | str = CassetteMode.REPLAY):
        self.directory = Path(directory)
        self.mode = CassetteMode(mode)

    def _path(self, key: str) -> Path:
        return self.directory / f"{key}.json"

    def load(self, key: str) -> dict:
        path = self._path(key)
        if not path.exists():
            raise CassetteMissError(
                f"replay miss:cassette 不存在 {path}"
                f"(mode=replay 不允许真实外呼;请先以 record 模式录制)"
            )
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)

    def save(
        self,
        key: str,
        *,
        method: str,
        url: str,
        request_body: Optional[bytes],
        status: int,
        headers: Mapping[str, str],
        body: bytes,
    ) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        record = {
            "request": {
                "method": method.upper(),
                "url": url,
                "body": _normalize_body(request_body),
            },
            "response": {
                "status": status,
                "headers": {
                    k: v for k, v in headers.items() if k.lower() in _HEADER_ALLOWLIST
                },
                "body_b64": None,
                "body": None,
            },
        }
        try:
            record["response"]["body"] = body.decode("utf-8")
        except UnicodeDecodeError:
            import base64

            record["response"]["body_b64"] = base64.b64encode(body).decode("ascii")
        with self._path(key).open("w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False, indent=2)

    def record_count(self) -> int:
        if not self.directory.exists():
            return 0
        return len(list(self.directory.glob("*.json")))


class CassetteResponse:
    """replay 模式下 http_get 的返回类型(duck-type 对齐 httpx.Response 常用面)。"""

    def __init__(self, status: int, headers: Mapping[str, str], content: bytes, url: str):
        self.status_code = status
        self.headers = httpx.Headers(headers)
        self.content = content
        self.url = httpx.URL(url)

    @property
    def text(self) -> str:
        return self.content.decode("utf-8")

    def json(self) -> Any:
        return json.loads(self.content)

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"cassette replayed error status {self.status_code}",
                request=None,  # type: ignore[arg-type]
                response=None,  # type: ignore[arg-type]
            )


def http_get(
    url: str,
    *,
    cassette: Optional[Cassette] = None,
    params: Optional[Mapping[str, Any]] = None,
    headers: Optional[Mapping[str, str]] = None,
    timeout: float = 30.0,
    client: Optional[httpx.Client] = None,
) -> httpx.Response | CassetteResponse:
    """外部 API GET 的统一入口(http_request 的薄封装)。"""
    return http_request(
        "GET", url, params=params, headers=headers,
        cassette=cassette, timeout=timeout, client=client,
    )


def http_request(
    method: str,
    url: str,
    *,
    json_body: Optional[Mapping[str, Any]] = None,
    params: Optional[Mapping[str, Any]] = None,
    headers: Optional[Mapping[str, str]] = None,
    cassette: Optional[Cassette] = None,
    timeout: float = 30.0,
    client: Optional[httpx.Client] = None,
) -> httpx.Response | CassetteResponse:
    """外部 API 请求的统一入口(GET/POST 等),走 cassette 录制/回放。

    键 =(method, 最终 URL, 归一化 body)。cassette=None/off → 透传;
    record → 透传+落盘;replay → 只读文件,miss 抛 CassetteMissError。
    """
    method = method.upper()
    body_bytes = (
        json.dumps(json_body, sort_keys=True, ensure_ascii=False).encode("utf-8")
        if json_body is not None
        else None
    )
    if cassette is None or cassette.mode is CassetteMode.OFF:
        return _real_request(method, url, json_body=json_body, params=params,
                             headers=headers, timeout=timeout, client=client)

    final_url = str(httpx.URL(url, params=params)) if params else url
    key = cassette_key(method, final_url, body_bytes)

    if cassette.mode is CassetteMode.REPLAY:
        try:
            record = cassette.load(key)
        except CassetteMissError:
            log_event(
                _logger, _logging.ERROR, "cassette_miss",
                cassette_key=key, method=method, url=final_url,
            )
            raise
        log_event(
            _logger, _logging.DEBUG, "cassette_replay",
            cassette_key=key, method=method, url=final_url,
        )
        body: bytes
        if record["response"].get("body_b64") is not None:
            import base64

            body = base64.b64decode(record["response"]["body_b64"])
        else:
            body = (record["response"]["body"] or "").encode("utf-8")
        return CassetteResponse(
            status=record["response"]["status"],
            headers=record["response"]["headers"],
            content=body,
            url=final_url,
        )

    # record:透传 + 落盘
    response = _real_request(method, url, json_body=json_body, params=params,
                             headers=headers, timeout=timeout, client=client)
    cassette.save(
        key,
        method=method,
        url=final_url,
        request_body=body_bytes,
        status=response.status_code,
        headers=dict(response.headers),
        body=response.content,
    )
    log_event(
        _logger, _logging.INFO, "cassette_record",
        cassette_key=key, method=method, url=final_url, status=response.status_code,
    )
    return response


def _real_request(
    method: str,
    url: str,
    *,
    json_body: Optional[Mapping[str, Any]],
    params: Optional[Mapping[str, Any]],
    headers: Optional[Mapping[str, str]],
    timeout: float,
    client: Optional[httpx.Client],
) -> httpx.Response:
    kwargs: dict = {"params": params, "headers": headers, "timeout": timeout}
    if json_body is not None:
        kwargs["json"] = dict(json_body)
    if client is not None:
        return client.request(method, url, **kwargs)
    with httpx.Client() as c:
        return c.request(method, url, **kwargs)
