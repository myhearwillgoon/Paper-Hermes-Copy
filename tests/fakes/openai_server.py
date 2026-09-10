"""假 OpenAI 兼容端点(PLAN §11 测试金字塔基座)。

stdlib http.server 实现 `POST /v1/chat/completions`,兼容官方 openai SDK:
- stream=false:返回标准 chat.completion JSON
- stream=true :SSE(`data:` 帧 + `[DONE]`),支持 tool_calls delta 流
- 脚本化场景队列(按请求顺序消费)+ `X-Fake-Scenario` 头按名选择
- 错误注入:429 / 500 / 超时(长睡眠,daemon 线程)/ 流式截断 / 畸形 JSON
"""

from __future__ import annotations

import json
import socket
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional

SCENARIO_HEADER = "X-Fake-Scenario"

# ---------------------------------------------------------------------------
# 场景构造器(场景就是一个 dict,kind 决定服务器行为)
# ---------------------------------------------------------------------------


def text_scenario(content: str, finish_reason: str = "stop") -> dict:
    """普通文本响应。stream 时按固定片长切分 delta。"""
    return {"kind": "text", "content": content, "finish_reason": finish_reason}


def tool_call_scenario(
    tool_calls: list[dict],
    content: Optional[str] = None,
    finish_reason: str = "tool_calls",
) -> dict:
    """工具调用响应。tool_calls 元素:{"id"?, "name", "arguments"(JSON 字符串)}。"""
    normalized = []
    for i, tc in enumerate(tool_calls):
        normalized.append(
            {
                "id": tc.get("id", f"call_fake_{i}"),
                "name": tc["name"],
                "arguments": tc["arguments"],
            }
        )
    return {
        "kind": "tool_call",
        "content": content,
        "tool_calls": normalized,
        "finish_reason": finish_reason,
    }


def error_scenario(status: int, message: Optional[str] = None) -> dict:
    """HTTP 错误注入。429 自动带 Retry-After 头。"""
    defaults = {
        429: ("Rate limit exceeded (fake)", "rate_limit_error", "rate_limit_exceeded"),
        500: ("Internal server error (fake)", "server_error", "internal_error"),
    }
    msg, etype, ecode = defaults.get(status, (f"Fake error {status}", "api_error", None))
    return {
        "kind": "error",
        "status": status,
        "message": message or msg,
        "error_type": etype,
        "error_code": ecode,
    }


def timeout_scenario(delay: float = 30.0) -> dict:
    """超时注入:接受连接但 delay 秒内不响应(测试端用短 client timeout)。"""
    return {"kind": "timeout", "delay": delay}


def truncated_stream_scenario() -> dict:
    """流式截断:SSE 中途断连(声明的 Content-Length 大于实际发送量)。"""
    return {"kind": "truncated_stream"}


def malformed_json_scenario() -> dict:
    """畸形 JSON:200 状态 + 无法解析的 body(供鲁棒性测试)。"""
    return {"kind": "malformed_json"}


# ---------------------------------------------------------------------------
# 服务器
# ---------------------------------------------------------------------------


