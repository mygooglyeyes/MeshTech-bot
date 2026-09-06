"""Module menu - optional add-on features users can switch on from the
web console.

A module is a Handler (see handlers/base.py) that additionally declares
its menu entry: a plain-language description and the settings fields the
console should offer (zip code, push channel, poll interval...). The
console reads the declaration to build the form and writes the user's
answers into the ``modules:`` section of config.yaml - so every module's
settings are editable without touching code.

Fields are deliberately simple on purpose:

  * text    - free text (zip codes, names)
  * number  - integer (minutes, magnitudes)
  * choice  - one of ``choices`` (rendered as a dropdown)
  * channel - a channel name from the bot's configured channels

A module opts into scheduled pushes by overriding ``pulse_seconds()``
(returns None = this module never pushes). The service's scheduler only
starts a poller for enabled modules that both declare a pulse and have
whatever settings it needs (e.g. a push channel).
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from handlers.base import Handler

if TYPE_CHECKING:
    from .service import BotService


# 24-hour "HH:MM" (also accepts "H:MM") - the storage format for time
# fields (e.g. weather's daily post time). The console shows a 12-hour
# editor; this is what ends up in config.yaml.
_HHMM_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def valid_hhmm(value: str) -> bool:
    """True for a 24-hour 'HH:MM' time string ('7:05', '19:30')."""
    return bool(_HHMM_RE.match((value or "").strip()))


class ModuleSpec(Handler):
    """A menu-manageable module: a Handler plus its console declaration."""

    # ---- menu entry (console only; no mesh behavior) --------------------
    menu_description: str = ""       # one plain sentence for the Modules card
    settings_fields: List[Dict[str, Any]] = []
    # e.g. [{"key": "zip", "label": "Default ZIP code", "type": "text",
    #        "default": "", "help": "US zip for !weather"}]

    # Placeholder support: a module can show as "unavailable" in the menu
    available: bool = True
    unavailable_reason: str = ""

    # ---- lifecycle -------------------------------------------------------
    def attach(self, service: "BotService") -> None:
        super().attach(service)
        self.on_settings_changed()

    def on_settings_changed(self) -> None:
        """Called at attach and after the console saves new settings."""

    # ---- scheduled push --------------------------------------------------
    def pulse_seconds(self) -> Optional[int]:
        """Poll interval for scheduled pushes, in seconds.

        None (default) = this module never pushes. Return an int to opt in;
        the scheduler also requires the module to be enabled.
        """
        return None

    async def pulse(self) -> Optional[str]:
        """One scheduled check. Return mesh text to push, or None if the
        module has nothing new to say (the common case - pushes should be
        rare and only on change)."""
        return None

    # ---- settings helpers --------------------------------------------------
    def setting(self, key: str, default: Any = None) -> Any:
        cfg = self.service.settings.modules.get(self.name)
        value = cfg.settings.get(key)
        return default if value is None or value == "" else value

    def is_enabled(self) -> bool:
        return bool(self.service.settings.modules.get(self.name).enabled)

    def validate_settings(self, values: Dict[str, Any]) -> List[str]:
        """Console-side validation; returns plain-language problems."""
        problems: List[str] = []
        for field_def in self.settings_fields:
            key = field_def.get("key")
            value = values.get(key)
            if value in (None, ""):
                continue
            ftype = field_def.get("type", "text")
            if ftype == "number":
                try:
                    int(str(value))
                except (TypeError, ValueError):
                    problems.append(f"{field_def.get('label', key)} must be a whole number")
            elif ftype == "choice":
                choices = field_def.get("choices") or []
                if choices and str(value) not in choices:
                    problems.append(f"{field_def.get('label', key)} must be one of: "
                                    + ", ".join(str(c) for c in choices))
            elif ftype == "time12":
                if not valid_hhmm(str(value)):
                    problems.append(f"{field_def.get('label', key)} must be a time "
                                    "like 7:30 pm (saved as 19:30)")
        return problems


# --------------------------------------------------------------------------
# Registry - the menu the console renders. Order here = order in the card.
# --------------------------------------------------------------------------

def module_menu() -> List[Dict[str, Any]]:
    """The console menu: every known module, its declaration and state.

    Built from the handler discovery: any handler class that is a
    ModuleSpec appears here automatically. Placeholder entries (NIXLE)
    are appended so users can see what's coming.
    """
    from handlers import discover_handlers  # local import: no cycle

    items: List[Dict[str, Any]] = []
    seen = set()
    for cls in discover_handlers():
        if not issubclass(cls, ModuleSpec):
            continue
        items.append({
            "name": cls.name,
            "description": cls.menu_description,
            "available": cls.available,
            "unavailable_reason": cls.unavailable_reason,
            "fields": cls.settings_fields,
            "has_pulse": cls.__dict__.get("pulse_seconds",
                                          ModuleSpec.pulse_seconds) is not ModuleSpec.pulse_seconds,
        })
        seen.add(cls.name)
    # Placeholder for announced-but-not-buildable modules: none right now.
    # (NIXLE was removed - it offers no open API, so the entry could never
    # become real. Re-add a placeholder block here if one is ever needed.)
    return items
