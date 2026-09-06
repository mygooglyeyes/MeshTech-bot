"""alerts - active NWS weather alerts for an area.

On demand (``!alerts [zip]``) the module lists currently-active alerts
for the zip's point, most severe first. When enabled with a push
channel it also checks on a interval and posts each NEW alert once
(deduped by the alert's NWS id), filtered by severity.

Alert text is kept short on purpose - a mesh reply should fit one
packet:

    ALERT Severe Tstorm Warning - Cache County - until 18:45

Data: api.weather.gov/alerts/active?point=<lat>,<lon> (free, no key;
same geocoding as the weather module). Severity filter and dedupe
state live in the module; the scheduler only wakes us to check.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional, Tuple

from core.models import HandlerResult
from .base import Handler
from .weather import WeatherModule, _HTTP_TIMEOUT   # geocoder + timing

log = logging.getLogger("meshtech-bot.alerts")

# severities that are worth pushing to a channel (NWS vocabulary).
# "Unknown" and "Other" are noise; Extreme/Severe/Moderate matter.
_PUSH_SEVERITIES = {"Extreme", "Severe", "Moderate"}

_CACHE_SECONDS = 120          # on-demand replies cache this long


def format_alert_line(props: Dict[str, Any],
                      now: Optional[float] = None) -> Optional[str]:
    """One compact line for an alert feature dict, or None if unusable."""
    event = (props.get("event") or "").strip()
    area = (props.get("areaDesc") or "").strip().split(";")[0].strip()
    expires = (props.get("expires") or "").strip()
    until = ""
    if expires:
        try:
            # "2026-09-06T18:45:00-06:00" -> "until 18:45"
            from datetime import datetime
            dt = datetime.fromisoformat(expires)
            until = f" until {dt:%H:%M}"
        except ValueError:
            pass
    if not event:
        return None
    head = f"ALERT {event}"
    if area:
        head += f" - {area}"
    return head + until


def sort_alerts(features: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Most severe first, then soonest expiry."""
    rank = {"Extreme": 0, "Severe": 1, "Moderate": 2}
    def key(f):
        p = f.get("properties", {})
        return (rank.get(p.get("severity"), 3),
                p.get("expires") or "9999")
    return sorted(features, key=key)


def alert_ids(features: List[Dict[str, Any]]) -> set:
    ids = set()
    for f in features:
        aid = (f.get("properties") or {}).get("id")
        if aid:
            ids.add(aid)
    return ids


class AlertsModule(WeatherModule):
    """Shares the weather module's geocoder and HTTP timing."""
    name = "alerts"
    # no keywords of its own that collide with weather; we do want the
    # !alerts command though:
    keywords = ["alerts"]
    description = "Active NWS weather alerts for an area"
    scope = "both"
    access = "public"
    require_prefix = True
    priority = 82

    menu_description = ("Active NWS alerts (!alerts [zip]) with an "
                        "optional push to one or more channels when new "
                        "alerts appear.")
    settings_fields = [
        {"key": "zip", "label": "Default ZIP code", "type": "text",
         "default": "", "help": "Used when someone types !alerts without a zip"},
        {"key": "push_channels", "label": "Push channels", "type": "channels",
         "default": "", "help": "Comma-separated channels for new alerts, "
         "e.g. #novato, #alert (leave empty for no push)"},
        {"key": "check_minutes", "label": "Check every (minutes)", "type": "number",
         "default": "5", "help": "How often to look for new alerts when pushing is on"},
        {"key": "min_severity", "label": "Push severity", "type": "choice",
         "default": "Severe", "choices": ["Severe", "Moderate", "Minor"],
         "help": "Lowest severity worth pushing (Severe = fewer posts)"},
    ]

    def __init__(self) -> None:
        self._cache: Dict[str, Any] = {}      # zip -> (ts, features)
        self._seen: set = set()               # alert ids already pushed
        self._seen_primed = False

    # ---------------------------------------------------------------- mesh

    async def handle(self, ctx) -> Optional[HandlerResult]:
        zip_code = ((ctx.args[0] if ctx.args else "") or "").strip() or \
            str(self.setting("zip", "") or "")
        if not zip_code:
            return HandlerResult(kind="text",
                                 data="Alerts: no zip. Try  !alerts 84321")
        if not zip_code.isdigit() or len(zip_code) != 5:
            return HandlerResult(kind="text", data="Alerts: zip must be 5 digits")

        try:
            place, features = await self._alerts_for(zip_code)
        except Exception as exc:
            log.info("alerts lookup failed for %s: %s", zip_code, exc)
            return HandlerResult(kind="text", data="Alerts: lookup failed, try later")

        if not features:
            return HandlerResult(kind="text",
                                 data=f"No active alerts for {place}")

        extended = ctx.verbosity == "full"
        lines: List[str] = []
        for f in (features if extended else features[:3]):
            line = format_alert_line(f.get("properties", {}))
            if line:
                lines.append(line)
        if not extended and len(features) > 3:
            lines.append(f"+{len(features) - 3} more - try !alertsx")
        return HandlerResult(kind="text", data="\n".join(lines))

    # ---------------------------------------------------------------- push

    def _check_minutes(self) -> int:
        try:
            return max(2, min(120, int(str(self.setting("check_minutes", 5)))))
        except (TypeError, ValueError):
            return 5

    def pulse_seconds(self) -> Optional[int]:
        if not self.push_channels():
            return None
        return self._check_minutes() * 60

    async def pulse(self) -> Optional[str]:
        zip_code = str(self.setting("zip", "") or "")
        channels = self.push_channels()
        if not zip_code or not channels or not self.is_enabled():
            return None
        try:
            _place, features = await self._alerts_for(zip_code)
        except Exception as exc:
            log.info("alerts push check failed: %s", exc)
            return None

        current = alert_ids(features)
        floor = str(self.setting("min_severity", "Severe") or "Severe")
        order = ["Minor", "Moderate", "Severe", "Extreme"]
        allowed = set(order[order.index(floor):]) if floor in order else _PUSH_SEVERITIES

        fresh = []
        for f in features:
            p = f.get("properties", {})
            if p.get("id") in self._seen:
                continue
            if p.get("severity") not in allowed:
                continue
            line = format_alert_line(p)
            if line:
                fresh.append(line)
        if not self._seen_primed:
            self._seen_primed = True     # first check: baseline, no push
            self._seen = current
            return None
        self._seen |= current
        if not fresh:
            return None
        return "\n".join(fresh[:3])      # never spam more than 3

    # ---------------------------------------------------------------- api

    async def _alerts_for(self, zip_code: str) -> Tuple[str, List[Dict[str, Any]]]:
        """(place, active alert features) for a zip, cached briefly."""
        now = time.time()
        hit = self._cache.get(zip_code)
        if hit and now - hit[0] < _CACHE_SECONDS:
            return hit[1], hit[2]
        import aiohttp
        async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=_HTTP_TIMEOUT)) as http:
            place, lat, lon = await WeatherModule._geocode(http, zip_code)
            async with http.get(
                    f"https://api.weather.gov/alerts/active?point={lat},{lon}",
                    headers={"User-Agent": "meshtech-bot/0.1"}) as r:
                if r.status != 200:
                    raise RuntimeError(f"NWS alerts HTTP {r.status}")
                data = await r.json(content_type=None)
        features = sort_alerts(data.get("features", []))
        self._cache[zip_code] = (now, place, features)
        return place, features
