"""AgentRuntime —— M2:流式 loop(E4)。落盘链与 M1 完全一致(冻结)。

- openai SDK 流式调用(lazy import:DrvFS 上 ~13s,子进程只在真调 API 时付)
- ThinkScrubber:<think> 块从显示/持久化 content 剥离,进 reasoning 列
- IterationBudget:每次 API 调用扣 1,耗尽优雅结束并落 budget_exhausted 通知
- 重试:指数退避(base 1s, factor 2, 至多 4 次)429/5xx/网络错误;
  关键(M0 发现):openai.APIError 与裸 httpx 传输错误都要捕获 ——
  SDK v2 不包装流式中途断连(httpx.RemoteProtocolError 逃逸)
- 组件 #6/#7/#8(resume / 主循环+中断 / DaemonThreads)语义不变
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

import httpx

from . import persistence
from .barrier import TestBarrier
from .compressor import Compressor
from .logging import log_event
from .metrics import RunMetrics
from .state_db import SessionDB
from .think_scrubber import ThinkScrubber
from .tools.registry import Registry, ToolContext

_logger = logging.getLogger("mini_hermes")

INTERRUPTED_TOOL_RESULT = (
    "[mini-hermes] tool execution interrupted by crash; result unavailable"
)

# M1 兼容别名(tests/ 与 cli.py 的旧导入路径)
ToolRegistry = Registry

RETRY_ATTEMPTS = 4
RETRY_BASE_S = 1.0
RETRY_FACTOR = 2.0


class TurnError(RuntimeError):
    """一轮失败(已捕获、已落盘),上浮给调用方。"""


class _Interrupted(Exception):
    """流式中途检测到中断标志(内部控制流,不对外)。"""


class IterationBudget:
    """每次 API 调用扣 1(E4:IterationBudget)。"""

    def __init__(self, limit: int = 90):
        self.limit = limit
        self.remaining = limit

    def consume(self) -> bool:
        """有额度则扣 1 返回 True;耗尽返回 False。"""
        if self.remaining <= 0:
            return False
        self.remaining -= 1
        return True


def make_echo_tool() -> tuple[str, str, dict, Callable[[dict, ToolContext], str]]:
    return (
        "echo",
        "回显输入文本(测试工具)。",
        {"type": "object", "properties": {"text": {"type": "string"}}},
        lambda args, ctx: f"echo: {args.get('text', '')}",
    )


def make_write_marker_file_tool(
    path: str,
) -> tuple[str, str, dict, Callable[[dict, ToolContext], str]]:
    """带真实副作用的测试工具:每次调用追加一行到 marker 文件。"""

    def handler(args: dict, ctx: ToolContext) -> str:
        with open(path, "a", encoding="utf-8") as f:
            f.write(str(args.get("text", "mark")) + "\n")
        return "marked"

    return (
        "write_marker_file",
        "追加一行到 marker 文件(测试副作用)。",
        {"type": "object", "properties": {"text": {"type": "string"}}},
        handler,
    )


# ---------------------------------------------------------------------------
# #8 DaemonThreads
# ---------------------------------------------------------------------------


class DaemonThreads:
    """命名 daemon 循环管理器。tick 抛任何异常(含 SystemExit)只记日志。"""

    def __init__(self, logger: Optional[logging.Logger] = None):
        self._logger = logger or _logger
        self._stop = threading.Event()
        self._loops: list[tuple[str, Callable[[], None], float]] = []
        self._recoveries: list[tuple[str, Callable[[], None]]] = []
        self._threads: list[threading.Thread] = []

    def add_loop(self, name: str, tick: Callable[[], None], interval_s: float) -> None:
        self._loops.append((name, tick, interval_s))

    def add_recovery(self, name: str, fn: Callable[[], None]) -> None:
        """启动时执行一次:恢复被中断的状态(如清理 stale running 标记)。"""
        self._recoveries.append((name, fn))

    def start(self) -> None:
        for name, fn in self._recoveries:
            try:
                fn()
            except BaseException as e:
                log_event(self._logger, logging.ERROR, "daemon_thread_error",
                          thread=name, phase="recovery", error=repr(e))

        def runner(name: str, tick: Callable[[], None], interval_s: float) -> None:
            while not self._stop.wait(interval_s):
                try:
                    tick()
                except BaseException as e:  # 永不死(不变量:长命线程吞异常)
                    log_event(self._logger, logging.ERROR, "daemon_thread_error",
                              thread=name, error=repr(e))

        for name, tick, interval_s in self._loops:
            t = threading.Thread(
                target=runner, args=(name, tick, interval_s),
                name=f"mini-hermes-{name}", daemon=True,
            )
            t.start()
            self._threads.append(t)

    def stop(self, wait_s: float = 2.0) -> None:
        self._stop.set()
        for t in self._threads:
            t.join(timeout=wait_s)


# ---------------------------------------------------------------------------
# #6/#7 AgentRuntime
# ---------------------------------------------------------------------------


class AgentRuntime:
    def __init__(
        self,
        db: SessionDB,
        session_id: str,
        *,
        base_url: str,
        api_key: str,
        model: str,
        aux_base_url: Optional[str] = None,   # 缺省回退主模型(PLAN E6)
        aux_api_key: Optional[str] = None,
        aux_model: Optional[str] = None,
        compression: Any = None,  # CompressionConfig;None = 不压缩
        system_prompt: Optional[str] = None,  # 会话级冻结快照(M5a 渐进披露)
        tools: Optional[Registry] = None,
        budget: Optional[IterationBudget] = None,
        max_iterations: Optional[int] = None,  # M1 兼容:等价于 budget.limit
        http_timeout: float = 60.0,
        barrier: Optional[TestBarrier] = None,
        watchdog: Any = None,  # watchdog.Watchdog,可选
        metrics: Optional[RunMetrics] = None,
        cwd: Optional[str | Path] = None,
        config: Any = None,
        on_text_delta: Optional[Callable[[str], None]] = None,
        on_tool_call: Optional[Callable[[str, str], None]] = None,
        logger: Optional[logging.Logger] = None,
    ):
        self.db = db
        self.session_id = session_id
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.aux_base_url = (aux_base_url or base_url).rstrip("/")
        self.aux_api_key = aux_api_key or api_key
        self.aux_model = aux_model or model
        self.compression = compression
        self.system_prompt = system_prompt
        self.tools = tools or Registry()
        self.budget = budget or IterationBudget(max_iterations or 90)
        self.http_timeout = http_timeout
        self.barrier = barrier or TestBarrier.from_env()
        self.watchdog = watchdog
        self.metrics = metrics
        self.cwd = Path(cwd) if cwd else Path.cwd()
        self.config = config
        self.on_text_delta = on_text_delta
        self.on_tool_call = on_tool_call
        self.logger = logger or _logger

        self.messages: list[dict] = []
        self.user_turns = 0
        self.current_phase: Optional[str] = None  # workflow 引擎设置(M5b 归因)
        self._interrupt = threading.Event()
        self._client: Any = None      # lazy openai.OpenAI(主)
        self._aux_openai: Any = None  # lazy openai.OpenAI(辅)
        self._compressor: Optional[Compressor] = None

    # -- 中断(#7)------------------------------------------------------------

    def request_interrupt(self) -> None:
        self._interrupt.set()

    @property
    def interrupt_requested(self) -> bool:
        return self._interrupt.is_set()

    # -- 公开入口 -------------------------------------------------------------

    def run_turn(self, user_text: str) -> str:
        """一轮:用户消息 → crash-persist → loop → finalize。返回最终文本。"""
        self.messages.append({"role": "user", "content": user_text})
        self.user_turns += 1
        persistence.crash_persist(self.db, self.session_id, self.messages)  # #2
        self.barrier.hit("after_user_persist")
        return self._contained_loop()

    def resume(self) -> bool:
        """#6:按 rowid 重读重建。返回 True = 有未完成的工作应 continue_run()。"""
        rows = self.db.get_messages(self.session_id)
        self.messages = rows
        self.user_turns = sum(1 for m in rows if m["role"] == "user")
        synthesized = self._synthesize_interrupted_tool_results()
        persistence.flush_new_messages(self.db, self.session_id, self.messages)
        log_event(self.logger, logging.INFO, "resume",
                  session_id=self.session_id, rows_hydrated=len(rows),
                  synthetic_tool_results=synthesized)
        return self.needs_continuation

    @property
    def needs_continuation(self) -> bool:
        """最后一条不是「最终 assistant 消息」= 轮次未完成。"""
        if not self.messages:
            return False
        last = self.messages[-1]
        if last["role"] == "assistant" and not last.get("tool_calls"):
            return False
        return True

    def continue_run(self) -> str:
        """resume 之后续跑被中断的轮次。"""
        return self._contained_loop()

    # -- 主循环(#7)------------------------------------------------------------

    def _contained_loop(self) -> str:
        """错误隔离:任何异常 → 落盘 → 上浮 TurnError,绝不杀死进程。"""
        if self.watchdog:
            self.watchdog.turn_begin()
        try:
            return self._loop()
        except persistence.SessionPersistenceFailed:
            raise  # 不变量 1:原样上浮,调用方必须看到
        except Exception as e:
            log_event(self.logger, logging.ERROR, "turn_error",
                      session_id=self.session_id, error=repr(e), exc_info=True)
            persistence.finalize_persist(self.db, self.session_id, self.messages)
            raise TurnError(str(e)) from e
        finally:
            if self.watchdog:
                self.watchdog.turn_end()

    def _loop(self) -> str:
        while True:
            if self.interrupt_requested:
                persistence.finalize_persist(self.db, self.session_id, self.messages)
                return "[interrupted]"
            if not self.budget.consume():
                notice = {
                    "role": "assistant",
                    "content": f"[iteration budget exhausted ({self.budget.limit})]",
                    "finish_reason": "budget_exhausted",
                }
                self.messages.append(notice)
                persistence.finalize_persist(self.db, self.session_id, self.messages)
                log_event(self.logger, logging.WARNING, "budget_exhausted",
                          session_id=self.session_id, limit=self.budget.limit)
                return notice["content"]
            if self.watchdog:
                self.watchdog.beat()
            if self.compression is not None and self.compression.enabled:
                compressed = self._get_compressor().maybe_compress(self.messages)
                if compressed is not None:
                    self.messages = compressed
            self.barrier.hit("before_api_call")

            try:
                assistant = self._api_call_with_retry()
            except _Interrupted:
                persistence.finalize_persist(self.db, self.session_id, self.messages)
                return "[interrupted]"
            self.messages.append(assistant)

            if assistant.get("tool_calls"):
                # #3 失败即中止本轮(不变量 1)
                persistence.pre_side_effect_persist(
                    self.db, self.session_id, self.messages
                )
                self.barrier.hit("before_tool_exec")
                for call in assistant["tool_calls"]:
                    name = call["function"]["name"]
                    arguments = call["function"].get("arguments", "")
                    if self.on_tool_call:
                        self.on_tool_call(name, arguments)
                    ctx = ToolContext(
                        cwd=self.cwd, session_id=self.session_id,
                        config=self.config, db=self.db,
                        phase=self.current_phase,
                    )
                    result = self.tools.execute(name, arguments, ctx)
                    self.messages.append({
                        "role": "tool",
                        "tool_call_id": call["id"],
                        "name": name,
                        "content": result,
                    })
                    # 工具结果执行后立即落盘
                    persistence.flush_new_messages(
                        self.db, self.session_id, self.messages
                    )
                    self.barrier.hit("after_tool_exec")
                continue

            self.barrier.hit("before_finalize")
            persistence.finalize_persist(self.db, self.session_id, self.messages)  # #5
            return assistant.get("content") or ""

    # -- resume 辅助 -----------------------------------------------------------

    def _synthesize_interrupted_tool_results(self) -> int:
        """崩溃恢复语义:最后一个带 tool_calls 的 assistant 消息若缺 tool-result
        行,补合成行('execution interrupted by crash'),绝不重新执行工具。"""
        last_call_idx = None
        for i in range(len(self.messages) - 1, -1, -1):
            m = self.messages[i]
            if m["role"] == "assistant" and m.get("tool_calls"):
                last_call_idx = i
                break
            if m["role"] == "assistant":
                break  # 最终 assistant 消息在后 = 轮次已完成
        if last_call_idx is None:
            return 0

        calls = self.messages[last_call_idx]["tool_calls"]
        answered = {
            m.get("tool_call_id")
            for m in self.messages[last_call_idx + 1:]
            if m["role"] == "tool"
        }
        synthesized = 0
        for call in calls:
            if call["id"] not in answered:
                self.messages.append({
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "name": call["function"]["name"],
                    "content": INTERRUPTED_TOOL_RESULT,
                })
                synthesized += 1
        return synthesized

    # -- API(openai SDK 流式 + 重试)--------------------------------------------

    def _openai_client(self):
        """lazy import:DrvFS 上 import openai ~13s,只有真调 API 才付。"""
        if self._client is None:
            import openai

            self._client = openai.OpenAI(
                base_url=self.base_url,
                api_key=self.api_key,
                max_retries=0,  # 重试由 runtime 自己控制
                timeout=self.http_timeout,
            )
        return self._client

    def _api_view(self) -> list[dict]:
        view = []
        if self.system_prompt:
            view.append({"role": "system", "content": self.system_prompt})
        for m in self.messages:
            if m.get("_ephemeral"):
                continue
            if m["role"] == "tool":
                view.append({"role": "tool", "tool_call_id": m["tool_call_id"],
                             "content": m.get("content") or ""})
            elif m["role"] == "assistant":
                item: dict = {"role": "assistant", "content": m.get("content")}
                if m.get("tool_calls"):
                    item["tool_calls"] = m["tool_calls"]
                view.append(item)
            else:
                view.append({"role": m["role"], "content": m.get("content") or ""})
        return view

    def _api_call_with_retry(self) -> dict:
        return self._with_retry(self._api_call_stream)

    def _with_retry(self, fn):
        """指数退避重试 429/5xx/网络错误(主辅模型调用共用)。

        - 400/413 带 context-length 签名:压缩后重试一次(E8)
        - 其余 4xx:立即上浮,重试无意义
        - 落盘链 `_db_persisted` 标记保证重试不产生重复行(不变量 2)
        """
        import openai  # lazy,同 _openai_client

        delay = RETRY_BASE_S
        overflow_retried = False
        for attempt in range(1, RETRY_ATTEMPTS + 1):
            try:
                return fn()
            except _Interrupted:
                raise
            except (openai.APIError, httpx.HTTPError) as e:
                if isinstance(e, openai.APIStatusError):
                    if (
                        e.status_code in (400, 413)
                        and self._is_context_overflow(e)
                        and self.compression is not None
                        and self.compression.enabled
                        and not overflow_retried
                    ):
                        overflow_retried = True
                        log_event(self.logger, logging.WARNING, "llm_error",
                                  session_id=self.session_id, model=self.model,
                                  error_type="context_overflow", attempt=attempt,
                                  retry_in_s=0)
                        self._compress_now()
                        continue
                    if not (e.status_code == 429 or e.status_code >= 500):
                        raise  # 4xx(非 429/溢出)= 请求本身有问题
                retry_in = delay if attempt < RETRY_ATTEMPTS else None
                log_event(self.logger, logging.WARNING, "llm_error",
                          session_id=self.session_id, model=self.model, aux=False,
                          error_type=type(e).__name__,
                          status=getattr(e, "status_code", None),
                          attempt=attempt, retry_in_s=retry_in)
                if retry_in is None:
                    raise
                time.sleep(delay)
                delay *= RETRY_FACTOR
        raise AssertionError("unreachable")

    @staticmethod
    def _is_context_overflow(e: Exception) -> bool:
        msg = str(e).lower()
        return any(s in msg for s in (
            "context length", "context_length", "maximum context",
            "too many tokens", "context window",
        ))

    # -- 压缩(M4)----------------------------------------------------------------

    def _get_compressor(self) -> Compressor:
        if self._compressor is None:
            self._compressor = Compressor(
                self.db,
                self.session_id,
                threshold=self.compression.threshold,
                tail_messages=self.compression.tail_messages,
                summarize=self._aux_summarize,
                barrier=self.barrier,
                logger=self.logger,
            )
        return self._compressor

    def _compress_now(self) -> None:
        compressed = self._get_compressor().compress(self.messages)
        if compressed is not None:
            self.messages = compressed

    def _aux_client(self):
        if self._aux_openai is None:
            import openai

            self._aux_openai = openai.OpenAI(
                base_url=self.aux_base_url,
                api_key=self.aux_api_key,
                max_retries=0,
                timeout=self.http_timeout,
            )
        return self._aux_openai

    def _aux_summarize(self, prompt: str) -> str:
        """辅模型非流式摘要调用,走与主调用相同的重试包装。"""
        def call() -> str:
            resp = self._aux_client().chat.completions.create(
                model=self.aux_model,
                messages=[{"role": "user", "content": prompt}],
                stream=False,
            )
            usage = getattr(resp, "usage", None)
            if usage is not None and self.metrics is not None:
                self.metrics.aux_tokens.input_tokens += usage.prompt_tokens
                self.metrics.aux_tokens.output_tokens += usage.completion_tokens
            return resp.choices[0].message.content or ""

        return self._with_retry(call)

    def _api_call_stream(self) -> dict:
        """一次流式调用:重组 text/tool_call delta,think 清洗,usage 记账。"""
        client = self._openai_client()
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": self._api_view(),
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        schemas = self.tools.schemas()
        if schemas:
            kwargs["tools"] = schemas

        started = time.monotonic()
        scrubber = ThinkScrubber()
        visible: list[str] = []
        reasoning: list[str] = []
        tool_acc: dict[int, dict] = {}
        finish_reason: Optional[str] = None
        usage: Any = None

        stream = client.chat.completions.create(**kwargs)
        try:
            for chunk in stream:
                if self.interrupt_requested:
                    raise _Interrupted()
                if getattr(chunk, "usage", None):
                    usage = chunk.usage
                if not chunk.choices:
                    continue  # usage-only chunk
                choice = chunk.choices[0]
                delta = choice.delta
                if delta.content:
                    v, r = scrubber.feed(delta.content)
                    visible.append(v)
                    reasoning.append(r)
                    if v and self.on_text_delta:
                        self.on_text_delta(v)
                for tc in delta.tool_calls or []:
                    acc = tool_acc.setdefault(
                        tc.index, {"id": None, "name": None, "arguments": []}
                    )
                    if tc.id:
                        acc["id"] = tc.id
                    if tc.function and tc.function.name:
                        acc["name"] = tc.function.name
                    if tc.function and tc.function.arguments:
                        acc["arguments"].append(tc.function.arguments)
                if choice.finish_reason:
                    finish_reason = choice.finish_reason
        finally:
            stream.close()
        v, r = scrubber.flush()
        visible.append(v)
        reasoning.append(r)
        if v and self.on_text_delta:
            self.on_text_delta(v)

        msg: dict = {"role": "assistant", "content": "".join(visible) or None}
        think_text = "".join(reasoning)
        if think_text:
            msg["reasoning"] = think_text  # 进 messages.reasoning 列
        if tool_acc:
            msg["tool_calls"] = [
                {
                    "id": acc["id"] or f"call_{idx}",
                    "type": "function",
                    "function": {
                        "name": acc["name"],
                        "arguments": "".join(acc["arguments"]),
                    },
                }
                for idx, acc in sorted(tool_acc.items())
            ]
        if finish_reason:
            msg["finish_reason"] = finish_reason

        latency_ms = int((time.monotonic() - started) * 1000)
        in_tok = getattr(usage, "prompt_tokens", 0) if usage else 0
        out_tok = getattr(usage, "completion_tokens", 0) if usage else 0
        if usage:  # 假端点不给 usage 时容忍缺失
            self.db.add_token_usage(self.session_id, in_tok, out_tok)
            if self.metrics is not None:
                self.metrics.main_tokens.input_tokens += in_tok
                self.metrics.main_tokens.output_tokens += out_tok
        log_event(self.logger, logging.DEBUG, "llm_response",
                  session_id=self.session_id, model=self.model, aux=False,
                  finish_reason=finish_reason,
                  input_tokens=in_tok, output_tokens=out_tok,
                  latency_ms=latency_ms)
        return msg
