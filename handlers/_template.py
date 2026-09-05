"""HOW TO ADD A NEW BOT FEATURE - copy this file.

1. Copy this file to a new name in the same folder, e.g.  weather.py
   (the leading underscore is what keeps templates out of the bot).
2. Rename the class and edit the fields below.
3. Fill in the handle() method.
4. Restart the bot (or ask it: !reload). Done - no other wiring needed.

Rules to keep replies mesh-friendly:
  * Every reply defaults to the COMPACT view (radio airtime is precious).
    Append 'x' to the command for the extended version - ctx.verbosity
    tells you which one was requested.
  * Keep lines short; the router splits long replies into [1/2] chunks.
  * Set scope="dm" when the reply is personal/verbose by nature.
"""
from __future__ import annotations

from typing import Optional

from core.models import HandlerResult
from .base import Handler


class ExampleTemplateHandler(Handler):  # rename me!
    name = "example"                    # unique handler id
    description = "Short description shown by !help"
    keywords = ["example"]              # what users type (add ! prefix when require_prefix)
    scope = "both"                      # "both" | "channel" | "dm"
    access = "public"                   # "public" | "admin"
    require_prefix = True
    priority = 60                       # lower = answered first on keyword clashes

    async def handle(self, ctx) -> Optional[HandlerResult]:
        # ctx.command     - the keyword the user typed
        # ctx.args        - extra words after the command (modifiers removed)
        # ctx.verbosity   - "brief" or "full"
        # ctx.sender_display() - friendly sender name (DM)
        # ctx.store / ctx.service - database + bot state
        #
        # Query the database, call an API, whatever the feature needs...
        answer = f"You asked '{ctx.command}' with {len(ctx.args)} extra word(s)."

        # Return None to stay silent for this message.
        return HandlerResult(kind="text", data=answer)
