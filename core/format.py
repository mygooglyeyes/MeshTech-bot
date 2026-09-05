"""Plain-text rendering helpers.

MeshCore messages are short text messages, so all replies are built from
these helpers to stay visually tidy:
  * wrap_text / chunk_text  - respect the 133-char message limit and split
    long replies across several messages with [1/2] markers
  * fmt_table              - aligned ASCII tables for lists of nodes etc.
  * time helpers           - human friendly timestamps / durations / delays
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import List, Optional, Sequence

ELLIPSIS = "\u2026"  # …


# --------------------------------------------------------------------------
# Text width helpers
# --------------------------------------------------------------------------

def truncate(text: str, width: int) -> str:
    """Shorten text to *width* characters, adding an ellipsis if cut."""
    text = str(text)
    if len(text) <= width:
        return text
    if width <= 1:
        return ELLIPSIS
    return text[: max(1, width - 1)] + ELLIPSIS


def wrap_line(line: str, width: int) -> List[str]:
    """Hard-wrap one line to *width* chars at word boundaries."""
    line = str(line)
    if width <= 0:
        return [line]
    if len(line) <= width:
        return [line] if line != "" else [""]
    words = line.split(" ")
    out: List[str] = []
    current = ""
    for word in words:
        candidate = word if not current else current + " " + word
        if len(candidate) <= width:
            current = candidate
            continue
        if current:
            out.append(current)
            current = ""
        # A single word longer than the width must be split
        while len(word) > width:
            out.append(word[:width])
            word = word[width:]
        current = word
    if current or not out:
        out.append(current)
    return out


def chunk_text(text: str, width: int, max_chunks: int = 6) -> List[str]:
    """Split reply *text* into <= width-char messages.

    Returns a list of message strings. When more than one message is needed
    each is prefixed with a [1/2] style marker. If the reply would exceed
    *max_chunks* messages it is truncated with a clear hint.
    """
    width = max(20, int(width))
    max_chunks = max(1, int(max_chunks))

    # First normalise every logical line to <= width
    lines: List[str] = []
    for logical in str(text).split("\n"):
        lines.extend(wrap_line(logical, width))

    # Pack short lines together into messages of up to *width* chars
    def pack(capacity: int) -> List[str]:
        messages: List[str] = []
        current = ""
        for line in lines:
            if len(line) > capacity:
                for piece in wrap_line(line, capacity):
                    if current:
                        messages.append(current)
                        current = ""
                    messages.append(piece)
                continue
            if current and len(current) + 1 + len(line) > capacity:
                messages.append(current)
                current = ""
            current = line if not current else current + "\n" + line
        if current:
            messages.append(current)
        return messages

    messages = pack(width)
    if len(messages) <= 1:
        return messages

    # Add [1/n] markers: rebuild so markers never push content over width
    total = len(messages)
    if total > max_chunks:
        total = max_chunks
    marker_width = len(f"[{total}/{total}] ") + 2
    capacity = max(20, width - marker_width)
    messages = pack(capacity)
    if len(messages) > max_chunks:
        messages = messages[: max_chunks - 1]
        messages.append("(" + ELLIPSIS + " more not shown - DM me with the "
                        "'x' word to see everything)")
    marked = [f"[{i}/{len(messages)}] {msg}" for i, msg in enumerate(messages, start=1)]
    # Guard: any single message that somehow still exceeds width gets hard cut
    return [truncate(m, width) for m in marked]


def fmt_table(headers: Sequence[str], rows: Sequence[Sequence[object]],
              col_caps: Optional[Sequence[int]] = None) -> List[str]:
    """Render an aligned ASCII table as a list of lines.

    col_caps limits each column width (long values are ellipsized).
    """
    headers = [str(h) for h in headers]
    data = [[("" if v is None else str(v)) for v in row] for row in rows]
    caps = list(col_caps) if col_caps else None
    if caps is None:
        caps = [24] * len(headers)
    caps = caps + [24] * (len(headers) - len(caps))

    widths: List[int] = []
    for col, header in enumerate(headers):
        content = [header] + [row[col] for row in data if col < len(row)]
        natural = max((len(c) for c in content), default=0)
        widths.append(min(natural, max(4, caps[col])))

    def render_row(cells: Sequence[str]) -> str:
        parts = []
        for col, cell in enumerate(cells):
            parts.append(truncate(cell, widths[col]).ljust(widths[col]))
        return "  ".join(parts).rstrip()

    lines = [render_row(headers)]
    lines.append("  ".join(["-" * w for w in widths]))
    for row in data:
        padded = row + [""] * (len(headers) - len(row))
        lines.append(render_row(padded))
    return lines


# --------------------------------------------------------------------------
# Time helpers
# --------------------------------------------------------------------------

def _tz(local_or_utc: str, iana_name: Optional[str] = None):
    if local_or_utc == "utc":
        return timezone.utc
    if local_or_utc == "iana" and iana_name:
        try:
            from zoneinfo import ZoneInfo
            return ZoneInfo(iana_name)
        except Exception:
            return None
    return None  # None => system local time


def fmt_ts(ts: float, local_or_utc: str = "local", iana_name: Optional[str] = None,
           with_date: bool = False) -> str:
    """Format a unix timestamp for humans."""
    try:
        tzinfo = _tz(local_or_utc, iana_name)
        if tzinfo is not None:
            dt = datetime.fromtimestamp(ts, tz=tzinfo)
        else:
            dt = datetime.fromtimestamp(ts).astimezone()
    except (OverflowError, OSError, ValueError):
        return "?"
    if with_date:
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    if dt.date() == datetime.now().date():
        return dt.strftime("%H:%M:%S")
    return dt.strftime("%m-%d %H:%M")


def rel_time(ts: float, now: Optional[float] = None) -> str:
    """Human 'how long ago' text, e.g. 'now', '42s', '3m', '2h', '4d'."""
    now = now if now is not None else _unix()
    delta = max(0.0, now - ts)
    if delta < 5:
        return "now"
    if delta < 60:
        return f"{int(delta)}s ago"
    if delta < 3600:
        return f"{int(delta // 60)}m ago"
    if delta < 86400:
        return f"{int(delta // 3600)}h ago"
    return f"{int(delta // 86400)}d ago"


def fmt_delay(seconds: float) -> str:
    """Format a duration (e.g. propagation delay) compactly."""
    if seconds is None:
        return "?"
    if seconds < 0:
        return "?"
    if seconds < 1:
        return f"{int(round(seconds * 1000))} ms"
    if seconds < 90:
        return f"{seconds:.1f} s"
    if seconds < 3600:
        return f"{int(seconds // 60)}m {int(seconds % 60)}s"
    return f"{int(seconds // 3600)}h {int((seconds % 3600) // 60)}m"


def _unix() -> float:
    import time
    return time.time()


# --------------------------------------------------------------------------
# Misc display helpers
# --------------------------------------------------------------------------

def friendly(prefix: Optional[str], name: Optional[str]) -> str:
    """Combine a stored name and key prefix into a display string."""
    if not prefix:
        return name or "?"
    if name:
        return f"{name} ({prefix})"
    return prefix


def fmt_path_hop(hops: Optional[int]) -> str:
    if hops is None:
        return "?"
    if hops == 0:
        return "direct"
    return f"{hops} hop" + ("" if hops == 1 else "s")


def avg(values: Sequence[Optional[float]]) -> Optional[float]:
    nums = [v for v in values if v is not None and not (isinstance(v, float) and math.isnan(v))]
    if not nums:
        return None
    return sum(nums) / len(nums)
