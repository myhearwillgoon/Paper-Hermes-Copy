"""M2 流式 loop 端到端(PLAN §6 M2:test_loop_e2e.py)。

假端点脚本化多轮 tool-calling;真实 openai SDK 流式;真实 SQLite。
"""

from __future__ import annotations

import pytest

from mini_hermes.metrics import RunMetrics
from mini_hermes.runtime import AgentRuntime, IterationBudget, ToolRegistry, make_echo_tool
from mini_hermes.state_db import SessionDB
from tests.fakes.openai_server import (
    error_scenario,
    text_scenario,
    tool_call_scenario,
    truncated_stream_scenario,
)


@pytest.fixture()
def runtime_env(tmp_path, fake_openai):
    db = SessionDB(tmp_path / "state.db")
    db.create_session("s1")
    tools = ToolRegistry()
    tools.register(*make_echo_tool())
    deltas: list[str] = []
    metrics = RunMetrics(session_id="s1")
    rt = AgentRuntime(
        db, "s1",
        base_url=fake_openai.base_url, api_key="fake", model="fake-model",
        tools=tools, metrics=metrics, cwd=tmp_path,
        on_text_delta=deltas.append,
    )
    yield db, rt, deltas, metrics
    db.close()


def _roles_contents(db):
    return [(r["role"], r["content"]) for r in db.get_messages("s1")]


# ------------------------------------------------------------ 多轮 tool-calling


def test_multi_turn_tool_calling_stream(runtime_env, fake_openai):
    db, rt, deltas, metrics = runtime_env
    fake_openai.queue_scenario(
        tool_call_scenario([{"id": "c1", "name": "echo", "arguments": '{"text": "hi"}'}])
    )
    fake_openai.queue_scenario(text_scenario("第一轮<think>藏</think>回答"))
    fake_openai.queue_scenario(text_scenario("第二轮回答"))

    reply1 = rt.run_turn("问 1")
    assert reply1 == "第一轮回答"  # think 已剥离
    assert "".join(deltas) == "第一轮回答"  # 流式重组 = 最终可见文本
    assert _roles_contents(db) == [
        ("user", "问 1"),
        ("assistant", None),
        ("tool", "echo: hi"),
        ("assistant", "第一轮回答"),
    ]
    # think 内容进了 reasoning 列,不在 content 里
    raw = db._conn.execute(
        "SELECT reasoning, content FROM messages WHERE role='assistant' AND content IS NOT NULL"
    ).fetchone()
    assert raw["reasoning"] == "藏"
    assert "藏" not in raw["content"]
    # usage chunk 记账(stream_options.include_usage 生效)
    assert metrics.main_tokens.input_tokens > 0

    deltas.clear()
    assert rt.run_turn("问 2") == "第二轮回答"
    assert len(db.get_messages("s1")) == 6


# --------------------------------------------------------------- budget 耗尽


def test_iteration_budget_exhausted_graceful(runtime_env, fake_openai):
    db, rt, _, _ = runtime_env
    rt.budget = IterationBudget(2)
    for _ in range(5):  # 模型永远要求调工具 → 撞预算
        fake_openai.queue_scenario(
            tool_call_scenario([{"name": "echo", "arguments": "{}"}])
        )
    reply = rt.run_turn("loop")
    assert "budget exhausted" in reply
    rows = db.get_messages("s1")
    assert rows[-1]["finish_reason"] == "budget_exhausted"  # 通知已落盘
    assert rt.budget.remaining == 0


# ------------------------------------------------------------------ 重试


def test_retry_429_then_success(runtime_env, fake_openai, monkeypatch):
    monkeypatch.setattr("mini_hermes.runtime.RETRY_BASE_S", 0.01)
    db, rt, _, _ = runtime_env
    fake_openai.queue_scenario(error_scenario(429))
    fake_openai.queue_scenario(text_scenario("挺过来了"))

    assert rt.run_turn("hi") == "挺过来了"
    # 不变量 2:重试不产生重复行 —— 恰好 user + assistant 两行
    rows = db.get_messages("s1")
    assert _roles_contents(db) == [("user", "hi"), ("assistant", "挺过来了")]
    assert len({r["_rowid"] for r in rows}) == 2


def test_retry_mid_stream_truncation(runtime_env, fake_openai, monkeypatch):
    """流式中途断连:SDK v2 漏出裸 httpx.RemoteProtocolError —— 必须被重试捕获。

    已知且接受的边界:失败 attempt 已流出的半截文本会出现在显示回调里
    (streaming UX 的固有取舍),但持久化 transcript 必须干净。
    """
    monkeypatch.setattr("mini_hermes.runtime.RETRY_BASE_S", 0.01)
    db, rt, deltas, _ = runtime_env
    fake_openai.queue_scenario(truncated_stream_scenario())
    fake_openai.queue_scenario(text_scenario("复活"))

    assert rt.run_turn("hi") == "复活"
    assert "".join(deltas).endswith("复活")  # 显示层可能有失败 attempt 的残片
    rows = db.get_messages("s1")
    assert [(r["role"], r["content"]) for r in rows] == [
        ("user", "hi"), ("assistant", "复活"),
    ]  # 断连的半截消息没有落盘、没有重复行


def test_retry_exhausted_surfaces(runtime_env, fake_openai, monkeypatch):
    from mini_hermes.runtime import TurnError

    monkeypatch.setattr("mini_hermes.runtime.RETRY_BASE_S", 0.01)
    db, rt, _, _ = runtime_env
    for _ in range(4):
        fake_openai.queue_scenario(error_scenario(500))
    with pytest.raises(TurnError):
        rt.run_turn("hi")
    assert _roles_contents(db) == [("user", "hi")]  # 只有 crash-persist 的用户行


def test_400_not_retried(runtime_env, fake_openai):
    """4xx(非 429)是请求问题,重试无意义,立即上浮。"""
    from mini_hermes.runtime import TurnError

    db, rt, _, _ = runtime_env
    fake_openai.queue_scenario(error_scenario(400, "bad request (fake)"))
    fake_openai.queue_scenario(text_scenario("不应被消费"))
    with pytest.raises(TurnError):
        rt.run_turn("hi")
    # 第二个场景还在队列里 = 没有重试发生
    assert len(fake_openai._queue) == 1
