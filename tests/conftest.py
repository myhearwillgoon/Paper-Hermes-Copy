"""pytest 公共 fixture:假 OpenAI 端点。"""

from __future__ import annotations

import pytest

from tests.fakes.openai_server import FakeOpenAIServer


@pytest.fixture()
def fake_openai():
    """启动假 OpenAI 端点(临时端口),yield 句柄。

    句柄可编程场景:`queue_scenario(...)` / `set_scenario(name, ...)`,
    `base_url` 直接传给 openai.OpenAI(base_url=...)。
    """
    server = FakeOpenAIServer().start()
    try:
        yield server
    finally:
        server.stop()
