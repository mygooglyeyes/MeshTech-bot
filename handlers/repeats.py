"""Repeats - how many duplicate packets the mesh delivered lately (v0.0.147)."""
from __future__ import annotations

from typing import Optional

from core.models import HandlerResult
from .base import Handler


class RepeatsHandler(Handler):
    name = "repeats"
    keywords = ["repeats"]
    description = "Repeated packets in the last hour"
    scope = "both"
    access = "public"
    priority = 96

    async def handle(self, ctx) -> Optional[HandlerResult]:
        stats = ctx.service.store.repeat_stats(3600.0)
        if stats is None:
            # Schema predates repeat marking (pre-merge build).
            return HandlerResult(kind="text",
                                 data="No repeat tracking on this build")
        reps, total = stats
        pct = (100.0 * reps / total) if total else 0.0
        return HandlerResult(
            kind="text",
            data=f"{reps} repeats / {pct:.0f}% of total packets {total}")
