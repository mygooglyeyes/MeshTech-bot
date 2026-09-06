"""pushbudget - airtime guardrail for scheduled module pushes.

Not a mesh command: this "module" exists only as a web-console card (the
same form the other modules use) where the operator tunes how much airtime
scheduled pushes may consume:

    - gap_seconds    minimum seconds between any two module pushes
    - max_per_hour   most pushes allowed in any rolling hour
    - max_per_day    most pushes allowed in any rolling day

core.service._module_push enforces these and reports every dropped push to
the activity feed. Disabling this card turns the budget off (not
recommended - it is the flood guard).

Defaults are deliberately quiet: 30 s apart, 5/hour, 15/day. MeshCore
channels are shared airtime; a module gone haywire must degrade to silence,
not to noise.
"""
from __future__ import annotations

from typing import Optional

from core.models import HandlerResult
from .base import Handler
from core.modules import ModuleSpec


class PushBudgetModule(ModuleSpec):
    name = "pushbudget"
    keywords: list = []                    # empty: never matches a mesh command
    description = "Push budget guardrail (console-only)"
    scope = "channel"
    access = "public"
    require_prefix = True
    priority = 99
    # available=True so the console renders its settings form. It has no
    # keywords, so no mesh message can ever reach it - it is a card only.

    menu_description = ("Airtime guardrail for scheduled pushes: the "
                        "minimum gap between pushes and the most pushes "
                        "allowed per hour / per day. Every push over the "
                        "budget is dropped and noted in the activity feed. "
                        "Leave the card off to remove the limit "
                        "(not recommended).")

    settings_fields = [
        {"key": "gap_seconds", "label": "Minimum gap (seconds)", "type": "number",
         "default": "30", "help": "No two module pushes closer than this. "
         "Leave blank for the default of 30 seconds."},
        {"key": "max_per_hour", "label": "Max pushes per hour", "type": "number",
         "default": "5", "help": "Most pushes allowed in any rolling hour. "
         "Leave blank for the default of 5."},
        {"key": "max_per_day", "label": "Max pushes per day", "type": "number",
         "default": "15", "help": "Most pushes allowed in any rolling day. "
         "Leave blank for the default of 15."},
    ]

    async def handle(self, ctx) -> Optional[HandlerResult]:  # pragma: no cover
        return None