def _chunk(completion_id: str, model: str, delta: dict, finish_reason=None) -> str:
    payload = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {"index": 0, "delta": delta, "finish_reason": finish_reason}
        ],
    }
    return f"data: {json.dumps(payload)}\n\n"


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # 安静一点,测试输出别被 access log 淹没
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        pass

    # -- 基础工具 ----------------------------------------------------------

    @property
    def _server_handle(self) -> "FakeOpenAIServer":
        return self.server.handle  # type: ignore[attr-defined]

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def _send_json(self, status: int, obj: Any, extra_headers: Optional[dict] = None) -> None:
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _send_raw(self, status: int, body: bytes, content_type: str = "text/plain") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # -- 路由 ---------------------------------------------------------------

    def do_GET(self) -> None:
        if self.path == "/health":
            self._send_json(200, {"status": "ok"})
        else:
            self._send_json(404, {"error": {"message": "not found"}})

    def do_POST(self) -> None:
        body = self._read_body()
        handle = self._server_handle
        handle.record_request(self.command, self.path, dict(self.headers), body)

        if self.path != "/v1/chat/completions":
            self._send_json(404, {"error": {"message": f"unknown path {self.path}"}})
            return

        try:
            request = json.loads(body) if body else {}
        except json.JSONDecodeError:
            self._send_json(
                400, {"error": {"message": "invalid request JSON", "type": "invalid_request_error"}}
            )
            return

        scenario = handle.pick_scenario(self.headers.get(SCENARIO_HEADER))
        self._serve_scenario(scenario, request)

    # -- 场景分发 -----------------------------------------------------------

    def _serve_scenario(self, scenario: dict, request: dict) -> None:
        kind = scenario.get("kind", "text")
        stream = bool(request.get("stream"))

        if kind == "timeout":
            # 接受连接但长时间不响应;daemon 线程保证服务器可关闭
            time.sleep(float(scenario.get("delay", 30.0)))
            self._send_json(200, {"note": "fake timeout elapsed"})
            return

        if kind == "error":
            status = int(scenario["status"])
            headers = {"Retry-After": "1"} if status == 429 else None
            self._send_json(
                status,
                {
                    "error": {
                        "message": scenario["message"],
                        "type": scenario["error_type"],
                        "param": None,
                        "code": scenario["error_code"],
                    }
                },
                extra_headers=headers,
            )
            return

        if kind == "malformed_json":
            self._send_raw(200, b'{"id": "chatcmpl-fake", "choices": [{{BROKEN', "application/json")
            return

        if kind == "truncated_stream":
            self._serve_truncated_stream(request)
            return

        if stream:
            self._serve_stream(scenario, request)
        else:
            self._serve_json_completion(scenario, request)

    # -- 正常响应 -----------------------------------------------------------

    def _message_payload(self, scenario: dict) -> dict:
        message: dict = {"role": "assistant", "content": scenario.get("content")}
        if scenario["kind"] == "tool_call":
            message["tool_calls"] = [
                {
                    "id": tc["id"],
                    "type": "function",
                    "function": {"name": tc["name"], "arguments": tc["arguments"]},
                }
                for tc in scenario["tool_calls"]
            ]
        return message

    def _serve_json_completion(self, scenario: dict, request: dict) -> None:
        completion = {
            "id": f"chatcmpl-fake-{uuid.uuid4().hex[:12]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": request.get("model", "fake-model"),
            "choices": [
                {
                    "index": 0,
                    "message": self._message_payload(scenario),
                    "finish_reason": scenario.get("finish_reason", "stop"),
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }
        self._send_json(200, completion)

    def _serve_stream(self, scenario: dict, request: dict) -> None:
        completion_id = f"chatcmpl-fake-{uuid.uuid4().hex[:12]}"
        model = request.get("model", "fake-model")

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

        frames = [_chunk(completion_id, model, {"role": "assistant"})]
        piece = 8  # 固定片长切分,模拟真实流式

        content = scenario.get("content")
        if content:
            for i in range(0, len(content), piece):
                frames.append(_chunk(completion_id, model, {"content": content[i : i + piece]}))

        for index, tc in enumerate(scenario.get("tool_calls", [])):
            frames.append(
                _chunk(
                    completion_id,
                    model,
                    {
                        "tool_calls": [
                            {
                                "index": index,
                                "id": tc["id"],
                                "type": "function",
                                "function": {"name": tc["name"], "arguments": ""},
                            }
                        ]
                    },
                )
            )
            args = tc["arguments"]
            for i in range(0, len(args), piece):
                frames.append(
                    _chunk(
                        completion_id,
                        model,
                        {"tool_calls": [{"index": index, "function": {"arguments": args[i : i + piece]}}]},
                    )
                )

        frames.append(
            _chunk(completion_id, model, {}, scenario.get("finish_reason", "stop"))
        )
        if (request.get("stream_options") or {}).get("include_usage"):
            usage_payload = {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": model,
                "choices": [],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            }
            frames.append(f"data: {json.dumps(usage_payload)}\n\n")
        frames.append("data: [DONE]\n\n")

        for frame in frames:
            self.wfile.write(frame.encode("utf-8"))
            self.wfile.flush()

    def _serve_truncated_stream(self, request: dict) -> None:
        """声明超大 Content-Length,写到一半直接掐断底层连接。"""
        completion_id = f"chatcmpl-fake-{uuid.uuid4().hex[:12]}"
        model = request.get("model", "fake-model")

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", "100000")  # 故意谎报
        self.end_headers()

        partial = _chunk(completion_id, model, {"role": "assistant"})
        partial += _chunk(completion_id, model, {"content": "partial"})
        partial += "data: {"  # 半个 JSON 帧,截断点
        try:
            self.wfile.write(partial.encode("utf-8"))
            self.wfile.flush()
        finally:
            self.close_connection = True
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self.connection.close()


class _Server(ThreadingHTTPServer):
    daemon_threads = True   # 超时场景的长睡眠线程不阻塞进程退出
    block_on_close = False  # server_close 不等这些线程

    def __init__(self, *args: Any, handle: "FakeOpenAIServer", **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.handle = handle


class FakeOpenAIServer:
    """假端点句柄:生命周期 + 场景编程 + 请求记录。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._queue: list[dict] = []
        self._named: dict[str, dict] = {}
        self.requests: list[dict] = []
        self._httpd = _Server(("127.0.0.1", 0), _Handler, handle=self)
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )

    # -- 生命周期 -----------------------------------------------------------

    def start(self) -> "FakeOpenAIServer":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)

    def __enter__(self) -> "FakeOpenAIServer":
        return self.start()

    def __exit__(self, *exc: Any) -> None:
        self.stop()

    # -- 属性 ----------------------------------------------------------------

    @property
    def base_url(self) -> str:
        host, port = self._httpd.server_address[:2]
        return f"http://{host}:{port}/v1"

    # -- 场景编程 -------------------------------------------------------------

    def queue_scenario(self, scenario: dict) -> None:
        """追加到 FIFO 队列(无 X-Fake-Scenario 头时按请求顺序消费)。"""
        with self._lock:
            self._queue.append(scenario)

    def set_scenario(self, name: str, scenario: dict) -> None:
        """注册命名场景,用 X-Fake-Scenario: <name> 头选择(可重复使用)。"""
        with self._lock:
            self._named[name] = scenario

    def pick_scenario(self, header_name: Optional[str]) -> dict:
        with self._lock:
            if header_name and header_name in self._named:
                return self._named[header_name]
            if self._queue:
                return self._queue.pop(0)
            return text_scenario("fake response")  # 兜底

    # -- 请求记录 -------------------------------------------------------------

    def record_request(self, method: str, path: str, headers: dict, body: bytes) -> None:
        with self._lock:
            self.requests.append(
                {"method": method, "path": path, "headers": headers, "body": body}
            )
