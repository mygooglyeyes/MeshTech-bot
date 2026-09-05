"""Mesh info - the database-backed commands.

    !nodes [x]               - known nodes (DM only)
    !path [x]                - path YOUR message took to the bot (public)
    !path <node> [x]         - route + history for another node (public)
    !stats <node|#channel>   - propagation time + hop statistics (DM, admin)

Compact replies by default; "x" gives the extended version - as a separate
word (!path K7ABC x) or glued on (!pathx, !nodesx).
"""
from __future__ import annotations

from typing import List, Optional

from core.format import (fmt_delay, fmt_path_hop, fmt_table, rel_time)
from core.models import HandlerResult
from .base import Handler


def bot_display_name(client, settings) -> str:
    """The bot's own name for replies: the openHop companion's node name
    when available, else the config override (bot.display_name), else "me"."""
    own = (getattr(client, "own_name", "") or "").strip(" \x00")
    if own:
        return own
    return settings.bot.display_name or "me"


def format_rxlog_chain(sender_label: str, bot_label: str, hops: Optional[int],
                       snr: Optional[float], delay_ms: Optional[int],
                       copies: List[dict]) -> List[str]:
    """Relay-chain report for !pathx, mined from RX_LOG capture copies.

    copies: parsed RX_LOG rows for the message's packet, each with path
    (hex), plen and hash_size. Relays are identified by their path hash
    (first two hex chars - names are not in the protocol) and listed in
    travel order from the sender to the bot:

        LoganBot -> 41 -> d6 -> 0b -> 70 -> 66 -> 1b -> me | travel time 340 ms to me
        11.5 dB | 7 hop(s)
    """
    # The decoded message matches the deepest (final) copy of its packet.
    deep = max(copies, key=lambda c: (c.get("plen") or 0, c.get("ts") or 0)) if copies else None
    path_hex = (deep or {}).get("path") or ""
    hsize = int((deep or {}).get("hash_size") or 1)
    step = hsize * 2
    ids = [path_hex[i:i + step][:2] for i in range(0, len(path_hex), step)]
    ids = [i for i in ids if i]

    chain = " -> ".join([f"[{sender_label}]"]
                        + [f"[{i}]" for i in ids]
                        + [f"[{bot_label}]"])
    if delay_ms is not None:
        chain += f" | travel time {delay_ms} ms to me"

    stats = " | ".join(part for part in (
        f"{snr:.1f} dB" if snr is not None else None,
        f"{hops} hop(s)" if hops is not None else None,
    ) if part)
    return [chain] + ([stats] if stats else [])


