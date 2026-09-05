"""DM - have the bot DM the sender back, starting a direct thread.

On a channel, ``!DM`` makes the bot reply by DIRECT MESSAGE instead of on
the channel. That proves the DM path to the bot works and opens a thread
between the two - useful for testing DM connectivity and for reaching the
DM-only commands (!status, !nodes...) without hopping onto a channel.

Sending ``!DM`` inside a DM already is harmless: the bot just says so.
"""
from __future__ import annotations

from typing import Optional

from core.models import HandlerResult
from .base import Handler


class DmHandler(Handler):
    name = "dm"
    keywords = ["dm"]
    description = "Have the bot DM you back (starts a DM thread)"
    scope = "both"
    access = "public"
    priority = 97

    async def handle(self, ctx) -> Optional[HandlerResult]:
        if ctx.msg.kind == "dm":
            return HandlerResult(kind="text",
                                 data="Already in a DM - just reply here.")
        client = ctx.service.client
        bot_name = (getattr(client, "own_name", "") if client is not None
                    else "") or ctx.service.settings.bot.display_name
        # Keep the DM text plain ASCII: the companion name can carry emoji
        # (e.g. "LoganBot🤖"), and some mesh clients fail to render non-ASCII
        # inside a DM. The mesh itself is fine with it - the console is not.
        bot_name = "".join(ch for ch in (bot_name or "") if ord(ch) < 128)
        return HandlerResult(
            kind="dm_text",
            data=f"DM from {bot_name or 'me'} - reply here to talk directly.",
        )