"""Canned answers - the code-free layer.

Every rule from the ``replies:`` section of config.yaml becomes part of this
handler's keyword list. Replies are selected at random when a rule lists
several texts. Runs last (lowest priority) so a dedicated command handler
wins when keywords overlap.
"""
from __future__ import annotations

import random
from typing import List, Optional

from core.models import HandlerResult
from .base import Handler


class CannedHandler(Handler):
    name = "canned"
    description = "Simple keyword answers from config.yaml"
    scope = "both"
    access = "public"
    require_prefix = False      # "hello" works without the !
    priority = 500

    def attach(self, service) -> None:
        super().attach(service)
        # Dynamic keywords come from the config's replies section
        keywords: List[str] = []
        for rule in service.settings.replies:
            keywords.extend(rule.keywords)
        self.keywords = sorted(set(keywords))

    async def handle(self, ctx) -> Optional[HandlerResult]:
        for rule in ctx.service.settings.replies:
            if ctx.command in rule.keywords:
                return HandlerResult(kind="text", data=random.choice(rule.texts))
        return None