class MeshInfoHandler(Handler):
    name = "meshinfo"
    keywords = ["nodes", "path", "stats"]
    description = "Nodes, paths and propagation stats"
    scope = "both"
    access = "public"
    # Per-keyword visibility: !nodes is DM-only, !path is public everywhere,
    # !stats stays admin-only (admins are only ever recognized on DMs).
    keyword_scope = {"nodes": "dm", "path": "both", "stats": "dm"}
    keyword_access = {"path": "public", "stats": "admin"}
    priority = 80

    # ------------------------------------------------------------------

    async def handle(self, ctx) -> Optional[HandlerResult]:
        command = ctx.command
        if command == "nodes" and ctx.msg.kind != "dm":
            return None  # node list is DM-only
        if command == "stats" and not ctx.is_admin:
            return None  # silent for non-admin senders
        if command == "nodes":
            return await self._nodes(ctx)
        if command == "path":
            return await self._path(ctx)
        if command == "stats":
            return await self._stats(ctx)
        return None

    # ------------------------------------------------------------------ nodes

    async def _nodes(self, ctx) -> HandlerResult:
        store = ctx.service.store
        total = store.node_count()
        limit = 8 if ctx.verbosity == "brief" else None
        nodes = store.list_nodes(limit=limit)
        now = ctx.now

        rows = []
        for node in nodes:
            name = node.get("name") or "-"
            snr = f"{node['last_snr']:.0f}" if node.get("last_snr") is not None else "-"
            hops = fmt_path_hop(node.get("route_hops"))
            if ctx.verbosity == "brief":
                rows.append([name, node.get("prefix", ""), rel_time(node["last_seen"], now)])
            else:
                rows.append([name, node.get("prefix", ""), rel_time(node["last_seen"], now),
                             snr, hops, node.get("source", "-")])
        headers = (["Node", "Prefix", "Seen"] if ctx.verbosity == "brief"
                   else ["Node", "Prefix", "Seen", "SNR", "Route", "Source"])
        caps = [18, 12, 8, 5, 7, 8]
        lines = fmt_table(headers, rows, col_caps=caps)
        if total > len(nodes):
            lines.append(f"... and {total - len(nodes)} more (!nodes x for the full list)")
        lines.append(f"{total} node(s) in the local database")
        return HandlerResult(kind="text", data="\n".join(lines))

    # ------------------------------------------------------------------ path

    async def _path(self, ctx) -> Optional[HandlerResult]:
        store = ctx.service.store
        query = " ".join(ctx.args).strip()
        if not query:
            return await self._own_path(ctx)
        node = store.find_node(query)
        if node is None:
            return HandlerResult(kind="text",
                                 data=f"No node matching '{query}' in the database yet. "
                                      "Nodes appear once they advertise or DM the bot.")
        prefix = node["prefix"]
        now = ctx.now
        history = store.route_history(prefix, limit=8 if ctx.verbosity == "full" else 3)
        name = node.get("name") or prefix

        lines = []
        if ctx.verbosity == "brief":
            hops = fmt_path_hop(node.get("route_hops"))
            lines.append(f"{name}: {hops}, last seen {rel_time(node['last_seen'], now)}")
            if history:
                latest = history[0]
                lines.append(f"latest route: hops={latest['hops'] if latest['hops'] is not None else '?'} "
                             f"(recorded {rel_time(latest['observed_at'], now)})")
            lines.append("!path <node> x for the full route history.")
        else:
            lines.append(f"Node: {name} ({prefix})")
            lines.append(f"First seen {rel_time(node['first_seen'], now)} | "
                         f"last seen {rel_time(node['last_seen'], now)}")
            if node.get("last_snr") is not None:
                lines.append(f"Last SNR: {node['last_snr']:.0f} dB")
            lines.append("")
            if history:
                rows = [[rel_time(h["observed_at"], now),
                         str(h["hops"]) if h["hops"] is not None else "?",
                         (h["summary"] or "-")[:26]] for h in history]
                lines.extend(fmt_table(["Seen", "Hops", "Route detail"], rows,
                                       col_caps=[9, 5, 26]))
                lines.append("(route snapshots only change when the mesh reports "
                             "a different path)")
            else:
                lines.append("No route snapshots yet - they are recorded as the "
                             "mesh reports path/advert data.")
        return HandlerResult(kind="text", data="\n".join(lines))

    # ------------------------------------------------------------------ own path

    @staticmethod
    def _short_label(store, prefix: Optional[str], name: Optional[str]) -> str:
        """A relay/node label: registry name, else the first 2 chars."""
        if name:
            return name
        if prefix:
            return prefix[:2]
        return "?"

    async def _own_path(self, ctx) -> HandlerResult:
        """Path the sender's message took to reach the bot (no node argument).

        Sender identity: DMs carry the public-key prefix; channel messages
        carry only the embedded display name, resolved to a known node via
        the registry when possible.

        Extended view: mines the RX_LOG capture for the actual frame the
        message arrived in. The log lists the relay chain (path hashes, one
        per hop) and - when the same flood was heard several times - the
        SNR and arrival offset of each relay's retransmission.
        """
        store = ctx.service.store
        prefix = ctx.msg.sender_prefix
        sender_name = None
        if not prefix and ctx.msg.kind == "channel" and ctx.msg.sender_name:
            node = store.find_node(ctx.msg.sender_name)
            if node:
                prefix = node["prefix"]
                sender_name = node.get("name")
        if not prefix:
            return HandlerResult(kind="text",
                                 data="I can't identify your node yet - message me "
                                      "again once your node has advertised, or check "
                                      "the dashboard.")
        sender_name = sender_name or store.resolve_name(prefix)
        hops_now = ctx.msg.hops
        bot_label = bot_display_name(ctx.service.client, ctx.settings)
        delay_ms = None
        if ctx.msg.sender_ts and ctx.msg.recv_ts:
            delay_ms = max(0, int(round((ctx.msg.recv_ts - ctx.msg.sender_ts) * 1000)))

        if ctx.verbosity == "brief":
            # Compact one-liner: hops to [bot] | dB | travel time - no labels.
            parts = []
            if hops_now is not None:
                parts.append(f"{hops_now} hop(s) to [{bot_label}]")
            if ctx.msg.snr is not None:
                parts.append(f"{ctx.msg.snr:.1f} dB")
            if delay_ms is not None:
                parts.append(f"{delay_ms} ms")
            return HandlerResult(kind="text", data=" | ".join(parts))

        # Extended: relay chain straight from the raw radio log.
        copies = store.rxlog_copies(ctx.msg.recv_ts, hops_now)
        lines = format_rxlog_chain(
            sender_label=self._short_label(store, prefix, sender_name),
            bot_label=bot_label,
            hops=hops_now, snr=ctx.msg.snr, delay_ms=delay_ms, copies=copies)
        return HandlerResult(kind="text", data="\n".join(lines))

    # ------------------------------------------------------------------ stats

    async def _stats(self, ctx) -> Optional[HandlerResult]:
        store = ctx.service.store
        query = " ".join(ctx.args).strip()
        if not query:
            return HandlerResult(kind="text",
                                 data="Usage: !stats <node-name-or-prefix | #channel> [x]")
        is_channel = query.startswith("#")

        prefix = None
        channel = None
        label = query
        if is_channel:
            channel = query
        else:
            node = store.find_node(query)
            if node is None:
                return HandlerResult(kind="text",
                                     data=f"No node matching '{query}' in the database.")
            prefix = node["prefix"]
            label = node.get("name") or prefix

        stats = store.propagation_stats(prefix=prefix, channel=channel)
        if stats["count"] == 0:
            return HandlerResult(kind="text",
                                 data=f"No inbound messages logged for '{label}' yet.")
        lines = [f"Stats for {label} ({stats['count']} inbound messages, 14d window)"]
        if stats["delay_count"]:
            lines.append(f"propagation delay: avg {fmt_delay(stats['delay_avg'])} | "
                         f"min {fmt_delay(stats['delay_min'])} | max {fmt_delay(stats['delay_max'])}")
            lines.append("(based on sender timestamps - clocks can skew)")
        else:
            lines.append("propagation delay: no usable sender timestamps yet")
        hops = stats["hop_distribution"]
        if hops:
            parts = "  ".join(f"{h}:{n}" for h, n in sorted(hops.items(),
                                                            key=lambda kv: (kv[0] == '?', kv[0])))
            lines.append("hops: " + parts)
        if ctx.verbosity == "full":
            rows = store.query_messages(kind="dm" if not is_channel else "channel",
                                        channel=channel,
                                        limit=5)
            if rows:
                lines.append("")
                lines.append("Latest messages:")
                for row in rows:
                    when = rel_time(row["recv_ts"], ctx.now)
                    sender = store.resolve_name(row["sender_prefix"]) or row["sender_prefix"] or "-"
                    lines.append(f"  {when} {sender}: {row['text'][:40]}")
        return HandlerResult(kind="text", data="\n".join(lines))
