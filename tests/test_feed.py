"""Tests for core/feed.py catch-up semantics.

The live feed stamps every event with a process generation id (``inst``)
and a monotonic sequence (``seq``) so a reconnecting dashboard can resume
at its last-rendered event instead of re-pasting history it already has.
Pure stdlib - no websockets/network needed.
"""
from __future__ import annotations

from core.feed import FeedHub


def _pub(feed: FeedHub, n: int, etype: str = "notice") -> None:
    for i in range(n):
        feed.publish(etype, {"text": f"event {i}"})


# ------------------------------------------------------------------ stamps

def test_events_carry_increasing_seq_and_inst():
    feed = FeedHub()
    _pub(feed, 3)
    events = feed.history(limit=10)
    assert [e["seq"] for e in events] == [1, 2, 3]
    assert len({e["inst"] for e in events}) == 1        # one generation
    assert events[0]["inst"] == feed.inst


def test_generation_id_differs_between_processes():
    a, b = FeedHub(), FeedHub()
    assert a.inst != b.inst          # a restart must look like a new feed


# ------------------------------------------------------------------ catch-up

def test_history_after_same_generation_only_newer():
    feed = FeedHub()
    _pub(feed, 5)
    got = feed.history_after(feed.inst, 3, limit=10)
    assert [e["seq"] for e in got] == [4, 5]


def test_history_after_nothing_newer_returns_empty():
    feed = FeedHub()
    _pub(feed, 5)
    assert feed.history_after(feed.inst, 5, limit=10) == []
    # client ahead of us (seq reset after restart, same inst impossible, but
    # be safe): nothing newer exists either
    assert feed.history_after(feed.inst, 99, limit=10) == []


def test_history_after_foreign_generation_gets_full_tail():
    feed = FeedHub()
    _pub(feed, 5)
    # A page from before the bot restarted reports the OLD generation id -
    # its sequence baseline is meaningless here, so give it the normal tail.
    got = feed.history_after("stale-gen", 1_000, limit=10)
    assert [e["seq"] for e in got] == [1, 2, 3, 4, 5]


def test_history_after_respects_limit():
    feed = FeedHub(history_limit=500)
    _pub(feed, 40)
    got = feed.history_after(feed.inst, 0, limit=30)
    assert len(got) == 30
    assert got[0]["seq"] == 11       # 40 events, last 30 -> starts at 11


def test_history_is_bounded_ring():
    feed = FeedHub(history_limit=10)
    _pub(feed, 25)
    assert len(feed.history(limit=50)) == 10
    assert feed.history(limit=50)[-1]["seq"] == 25
