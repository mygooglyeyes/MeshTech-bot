"""Tests for core/format.py - text layout helpers."""
from __future__ import annotations

from core.format import (chunk_text, fmt_delay, fmt_table, fmt_ts,
                         rel_time, truncate, wrap_line)


def test_truncate_adds_ellipsis():
    assert truncate("abcdef", 5) == "abcd\u2026"
    assert truncate("abc", 10) == "abc"


def test_wrap_line_respects_width():
    line = "one two three four five"
    wrapped = wrap_line(line, 8)
    assert all(len(part) <= 8 for part in wrapped)
    assert "".join(wrapped).replace(" ", "") == line.replace(" ", "")


def test_wrap_long_word_split():
    wrapped = wrap_line("abcdefghijklmnopqrstuvwxyz", 10)
    assert all(len(part) <= 10 for part in wrapped)
    assert "".join(wrapped) == "abcdefghijklmnopqrstuvwxyz"


def test_chunk_single_short_message():
    assert chunk_text("hello world", 133) == ["hello world"]


def test_chunk_long_message_uses_markers_and_width():
    text = "\n".join(f"line {i} " + "x" * 60 for i in range(20))
    messages = chunk_text(text, 80, max_chunks=10)
    assert len(messages) > 1
    for message in messages:
        assert len(message) <= 80, message
    assert messages[0].startswith("[1/")


def test_chunk_truncates_at_max_chunks():
    text = "\n".join("word " * 50 for _ in range(30))
    messages = chunk_text(text, 133, max_chunks=3)
    assert len(messages) <= 3
    assert any("more not shown" in m for m in messages)


def test_fmt_table_aligned_and_bounded():
    lines = fmt_table(["Node", "Prefix"], [["Alice", "aabbccddeeff"],
                                           ["Bob", "112233445566"]],
                      col_caps=[8, 12])
    assert len(lines) == 4  # header + separator + two data rows
    assert lines[0].startswith("Node")
    assert set(lines[1].replace(" ", "")) == {"-"}
    assert "Alice" in lines[2] and "112233445566" in lines[3]


def test_time_helpers():
    import time
    now = time.time()
    assert rel_time(now - 10, now) == "10s ago"
    assert rel_time(now - 5000, now) == "1h ago"
    assert fmt_delay(0.25) == "250 ms"
    assert fmt_delay(3.0) == "3.0 s"
    assert fmt_delay(90) == "1m 30s"
    assert ":" in fmt_ts(now, "utc")
