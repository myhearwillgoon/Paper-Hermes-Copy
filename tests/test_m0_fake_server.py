"""M0 验收测试:真实 openai SDK 打假端点(不用 mock)。

覆盖(PLAN §6 M0 验收标准:假端点可被 openai SDK 正常调用;错误注入可触发):
a. 非流式 chat 调用
b. 流式调用重组(含 tool_calls delta)
c. 错误注入:429 → RateLimitError;500 → InternalServerError;
   超时 → APITimeoutError;流式截断 → APIError/APIConnectionError
"""

from __future__ import annotations

import json

import httpx
import openai
import pytest

from tests.fakes.openai_server import (
    error_scenario,
    malformed_json_scenario,
    text_scenario,
    timeout_scenario,
    tool_call_scenario,
    truncated_stream_scenario,
)


def _client(fake_openai, **kwargs) -> openai.OpenAI:
    return openai.OpenAI(
        base_url=fake_openai.base_url,
        api_key="fake-key",
        max_retries=0,  # 错误注入必须立即浮出,不被 SDK 重试吞掉
        **kwargs,
    )


# ---------------------------------------------------------------- a. 非流式


def test_non_stream_chat_completion(fake_openai):
    fake_openai.queue_scenario(text_scenario("你好,mini-hermes"))
    client = _client(fake_openai)

    resp = client.chat.completions.create(
        model="fake-model",
        messages=[{"role": "user", "content": "hi"}],
    )

    assert resp.choices[0].message.content == "你好,mini-hermes"
    assert resp.choices[0].finish_reason == "stop"
    assert resp.usage.total_tokens > 0
    # 服务器确实收到了 SDK 的真实请求
    assert fake_openai.requests[0]["path"] == "/v1/chat/completions"
    sent = json.loads(fake_openai.requests[0]["body"])
    assert sent["model"] == "fake-model"


def test_non_stream_tool_call(fake_openai):
    fake_openai.queue_scenario(
        tool_call_scenario(
            [{"name": "web_search", "arguments": '{"query": "hermes agent"}'}]
        )
    )
    client = _client(fake_openai)

    resp = client.chat.completions.create(
        model="fake-model",
        messages=[{"role": "user", "content": "search"}],
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "web_search",
                    "parameters": {"type": "object", "properties": {"query": {"type": "string"}}},
                },
            }
        ],
    )

    assert resp.choices[0].finish_reason == "tool_calls"
    tc = resp.choices[0].message.tool_calls[0]
    assert tc.function.name == "web_search"
    assert json.loads(tc.function.arguments) == {"query": "hermes agent"}


# ----------------------------------------------------------------- b. 流式


def test_stream_reassembles_text(fake_openai):
    fake_openai.queue_scenario(text_scenario("流式响应应当被完整重组"))
    client = _client(fake_openai)

    chunks = list(
        client.chat.completions.create(
            model="fake-model",
            messages=[{"role": "user", "content": "hi"}],
            stream=True,
        )
    )

    text = "".join(
        c.choices[0].delta.content for c in chunks if c.choices[0].delta.content
    )
    assert text == "流式响应应当被完整重组"
    assert chunks[-1].choices[0].finish_reason == "stop"


def test_stream_reassembles_tool_call(fake_openai):
    arguments = '{"query": "hermes agent 流式工具调用", "top_k": 5}'
    fake_openai.queue_scenario(
        tool_call_scenario([{"id": "call_abc", "name": "web_search", "arguments": arguments}])
    )
    client = _client(fake_openai)

    chunks = list(
        client.chat.completions.create(
            model="fake-model",
            messages=[{"role": "user", "content": "search"}],
            stream=True,
        )
    )

    # 从 delta 流重组 tool_call
    tool_id = tool_name = None
    args_parts: list[str] = []
    for c in chunks:
        for tc in c.choices[0].delta.tool_calls or []:
            if tc.id:
                tool_id = tc.id
            if tc.function and tc.function.name:
                tool_name = tc.function.name
            if tc.function and tc.function.arguments:
                args_parts.append(tc.function.arguments)

    assert tool_id == "call_abc"
    assert tool_name == "web_search"
    assert "".join(args_parts) == arguments
    assert chunks[-1].choices[0].finish_reason == "tool_calls"


