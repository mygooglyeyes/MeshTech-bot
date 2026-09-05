"""Live event feed.

The bot publishes small JSON events (messages in/out, status changes) and
web-dashboard WebSocket subscribers receive them in real time.  A short
ring buffer lets new subscribers catch up with recent activity.
"""
from __future__ import annotations

import asyncio
import json
import secrets
import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional, Set

_HISTORY_LIMIT = 500


class FeedHub:
    def __init__(self, history_limit: int = _HISTORY_LIMIT):
        self._history: Deque[Dict[str, Any]] = deque(maxlen=history_limit)
        self._subscribers: Set[asyncio.Queue] = set()
        # Monotonic event sequence - lets a reconnecting dashboard tell the
        # server what it has already seen, so history is not re-pasted over
        # rows already on screen.
        self._seq = 0
        # Process generation id.  seq restarts at 1 whenever the bot restarts,
        # so every event also carries this id; a dashboard that reconnects
        # after a restart can detect the new generation instead of treating
        # the fresh seq numbers as duplicates of what it already rendered.
        self._inst = secrets.token_hex(4)

    @property
    def seq(self) -> int:
        """Highest sequence number issued so far in this process."""
        return self._seq

    @property
    def inst(self) -> str:
        """Id of this feed generation (changes every bot process start)."""
        return self._inst

    def publish(self, event_type: str, payload: Dict[str, Any]) -> None:
        self._seq += 1
        event: Dict[str, Any] = {"type": event_type, "ts": time.time(),
                                 "seq": self._seq, "inst": self._inst}
        event.update({"payload": payload})
        self._history.append(event)
        if self._subscribers:
            for queue in list(self._subscribers):
                try:
                    queue.put_nowait(event)
                except asyncio.QueueFull:
                    # Drop oldest event for a slow consumer and retry once
                    try:
                        queue.get_nowait()
                        queue.put_nowait(event)
                    except (asyncio.QueueEmpty, asyncio.QueueFull):
                        pass

    def history(self, limit: int = 50) -> List[Dict[str, Any]]:
        items = list(self._history)
        return items[-limit:]

    def history_after(self, inst: Optional[str], seq: int,
                      limit: int = 50) -> List[Dict[str, Any]]:
        """Events a client that has seen up to ``seq`` still needs to see.

        A client whose generation id matches this process only needs events
        newer than ``seq``.  A client from another process (fresh page, or
        the bot restarted while it was open) cannot meaningfully resume -
        give it the usual tail instead.
        """
        if inst != self._inst:
            return self.history(limit)
        items = [e for e in self._history if e["seq"] > seq]
        return items[-limit:]

    async def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=200)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)

    def json_history(self, limit: int = 50) -> str:
        return json.dumps(self.history(limit))
