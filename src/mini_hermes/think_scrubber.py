"""Think 清洗器(E4):流式剥离 <think>...</think>。

跨 chunk 状态机:tag 可能被拆在任意 chunk 边界(`<thi` + `nk>...`)。
feed(delta) → (visible, reasoning):visible 进显示与持久化 content,
reasoning 进 messages.reasoning 列(schema 已有)。
"""

from __future__ import annotations

TAG_OPEN = "<think>"
TAG_CLOSE = "</think>"


def _partial_suffix_len(buf: str, tag: str) -> int:
    """buf 尾部与 tag 前缀匹配的最长长度(可能是未完成 tag)。"""
    max_len = min(len(buf), len(tag) - 1)
    for n in range(max_len, 0, -1):
        if buf.endswith(tag[:n]):
            return n
    return 0


class ThinkScrubber:
    def __init__(self) -> None:
        self._in_think = False
        self._buf = ""

    def feed(self, delta: str) -> tuple[str, str]:
        """喂一个流式 delta,返回 (可见文本, 推理文本)。"""
        self._buf += delta
        visible: list[str] = []
        reasoning: list[str] = []
        while self._buf:
            tag = TAG_CLOSE if self._in_think else TAG_OPEN
            idx = self._buf.find(tag)
            if idx == -1:
                keep = _partial_suffix_len(self._buf, tag)
                emit = self._buf[: len(self._buf) - keep]
                (reasoning if self._in_think else visible).append(emit)
                self._buf = self._buf[len(self._buf) - keep:]
                break
            (reasoning if self._in_think else visible).append(self._buf[:idx])
            self._buf = self._buf[idx + len(tag):]
            self._in_think = not self._in_think
        return "".join(visible), "".join(reasoning)

    def flush(self) -> tuple[str, str]:
        """流结束时吐出残留(未闭合 think 的尾部按所在状态归类)。"""
        rest, self._buf = self._buf, ""
        return ("", rest) if self._in_think else (rest, "")
