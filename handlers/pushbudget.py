"""pushbudget - airtime budget for EVERYTHING the bot transmits.

Not a mesh command: this "module" exists only as a web-console card (the
same form the other modules use) where the operator tunes how much airtime
the bot may consume:

    - gap_seconds    minimum seconds between any two bot transmissions
    - max_per_hour   most transmissions allowed in any rolling hour
    - max_per_day    most transmissions allowed in any rolling day

It covers BOTH kinds of traffic: scheduled module pushes (weather
forecasts, alerts, quakes) AND keyword-command replies (!help, !wx, ...).
Everything the bot puts on the air shares one budget, so a crowd of
requesters cannot spam the mesh through it. Every drop is reported to
the activity feed. Disabling this card turns the budget off (not
recommended - it is the flood guard).

Admin DMs are exempt: key-verified administrators stay answerable while
the bot is being flooded, and their traffic does not consume the
strangers' budget.

Defaults are deliberately quiet: 30 s apart, 5/hour, 15/day. MeshCore
channels are shared airtime; a bot gone haywire must degrade to silence,
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

    menu_description = ("Airtime budget for everything the bot transmits "
                        "- module pushes AND command replies share it: the "
                        "minimum gap between transmissions and the most "
                        "allowed per hour / per day. Anything over budget "
                        "is dropped and noted in the activity feed. Admin "
                        "DMs are exempt. Leave the card off to remove the "
                        "limit (not recommended).")

    settings_fields = [
        {"key": "gap_seconds", "label": "Minimum gap (seconds)", "type": "number",
         "default": "30", "help": "No two bot transmissions (push or reply) "
         "closer than this. Leave blank for the default of 30 seconds."},
        {"key": "max_per_hour", "label": "Max transmissions per hour",
         "type": "number", "default": "5",
         "help": "Most pushes + replies combined allowed in any rolling "
         "hour. Leave blank for the default of 5."},
        {"key": "max_per_day", "label": "Max transmissions per day",
         "type": "number", "default": "15",
         "help": "Most pushes + replies combined allowed in any rolling "
         "day. Leave blank for the default of 15."},
    ]

    async def handle(self, ctx) -> Optional[HandlerResult]:  # pragma: no cover
        return None