# -------------------------------------------------------------- c. 错误注入


def test_429_raises_rate_limit_error(fake_openai):
    fake_openai.set_scenario("rate_limited", error_scenario(429))
    client = _client(fake_openai)

    with pytest.raises(openai.RateLimitError) as exc_info:
        client.chat.completions.create(
            model="fake-model",
            messages=[{"role": "user", "content": "hi"}],
            extra_headers={"X-Fake-Scenario": "rate_limited"},
        )
    assert exc_info.value.response.status_code == 429
    assert exc_info.value.response.headers.get("retry-after") == "1"


def test_500_raises_internal_server_error(fake_openai):
    fake_openai.set_scenario("server_error", error_scenario(500))
    client = _client(fake_openai)

    with pytest.raises(openai.InternalServerError) as exc_info:
        client.chat.completions.create(
            model="fake-model",
            messages=[{"role": "user", "content": "hi"}],
            extra_headers={"X-Fake-Scenario": "server_error"},
        )
    assert exc_info.value.response.status_code == 500


def test_timeout_raises_api_timeout_error(fake_openai):
    fake_openai.set_scenario("hang", timeout_scenario(delay=30.0))
    client = _client(fake_openai, timeout=0.5)  # 短客户端超时

    with pytest.raises(openai.APITimeoutError):
        client.chat.completions.create(
            model="fake-model",
            messages=[{"role": "user", "content": "hi"}],
            extra_headers={"X-Fake-Scenario": "hang"},
        )


def test_truncated_stream_raises_api_error(fake_openai):
    """流式截断:SSE 中途断连。

    注意(M0 重要发现):openai SDK v2 只在「请求发起阶段」把传输错误包装成
    APIConnectionError;流式迭代中途的断连以原始 httpx.RemoteProtocolError
    浮出(见 _streaming.py:Stream.__iter__ 无包装)。M2 loop 的重试逻辑(E4)
    必须同时捕获 openai.APIError 与 httpx 传输错误。
    """
    fake_openai.set_scenario("truncated", truncated_stream_scenario())
    client = _client(fake_openai)

    with pytest.raises((openai.APIError, httpx.HTTPError)):
        with client.chat.completions.create(
            model="fake-model",
            messages=[{"role": "user", "content": "hi"}],
            stream=True,
            extra_headers={"X-Fake-Scenario": "truncated"},
        ) as stream:
            for _ in stream:
                pass


def test_malformed_json_injected_once(fake_openai):
    """畸形 JSON 注入:第一次畸形,第二次(队列消费后)正常 —— 证明'once'语义。"""
    fake_openai.queue_scenario(malformed_json_scenario())
    fake_openai.queue_scenario(text_scenario("恢复后的正常响应"))
    client = _client(fake_openai)

    # SDK v2 对 200 + 畸形 JSON 不包装,原始 JSONDecodeError(ValueError 子类)浮出
    with pytest.raises((openai.APIError, ValueError)):
        client.chat.completions.create(
            model="fake-model", messages=[{"role": "user", "content": "hi"}]
        )

    resp = client.chat.completions.create(
        model="fake-model", messages=[{"role": "user", "content": "hi"}]
    )
    assert resp.choices[0].message.content == "恢复后的正常响应"


def test_scenario_queue_consumed_in_order(fake_openai):
    """脚本化队列按请求顺序消费。"""
    fake_openai.queue_scenario(text_scenario("第一次"))
    fake_openai.queue_scenario(text_scenario("第二次"))
    client = _client(fake_openai)

    r1 = client.chat.completions.create(model="m", messages=[{"role": "user", "content": "1"}])
    r2 = client.chat.completions.create(model="m", messages=[{"role": "user", "content": "2"}])
    assert r1.choices[0].message.content == "第一次"
    assert r2.choices[0].message.content == "第二次"
