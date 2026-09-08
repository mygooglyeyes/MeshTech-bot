"""Mesh health: flood scoring and offender ranking.

Computed on demand from the message log the bot already keeps - no new
tables, no background work, safe on a Raspberry Pi.  A sender's score
combines three plain-language signals, each visible (never hidden):

  burst   - messages per minute over the last 10 minutes
  share   - percent of ALL traffic in the last hour this sender took
  repeat  - identical text sent repeatedly in 10 minutes (stuck-node /
            beacon signature)

The composite is 0-100 and deliberately coarse: it exists to point a
human at the right sender, not to convict one.  Nothing here ever blocks
a node automatically - the Mesh Health card surfaces offenders and the
operator decides (surface-only doctrine).
"""
from __future__ import annotations

import time
from typing import Any, Dict, List

from .store import Store

# Tuning knobs (plain numbers, easy to reason about):
BURST_WINDOW_MIN = 10.0     # burst window
BURST_MSGS_PER_MIN_WARN = 6.0     # >6 msg/min sustained = chatty
BURST_MSGS_PER_MIN_MAX = 15.0     # 15+ msg/min = definitely flooding
SHARE_WINDOW_MIN = 60.0
SHARE_PCT_WARN = 30.0       # >30% of an hour's traffic from one sender
SHARE_PCT_MAX = 60.0        # >60% = dominating the channel
REPEAT_WINDOW_MIN = 10.0
REPEAT_COUNT_WARN = 3       # 3x identical text in 10 min
REPEAT_COUNT_MAX = 8        # 8+ = stuck beacon


def _clamp0_100(value: float) -> int:
    return int(max(0, min(100, round(value))))


def score_sender(senders: Dict[str, Dict[str, Any]], who: str,
                 total_share: int) -> Dict[str, Any]:
    """Score one sender from its windowed signals.

    Components (each 0-100, then averaged, repeat weighted highest):
      burst  - linear from BURST_WARN to BURST_MAX msg/min
      share  - linear from SHARE_WARN to SHARE_MAX percent
      repeat - linear from REPEAT_WARN to REPEAT_MAX identical sends
    """
    s = senders.get(who, {})
    burst_n = float(s.get("burst", 0))
    share_n = float(s.get("share", 0))
    repeat_n = float(s.get("repeat", 0))

    burst_rate = burst_n / BURST_WINDOW_MIN            # msgs per minute
    burst_score = 100.0 * (burst_rate - BURST_MSGS_PER_MIN_WARN) / (
        BURST_MSGS_PER_MIN_MAX - BURST_MSGS_PER_MIN_WARN)
    share_pct = (share_n / total_share * 100.0) if total_share else 0.0
    share_score = 100.0 * (share_pct - SHARE_PCT_WARN) / (
        SHARE_PCT_MAX - SHARE_PCT_WARN)
    repeat_score = 100.0 * (repeat_n - REPEAT_COUNT_WARN) / (
        REPEAT_COUNT_MAX - REPEAT_COUNT_WARN)

    burst_score = max(0.0, min(100.0, burst_score))
    share_score = max(0.0, min(100.0, share_score))
    repeat_score = max(0.0, min(100.0, repeat_score))
    composite = (0.30 * burst_score + 0.30 * share_score + 0.40 * repeat_score)
    return {
        "score": _clamp0_100(composite),
        "burst": {"msgs": int(burst_n),
                  "per_min": round(burst_rate, 1),
                  "pct": _clamp0_100(burst_score)},
        "share": {"msgs": int(share_n),
                  "pct": round(share_pct, 1),
                  "score": _clamp0_100(share_score)},
        "repeat": {"count": int(repeat_n),
                   "score": _clamp0_100(repeat_score)},
    }


def mesh_health(store: Store, limit: int = 10) -> Dict[str, Any]:
    """Top senders by flood score + mesh totals for the card.

    Names are resolved through the registry when possible; name-only
    identities render as their embedded name.
    """
    windows = store.sender_windows(
        burst_minutes=BURST_WINDOW_MIN,
        share_minutes=SHARE_WINDOW_MIN,
        repeat_minutes=REPEAT_WINDOW_MIN)
    senders = windows["senders"]
    total_share = windows["total_share"]

    rows: List[Dict[str, Any]] = []
    for who, _sig in senders.items():
        scored = score_sender(senders, who, total_share)
        if scored["score"] <= 0 and scored["share"]["msgs"] <= 0:
            continue
        name = who
        prefix = ""
        if who.startswith("name:"):
            name = who[5:]
        else:
            prefix = who
            name = store.resolve_name(prefix) or prefix[:6]
        rows.append({"who": who, "prefix": prefix, "name": name, **scored})
    rows.sort(key=lambda r: (-r["score"], -r["share"]["msgs"]))

    now = time.time()
    total_hour = sum(float(v.get("share", 0)) for v in senders.values())
    return {
        "generated_at": int(now),
        "total_msgs_hour": int(total_hour),
        "senders": rows[: max(1, min(int(limit), 25))],
    }
