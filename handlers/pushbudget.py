"""pushbudget - flood guardrails, two layers.

Not a mesh command: this "module" exists only as a web-console card (the
same form the other modules use). It owns two independent limit sets:

Per person (keyword replies only - pushes have no person):
    - person_gap_seconds    wait between answers to ONE requester
    - person_max_per_hour   most answers one person gets per rolling hour
    - person_max_per_day    most answers one person gets per rolling day
    A "person" is the DM sender's key prefix, or in channels the embedded
    name resolved to a known node (same identity the block list uses).
    Admins are exempt.

Total (everything the bot transmits - replies AND pushes combined):
    - gap_seconds    minimum seconds between any two transmissions
    - max_per_hour   most transmissions per rolling hour
    - max_per_day    most transmissions per rolling day

Both layers are enforced in core.service.budget_check / person_budget_check
and the router; every drop is reported to the activity feed. Disabling
this card turns both layers off (not recommended - it is the flood
guard).
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

    menu_description = ("Flood guardrails, two layers. Per person: how "
                        "often one requester can be answered (30 s gap, 5 "
                        "per hour, 15 per day; admins exempt; pushes not "
                        "counted). Total: how much the bot may transmit "
                        "overall, replies and pushes combined (30 s gap, "
                        "30 per hour, 250 per day). Anything over either "
                        "limit is dropped and noted in the activity feed. "
                        "Leave the card off to remove the limits (not "
                        "recommended).")

    settings_fields = [
        # -- per person (keyword replies only; pushes have no person) ----
        {"key": "person_gap_seconds", "label": "Per person: gap (seconds)",
         "type": "number", "default": "30",
         "help": "After the bot answers one requester, that person waits "
         "this long before being answered again. Leave blank for the "
         "default of 30 seconds."},
        {"key": "person_max_per_hour", "label": "Per person: max per hour",
         "type": "number", "default": "5",
         "help": "Most answers one person can get in any rolling hour. "
         "Leave blank for the default of 5."},
        {"key": "person_max_per_day", "label": "Per person: max per day",
         "type": "number", "default": "15",
         "help": "Most answers one person can get in any rolling day. "
         "Leave blank for the default of 15."},
        # -- total (replies + pushes combined) ---------------------------
        {"key": "gap_seconds", "label": "Total: gap (seconds)",
         "type": "number", "default": "30",
         "help": "No two bot transmissions (any reply or push) closer than "
         "this. Leave blank for the default of 30 seconds."},
        {"key": "max_per_hour", "label": "Total: max per hour",
         "type": "number", "default": "30",
         "help": "Most transmissions (replies + pushes) in any rolling "
         "hour. Leave blank for the default of 30."},
        {"key": "max_per_day", "label": "Total: max per day",
         "type": "number", "default": "250",
         "help": "Most transmissions (replies + pushes) in any rolling "
         "day. Leave blank for the default of 250."},
    ]

    async def handle(self, ctx) -> Optional[HandlerResult]:  # pragma: no cover
        return None
