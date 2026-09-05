"""Live event feed.

The bot publishes small JSON events (messages in/out, status changes) and
web-dashboard WebSocket subscribers receive them in real time.  A short
ring buffer lets new subscribers catch up with recent activity.
"""
from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from typing import Any, Deque, Dict, List, Set

_HISTORY_LIMIT = 500


class FeedHub:
    def __init__(self, history_limit: int = _HISTORY_LIMIT):
        self._history: Deque[Dict[str, Any]] = deque(maxlen=history_limit)
        self._subscribers: Set[asyncio.Queue] = set()

    def publish(self, event_type: str, payload: Dict[str, Any]) -> None:
        event: Dict[str, Any] = {"type": event_type, "ts": time.time()}
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

    async def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=200)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)

    def json_history(self, limit: int = 50) -> str:
        return json.dumps(self.history(limit))
