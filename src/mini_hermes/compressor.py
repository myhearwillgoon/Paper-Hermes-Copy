"""上下文压缩(E8 简化版,不变量 5)。

头保护(system + 首轮 user/assistant)+ 尾保护(最近 N 条)+ 辅模型中段摘要。
摘要作为真实消息进 transcript(role=user,内容前缀 `[COMPACTION SUMMARY v<n>]`);
被压缩的中段消息标 compacted=1 —— 留在库里可审计/可 FTS,但不再进上下文。

不变量 5:压缩是唯一合法的历史改写,且必须全程持有 compression_locks 租约。
崩溃安全:apply_compaction 单事务 —— 事务前死 = 无变化;事务中死 = 回滚。
"""

from __future__ import annotations

import logging
import time
from typing import Callable, Optional

from .barrier import TestBarrier
from .logging import log_event
from .state_db import SessionDB

_logger = logging.getLogger("mini_hermes")

SUMMARY_MARKER = "[COMPACTION SUMMARY"
LEASE_WAIT_S = 5.0
LEASE_TTL_S = 300.0

SUMMARIZE_TEMPLATE = """把以下 agent 对话的中段历史压缩为结构化摘要,供后续轮次使用。
要求:
- 保留所有事实性结论、已完成的操作及其结果、重要的文件路径与标识符
- 丢弃客套话与重复内容
- 若输入中包含更早的 [COMPACTION SUMMARY],把它的事实合并进新摘要(摘要迭代)

输出格式(严格遵守):
## Resolved
- <已完成的工作/确立的事实,逐条>
## Pending
- <未决任务/开放问题,逐条>

对话中段:
{middle}
"""


def estimate_tokens(messages: list[dict]) -> int:
    """粗估:persisted token_count 优先,否则字符数/4。"""
    total = 0
    for m in messages:
        if m.get("token_count"):
            total += m["token_count"]
            continue
        n = len(m.get("content") or "")
        for tc in m.get("tool_calls") or []:
            n += len(tc.get("function", {}).get("arguments", ""))
        total += n // 4 + 4
    return total


def split_head_middle_tail(
    messages: list[dict], tail_n: int
) -> tuple[list[dict], list[dict], list[dict]]:
    """头 = system* + 首轮 user/assistant;尾 = 最近 tail_n 条;其余为中段。"""
    i = 0
    head: list[dict] = []
    while i < len(messages) and messages[i]["role"] == "system":
        head.append(messages[i])
        i += 1
    if i < len(messages) and messages[i]["role"] == "user":
        head.append(messages[i])
        i += 1
        if i < len(messages) and messages[i]["role"] == "assistant":
            head.append(messages[i])
            i += 1
    tail_start = max(len(messages) - tail_n, len(head))
    return head, messages[len(head):tail_start], messages[tail_start:]


class Compressor:
    def __init__(
        self,
        db: SessionDB,
        session_id: str,
        *,
        threshold: int = 60000,
        tail_messages: int = 20,
        summarize: Optional[Callable[[str], str]] = None,
        barrier: Optional[TestBarrier] = None,
        logger: Optional[logging.Logger] = None,
    ):
        self.db = db
        self.session_id = session_id
        self.threshold = threshold
        self.tail_messages = tail_messages
        self.summarize = summarize
        self.barrier = barrier or TestBarrier.from_env()
        self.logger = logger or _logger

    def maybe_compress(self, messages: list[dict]) -> Optional[list[dict]]:
        """超过阈值则压缩,返回新消息列表;否则/跳过返回 None。"""
        if estimate_tokens(messages) <= self.threshold:
            return None
        return self.compress(messages)

    def compress(self, messages: list[dict]) -> Optional[list[dict]]:
        """执行压缩。租约拿不到(5s busy wait)则本轮跳过,返回 None。"""
        head, middle, tail = split_head_middle_tail(messages, self.tail_messages)
        middle = [m for m in middle if m.get("_rowid") is not None]
        if not middle:
            log_event(self.logger, logging.DEBUG, "compression_skipped",
                      session_id=self.session_id, reason="empty middle")
            return None

        holder = self._acquire_lease()
        if holder is None:
            log_event(self.logger, logging.WARNING, "compression_skipped",
                      session_id=self.session_id, reason="lease held by live peer")
            return None
        try:
            prompt = SUMMARIZE_TEMPLATE.format(middle=self._render_middle(middle))
            self.barrier.hit("before_aux_summarize")
            summary_text = self.summarize(prompt) if self.summarize else "(no summarizer)"

            version = self.db.count_summaries(self.session_id) + 1
            covers_to = max(m["_rowid"] for m in middle)
            summary_msg = {
                "role": "user",  # 见模块 docstring:role=user + marker 前缀
                "content": f"{SUMMARY_MARKER} v{version} covers_to={covers_to}]\n{summary_text}",
            }
            self.barrier.hit("before_compaction_txn")
            rowid = self.db.apply_compaction(
                self.session_id,
                [m["_rowid"] for m in middle],
                summary_msg,
                hook=lambda: self.barrier.hit("inside_compaction_txn"),
            )
            summary_msg["_rowid"] = rowid
            summary_msg["_db_persisted"] = True
            log_event(self.logger, logging.INFO, "compression_applied",
                      session_id=self.session_id, version=version,
                      compacted_rows=len(middle))
            return head + [summary_msg] + tail
        finally:
            self.db.release_compression_lock(self.session_id, holder)

    def _acquire_lease(self) -> Optional[str]:
        """5s busy wait;活跃外部租约不退让则放弃本轮。"""
        deadline = time.monotonic() + LEASE_WAIT_S
        while True:
            holder = self.db.acquire_compression_lock(self.session_id, LEASE_TTL_S)
            if holder is not None:
                return holder
            if time.monotonic() >= deadline:
                return None
            time.sleep(0.25)

    @staticmethod
    def _render_middle(middle: list[dict]) -> str:
        parts = []
        for m in middle:
            content = m.get("content") or ""
            if m.get("tool_calls"):
                calls = ", ".join(
                    tc["function"]["name"] for tc in m["tool_calls"]
                )
                content += f" [tool_calls: {calls}]"
            parts.append(f"<{m['role']}>\n{content}")
        return "\n\n".join(parts)
