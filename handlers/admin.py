"""Admin control (DM only, allowlisted nodes).

    !diag [x]        - database + traffic summary
    !reload          - re-read config.yaml and refresh handlers
    !shutdown        - stop the bot gracefully
    !up              - raise the airtime budget (boost), e.g. before an
                       event: +30/hour +150/day per use, at most 90/hour
                       and 2200/day. Boosts age out after 24 hours.
    !down            - cancel ALL active boosts; the budget returns to
                       its base caps immediately.
    !trust on|smart|off
                     - set how the bot treats the sender name embedded
                       in channel messages (mesh.channel_sender_name):
                       on = always strip it, smart = strip only when the
                       rest wouldn't match a command, off = never strip.
                       Writes config.yaml (validated) and reloads.
                       Replies: "Trust set to <mode>". Bare !trust
                       replies "Trust is <current>".

Access is enforced by the router (access="admin") plus the allowlist in
config.yaml (dm.admin_pubkey_prefixes).
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import List, Optional

from core.format import fmt_table, rel_time
from core.models import HandlerResult
from .base import Handler

log = logging.getLogger("meshtech-bot.handlers.admin")


class AdminHandler(Handler):
    name = "admin"
    keywords = ["diag", "reload", "shutdown", "up", "down", "trust"]
    description = "Bot administration (diag/reload/shutdown/up/down/trust)"
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
            expires = time.strftime("%H:%M", time.localtime(
                result["next_expiry"]))
            lines = [f"Budget up: {result['hour_cap']:.0f}/h, "
                     f"{result['day_cap']:.0f}/d "
                     f"({result['boosts']} boost(es) in 24h, oldest ends "
                     f"{expires})"]
            if result["hour_maxed"] and result["day_maxed"]:
                lines.append("Both caps maxed - no room left.")
            elif result["hour_maxed"]:
                lines.append("Hourly cap maxed (90).")
            elif result["day_maxed"]:
                lines.append("Daily cap maxed (2200).")
            return HandlerResult(kind="text", data="\n".join(lines))
        if command == "down":
            result = ctx.service.deflate_budget()
            if result["cancelled"]:
                return HandlerResult(kind="text", data=
                    f"Budget down: {result['cancelled']} boost(s) cancelled "
                    f"- now {result['hour_cap']:.0f}/h, "
                    f"{result['day_cap']:.0f}/d")
            return HandlerResult(kind="text", data=
                f"No boosts active - budget already at base "
                f"({result['hour_cap']:.0f}/h, {result['day_cap']:.0f}/d)")
        if command == "shutdown":
            asyncio.get_event_loop().call_later(1.5, ctx.service.request_shutdown,
                                                "admin DM command")
            return HandlerResult(kind="text", data="Shutting down the bot now. 73!")
        if command == "trust":
            return self._trust(ctx)
        return None

    def _trust(self, ctx) -> HandlerResult:
        """!trust on|smart|off - set mesh.channel_sender_name.

        Value words: on = trust, smart = smart, off = off (the words the
        mesh uses; 'trust' is also accepted as a synonym for 'on').
        Writes config.yaml through the validated splicer, then reloads so
        every dependent state refreshes. The reload's on-air line stays
        the standard one; this reply is the one that names the new value.
        Replies use the user-facing word 'on' for the trust mode (Brett):
        "Trust set to on" / "Trust is on" - never the internal value.
        """
        raw = (ctx.args[0] if ctx.args else "").strip().lower()
        aliases = {"on": "trust", "trust": "trust", "smart": "smart",
                   "off": "off"}
        labels = {"trust": "on", "smart": "smart", "off": "off"}
        if raw not in aliases:
            current = labels.get(ctx.settings.mesh.channel_sender_name,
                                 ctx.settings.mesh.channel_sender_name)
            return HandlerResult(kind="text", data=f"Trust is {current}")
        mode = aliases[raw]
        from core.persist import set_mesh_sender_name
        try:
            set_mesh_sender_name(ctx.settings.config_path, mode)
        except Exception as exc:
            log.warning("!trust write failed: %s", exc)
            return HandlerResult(kind="text", data=(
                "Could not save the setting - nothing was changed."))
        ctx.service.reload()               # refresh all dependent state
        return HandlerResult(kind="text", data=f"Trust set to {labels[mode]}")

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
