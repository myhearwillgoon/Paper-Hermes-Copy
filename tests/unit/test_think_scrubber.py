"""ThinkScrubber 单元测试:tag 跨 chunk 边界的所有切法。"""

from __future__ import annotations

import itertools

import pytest

from mini_hermes.think_scrubber import ThinkScrubber


def _run(chunks):
    s = ThinkScrubber()
    visible, reasoning = [], []
    for c in chunks:
        v, r = s.feed(c)
        visible.append(v)
        reasoning.append(r)
    v, r = s.flush()
    visible.append(v)
    reasoning.append(r)
    return "".join(visible), "".join(reasoning)


def test_no_think_passthrough():
    assert _run(["hello ", "world"]) == ("hello world", "")


def test_single_block_one_chunk():
    assert _run(["a<think>secret</think>b"]) == ("ab", "secret")


def test_tag_split_everywhere():
    """<think>...</think> 按所有可能的两段切分都要正确。"""
    full = "前<think>推理内容</think>后"
    for i in range(1, len(full)):
        assert _run([full[:i], full[i:]]) == ("前后", "推理内容"), f"切在 {i}"


def test_tag_split_three_ways_inside_tag():
    assert _run(["x<thi", "nk>yy</th", "ink>z"]) == ("xz", "yy")


def test_multiple_blocks():
    assert _run(["a<think>r1</think>b<think>r2</think>c"]) == ("abc", "r1r2")


def test_unterminated_think_goes_to_reasoning():
    visible, reasoning = _run(["answer<think>unfinished"])
    assert visible == "answer"
    assert reasoning == "unfinished"


def test_partial_open_tag_at_end_flushed_as_visible():
    """流结束时残留的 '<th' 不是完整 tag,按可见文本吐出。"""
    visible, _ = _run(["text<th"])
    assert visible == "text<th"


def test_empty_deltas():
    assert _run(["", "a", "", "<think>", "r", "</think>", ""]) == ("a", "r")


def test_exhaustive_small_alphabet():
    """小字母表穷举:所有 ≤3 段切分结果一致。"""
    full = "p<think>Q</think>r"
    expected = ("pr", "Q")
    for n_cuts in (1, 2):
        for cuts in itertools.combinations(range(1, len(full)), n_cuts):
            chunks, prev = [], 0
            for c in cuts + (len(full),):
                chunks.append(full[prev:c])
                prev = c
            assert _run(chunks) == expected, f"cuts={cuts}"
