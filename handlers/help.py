"""Help - lists the bot's commands and explains the modifier words."""
from __future__ import annotations

from typing import List, Optional

from core.format import fmt_table
from core.models import HandlerResult
from .base import Handler


class HelpHandler(Handler):
    name = "help"
    keywords = ["help"]
    description = "List commands and usage"
    scope = "both"
    access = "public"
    priority = 90

    async def handle(self, ctx) -> Optional[HandlerResult]:
        settings = ctx.service.settings
        kind = ctx.msg.kind

        # Per-keyword visibility: handlers may declare keyword_scope /
        # keyword_access overrides (e.g. !nodes DM-only, !path public).
        pairs = []  # (handler, keyword, scope, access)
        for handler in ctx.service.registry:
            if handler.name == "canned":
                continue  # listed separately as plain words
            scope_map = getattr(handler, "keyword_scope", {})
            access_map = getattr(handler, "keyword_access", {})
            for kw in handler.keywords:
                scope = scope_map.get(kw, handler.scope)
                if scope not in ("both", kind):
                    continue
                access = access_map.get(kw, handler.access)
                if access == "admin" and not ctx.is_admin:
                    continue
                pairs.append((handler, kw, scope, access))

        canned_keywords = [kw for rule in settings.replies for kw in rule.keywords]
        canned_words = " ".join(sorted(set(canned_keywords))) or "(none)"

        if ctx.verbosity == "brief":
            command_words = " ".join(sorted({kw for _, kw, _, _ in pairs}))
            lines = [
                "Commands: " + command_words,
                "Plain words I also answer: " + canned_words,
                "Add 'x' for the extended version (e.g. !nodes x, !pathx).",
                "Admin can use: reload, shutdown, diag (DM only)." if ctx.is_admin
                else "DM me for admin help once your node is allowlisted.",
            ]
            return HandlerResult(kind="text", data="\n".join(lines))

        rows = []
        for handler, kw, scope, access in sorted(pairs, key=lambda p: p[0].priority):
            where = ("DM only" if scope == "dm" else
                     "channel/DM" if scope == "both" else "channel")
            if access == "admin":
                where += " (admin)"
            rows.append(["!" + kw, handler.description, where])
        table = fmt_table(["Command", "What it does", "Where"], rows,
                          col_caps=[16, 46, 10])
        lines = list(table)
        lines.append("")
        lines.append("Append 'x' to most commands for the extended version "
                     "(e.g. !nodes x, !pathx, !path <node> x) - glued on is fine "
                     "(!pathx == !path x).")
        lines.append("Examples: !nodes   |   !nodes x   |   !pathx   |   !path K7ABC x")
        lines.append("Plain-word answers: " + canned_words)
        return HandlerResult(kind="text", data="\n".join(lines))
