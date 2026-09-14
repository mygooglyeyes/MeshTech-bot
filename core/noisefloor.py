"""Noise-floor monitoring for the dashboard (noise-floor branch, v0.0.143).

The radio driver (the repeater core's SX1262Radio) already does the hard part:
it samples the SX1262's instantaneous RSSI during quiet periods, rejects
signal peaks, averages 20 samples and clamps the result to -150..-50 dBm
(`_sample_noise_floor` / `get_noise_floor` in hardware/sx1262_wrapper.py).
This module only RECORDS that value over time so the web console can draw
a rolling graph of the last 30 minutes.

Design notes:
* The driver returns 0.0 while transmitting or before init - those reads
  are SKIPPED, not stored (a 0 would wreck the y-scale).
* One sample every SAMPLE_INTERVAL seconds; the buffer is a plain list of
  (ts, floor) pruned to NOISE_WINDOW_SECONDS on each tick - 360 points
  max, trivial memory.
* Reading `get_noise_floor()` only touches a Python attribute (the driver
  averages in its own thread), but the read still goes through the
  executor like every other radio access, so a slow call can never stall
  the event loop.
* Companion mode has no local radio: bot.py never creates a monitor, the
  endpoint reports "available: false", and the dashboard hides the card.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, List, Optional, Tuple

if TYPE_CHECKING:  # pragma: no cover - import cycle guard for type checkers
    from core.mcp import Mcp

log = logging.getLogger("meshtech-bot.noisefloor")

# Graph window: Brett's ask - a running graph of the last 30 minutes.
NOISE_WINDOW_SECONDS = 30 * 60
# One point per tick: 30 min / 5 s = 360 points - smooth enough to read,
# cheap enough to ship as JSON every poll.
SAMPLE_INTERVAL_SECONDS = 5.0


class NoiseFloorMonitor:
    """Records the radio driver's averaged noise floor over time."""

    def __init__(self, mcp: "Mcp",
                 window: float = NOISE_WINDOW_SECONDS,
                 interval: float = SAMPLE_INTERVAL_SECONDS,
                 store=None):
        self._mcp = mcp
        self._window = float(window)
        self._interval = float(interval)
        self._store = store  # optional: persist samples for the analysis card
        self._samples: List[Tuple[float, float]] = []  # (ts, dBm), oldest first
        self._last: Optional[float] = None
        self._next_prune = 0.0  # prune the DB series roughly daily

    # ------------------------------------------------------------------ data

    def record(self, ts: float, floor_dbm: float) -> None:
        """Store one sample; silently drops invalid readings.

        The driver signals 'unknown / transmitting' with 0.0; anything
        outside the driver's own clamp range (-150..-50) is a glitch.
        Neither ever reaches the graph.
        """
        if floor_dbm is None:
            return
        floor_dbm = float(floor_dbm)
        if not (-150.0 < floor_dbm < -50.0):
            return
        self._samples.append((float(ts), floor_dbm))
        self._prune(ts)
        self._last = floor_dbm
        if self._store is not None:
            try:
                self._store.add_noise_sample(ts, floor_dbm)
            except Exception as exc:  # display history is never critical
                log.debug("noise sample persist failed: %s", exc)

    def _prune(self, now: float) -> None:
        cutoff = now - self._window
        idx = 0
        while idx < len(self._samples) and self._samples[idx][0] < cutoff:
            idx += 1
        if idx:
            del self._samples[:idx]

    def series(self) -> List[Tuple[float, float]]:
        """The buffered (ts, dBm) points, oldest first, 30 min max."""
        self._prune(time.time())
        return list(self._samples)

    @property
    def current(self) -> Optional[float]:
        """The most recent accepted floor, or None before the first tick."""
        return self._last

    # ------------------------------------------------------------------ loop

    async def run(self) -> None:
        """Background sampler: one radio read per interval until shutdown.

        Never raises: monitoring must not take the radio down. A failing
        read just leaves a gap in the graph (the driver itself returns
        0.0 when it cannot answer, which record() drops).
        """
        log.info("Noise-floor monitor started (every %.0fs, window %.0fmin)",
                 self._interval, self._window / 60)
        while True:
            try:
                await self._sample_once()
                self._maybe_prune()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.debug("noise-floor sample failed: %s", exc)
            await asyncio.sleep(self._interval)

    def _maybe_prune(self) -> None:
        """Trim the persisted series roughly daily (keeps 14 days)."""
        if self._store is None or time.time() < self._next_prune:
            return
        self._next_prune = time.time() + 24 * 3600
        try:
            removed = self._store.prune_noise_samples()
            if removed:
                log.info("Noise-floor history pruned: %d rows", removed)
        except Exception as exc:
            log.debug("noise-floor prune failed: %s", exc)

    async def _sample_once(self) -> None:
        radio = getattr(self._mcp, "radio", None)
        if radio is None:
            return
        getter = getattr(radio, "get_noise_floor", None)
        if not callable(getter):
            return
        try:
            loop = asyncio.get_running_loop()
            floor = await loop.run_in_executor(None, getter)
        except Exception as exc:
            # A dead/failed read just leaves a gap in the graph.
            log.debug("noise-floor read failed: %s", exc)
            return
        self.record(time.time(), floor)
