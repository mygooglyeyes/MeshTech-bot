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
        visible = []
        for handler in ctx.service.registry:
            if handler.access == "admin" and not ctx.is_admin:
                continue
            if handler.scope not in ("both", ctx.msg.kind):
                continue
            if handler.name == "canned":
                continue  # listed separately as plain words
            visible.append((handler, handler.keywords))

        canned_keywords = [kw for rule in settings.replies for kw in rule.keywords]
        canned_words = " ".join(sorted(set(canned_keywords))) or "(none)"

        if ctx.verbosity == "brief":
            command_words = " ".join(sorted({kw for _, kws in visible for kw in kws}))
            lines = [
                "Commands: " + command_words,
                "Plain words I also answer: " + canned_words,
                "Add 'full' for more detail (e.g. !nodes full).",
                "Admin can use: reload, shutdown, diag (DM only)." if ctx.is_admin
                else "DM me for admin help once your node is allowlisted.",
            ]
            return HandlerResult(kind="text", data="\n".join(lines))

        rows = []
        for handler, keywords in sorted(visible, key=lambda item: item[0].priority):
            rows.append(["!" + keywords[0], handler.description,
                         ("DM only" if handler.scope == "dm" else
                          "channel/DM" if handler.scope == "both" else "channel")])
        table = fmt_table(["Command", "What it does", "Where"], rows,
                          col_caps=[16, 46, 10])
        lines = list(table)
        lines.append("")
        lines.append("You can add a detail word to most commands: "
                     f"{'/'.join(settings.verbosity.all_brief())} = short, "
                     f"{'/'.join(settings.verbosity.all_full())} = extended.")
        lines.append("Examples: !nodes   |   !nodes full   |   !path <node> full")
        lines.append("Plain-word answers: " + canned_words)
        if ctx.is_admin:
            lines.append("Admin (DM): !diag, !reload, !shutdown")
        return HandlerResult(kind="text", data="\n".join(lines))
