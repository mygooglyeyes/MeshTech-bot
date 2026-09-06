"""Admin control (DM only, allowlisted nodes).

    !diag [x]        - database + traffic summary
    !reload          - re-read config.yaml and refresh handlers
    !shutdown        - stop the bot gracefully
    !up              - raise the airtime budget (boost), e.g. before an
                       event: +30/hour +150/day per use, at most 90/hour
                       and 2200/day. Boosts age out after 24 hours.

Access is enforced by the router (access="admin") plus the allowlist in
config.yaml (dm.admin_pubkey_prefixes).
"""
from __future__ import annotations

import asyncio
from typing import List, Optional

from core.format import fmt_table, rel_time
from core.models import HandlerResult
from .base import Handler


class AdminHandler(Handler):
    name = "admin"
    keywords = ["diag", "reload", "shutdown", "up"]
    description = "Bot administration (diag/reload/shutdown/up)"
    scope = "dm"
    access = "admin"
    priority = 50

    async def handle(self, ctx) -> Optional[HandlerResult]:
        command = ctx.command
        if command == "diag":
            return await self._diag(ctx)
        if command == "reload":
            return HandlerResult(kind="text", data=ctx.service.reload())
        if command == "up":
            result = ctx.service.boost_budget()
            if not result.get("ok"):
                return HandlerResult(kind="text", data=result["message"])
            lines = [f"Budget up: {result['hour_cap']:.0f}/h, "
                     f"{result['day_cap']:.0f}/d "
                     f"({result['boosts']} boost(es) in 24h)"]
            if result["hour_maxed"] and result["day_maxed"]:
                lines.append("Both caps maxed - no room left.")
            elif result["hour_maxed"]:
                lines.append("Hourly cap maxed (90).")
            elif result["day_maxed"]:
                lines.append("Daily cap maxed (2200).")
            return HandlerResult(kind="text", data="\n".join(lines))
        if command == "shutdown":
            asyncio.get_event_loop().call_later(1.5, ctx.service.request_shutdown,
                                                "admin DM command")
            return HandlerResult(kind="text", data="Shutting down the bot now. 73!")
        return None

    # ------------------------------------------------------------------

    async def _diag(self, ctx) -> HandlerResult:
        store = ctx.service.store
        stats = store.stats_row()
        totals = stats["totals"]
        now = ctx.now

        if ctx.verbosity == "brief":
            lines = [
                f"uptime {rel_time(ctx.service.started_at, now)} | "
                f"nodes {stats['nodes']} | messages {totals.get('total', 0)}",
                f"in: dm {totals.get('in_dm', 0)} / ch {totals.get('in_channel', 0)}  "
                f"out: dm {totals.get('out_dm', 0)} / ch {totals.get('out_channel', 0)}",
                f"packets captured: {stats.get('packets', 0)}",
            ]
            hops = stats["hop_distribution"]
            if hops:
                lines.append("inbound hops: " + "  ".join(
                    f"{h['hops']}={h['count']}" for h in hops))
            return HandlerResult(kind="text", data="\n".join(lines))

        rows = [[c["channel_name"], c["n"]] for c in stats["channels_24h"]]
        lines = [
            f"nodes: {stats['nodes']} | total messages logged: {totals.get('total', 0)}",
            "messages: " + ", ".join(f"{k}={v}" for k, v in totals.items() if k != "total"),
            "",
        ]
        if rows:
            lines.extend(fmt_table(["Channel", "Msgs (24h)"], rows, col_caps=[14, 10]))
        hops = stats["hop_distribution"]
        if hops:
            lines.append("")
            lines.append("Inbound hop distribution: " + "  ".join(
                f"{h['hops']} hop(s) x {h['count']}" for h in hops))
        if ctx.service.settings.warnings:
            lines.append("")
            lines.append("Config warnings:")
            lines.extend(f"  - {w}" for w in ctx.service.settings.warnings[:5])
        lines.append("")
        lines.append("Hop counts come from the radio; store data as truth for the dashboard.")
        return HandlerResult(kind="text", data="\n".join(lines))
