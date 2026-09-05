"""Tests for handlers/two_byte.py - the single-line shaded bar reply.

Bar glyphs are CP437 shading: 0xB2/178 (\u2593 ▓) for the percentage
share, 0xB1/177 (\u2592 ▒) for the remainder up to 100%.
"""

from handlers.two_byte import _FILL, _EMPTY, _bar, format_2byte_report


def test_bar_width_and_fill():
    bar = _bar(50.0, width=20)
    assert len(bar) == 20 and set(bar) <= {_FILL, _EMPTY}
    assert bar.count(_FILL) == 10 and bar.count(_EMPTY) == 10
    assert _bar(0.0).count(_FILL) == 0
    assert _bar(100.0).count(_EMPTY) == 0


def test_single_line_with_bar_pct_and_count():
    stats = {"frames_total": 100, "frames": {1: 70, 2: 30},
             "node_total": 4, "nodes": {1: 1, 2: 2, 3: 1}}
    text = format_2byte_report(stats)
    assert "\n" not in text  # exactly one line
    # 50% of 20 columns -> 10 x 178/▓ then 10 x 177/▒
    assert text == "2-byte path nodes: [" + _FILL * 10 + _EMPTY * 10 + \
        "] 50% (2/4 of registered nodes)"


def test_falls_back_to_frames_when_few_named():
    stats = {"frames_total": 100, "frames": {1: 80, 2: 20},
             "node_total": 1, "nodes": {1: 1}}
    # 20% of 20 columns -> 4 filled
    assert format_2byte_report(stats) == \
        "2-byte path nodes: [" + _FILL * 4 + _EMPTY * 16 + \
        "] 20% (20/100 by frames)"


def test_zero_2byte_nodes():
    stats = {"frames_total": 100, "frames": {1: 100},
             "node_total": 5, "nodes": {1: 5}}
    text = format_2byte_report(stats)
    assert text.endswith("0% (0/5 of registered nodes)")
    assert _FILL not in text and _EMPTY in text  # fully empty bar


def test_empty_data():
    assert "No path-hash data yet" in format_2byte_report(
        {"frames_total": 0, "frames": {}, "node_total": 0, "nodes": {}})
