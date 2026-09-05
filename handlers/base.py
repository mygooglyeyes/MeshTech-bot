"""Handler base class.

A handler is one bot capability:

    class MyHandler(Handler):
        name = "weather"
        keywords = ["weather"]          # what users type to trigger it
        description = "Local weather"
        scope = "both"                  # "both" | "channel" | "dm"
        access = "public"               # "public" | "admin" (admin = allowlist)
        require_prefix = True           # require "!weather" (not bare word)

        async def handle(self, ctx):
            ... look things up ...
            return HandlerResult(kind="text", data="It is sunny.")

Handlers are discovered automatically from the handlers/ folder - a new
feature is simply a new file with a new class.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from core.models import HandlerResult

if TYPE_CHECKING:
    from core.router import RouterCtx
    from core.service import BotService

log = logging.getLogger("meshtech-bot.handlers")


class Handler:
    name: str = "handler"
    description: str = ""
    keywords: List[str] = []
    scope: str = "both"            # both | channel | dm
    access: str = "public"         # public | admin
    # Optional per-keyword overrides for handlers exposing several commands:
    keyword_scope: Dict[str, str] = {}    # keyword -> scope override
    keyword_access: Dict[str, str] = {}   # keyword -> access override
    require_prefix: bool = True    # !<keyword> unless False (plain word match)
    priority: int = 100            # lower runs first

    def attach(self, service: "BotService") -> None:
        """Called once at startup / reload; store the service reference."""
        self.service = service

    async def handle(self, ctx: "RouterCtx") -> Optional[HandlerResult]:
        """Compute the reply for one message. Return None to stay silent."""
        raise NotImplementedError

    def render_lines(self, result: HandlerResult, verbosity: str) -> List[str]:
        """Turn a structured result into display lines for the given verbosity."""
        data = result.data
        if isinstance(data, str):
            return data.splitlines() or [""]
        if data is None:
            return []
        return [str(data)]

    # -- helpers shared by handlers --------------------------------------

    @property
    def log(self):
        return log
