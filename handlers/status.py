"""Status - quick health overview of the bot."""
from __future__ import annotations

from typing import List, Optional

from core.format import fmt_table, fmt_ts, rel_time
from core.models import HandlerResult
from .base import Handler


class StatusHandler(Handler):
    name = "status"
    keywords = ["status"]
    description = "Bot status and channel health"
    scope = "dm"
    access = "public"
    priority = 95

    async def handle(self, ctx) -> Optional[HandlerResult]:
        service = ctx.service
        log_cfg = service.settings.logging
        uptime = rel_time(service.started_at, now=ctx.now)
        counts = service.store.stats_row()
        totals = counts["totals"]

        channel_text = ", ".join(
            f"{state['name']}" + ("" if state["reply"] else " (listen)")
            for state in service.effective_channel_states()
        )
        link = "\u2713" if service.client is not None and service.client.is_connected else "\u2717"
        lines = [f"\u25cf up {uptime}  |  link {link}",
                 f"channels: {channel_text or '(none)'}",
                 f"nodes: {counts['nodes']}  msgs: {totals.get('total', 0)}"]

        if ctx.verbosity == "full":
            conn = service.settings.connection
            rows = [
                ["repeater", f"{conn.host}:{conn.port}"],
                ["hop limit", str(service.settings.mesh.max_inbound_hops)
                    if service.settings.mesh.max_inbound_hops else "unlimited"],
                ["muted", "yes (dashboard switch)" if service.store.global_mute() else "no"],
                ["unknown sender",
                 "silent" if not service.settings.bot.answer_unknown_senders else "answered"],
            ]
            lines.append("")
            for header, value in rows:
                lines.append(f"{header:<11} {value}")
            lines.append(f"started     {fmt_ts(service.started_at, log_cfg.timezone, log_cfg.tz_iana)}")
            lines.append("")
            per_channel = fmt_table(["Channel", "Msgs (24h)"],
                                    [[c["channel_name"], c["n"]]
                                     for c in counts["channels_24h"]],
                                    col_caps=[14, 10])
            lines.extend(per_channel)
            hops = " ".join(f"{h['hops']}->{h['count']}" for h in counts["hop_distribution"])
            lines.append("Inbound hops: " + (hops or "(no data yet)"))
            lines.append("Hop counts come from the radio protocol; '?' rows are logged but not answered.")
        return HandlerResult(kind="text", data="\n".join(lines))

    def render_lines(self, result, verbosity: str) -> List[str]:
        return str(result.data).splitlines() or [""]
