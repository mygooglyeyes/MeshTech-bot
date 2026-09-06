"""Weather module - the template for MeshTech-Bot add-on modules.

Mesh command:
    !weather          - conditions RIGHT NOW at the module's default zip
    !weather 84321    - conditions right now for that zip
    !weatherx         - adds a 3-day outlook

Data: National Weather Service api.weather.gov (free, no API key);
zip -> coordinates via zippopotam.us (free, no key). US coverage only.
The NWS "current" block is actually today's forecast, so filler words
(Sunny, Clear, Partly Sunny...) are stripped and the reply is labelled
"now" - it reads as conditions, not a forecast.

Scheduled push (off until the user enables it in the console):
polls NWS every ``poll_minutes`` and posts one short line when the
conditions summary changes - rare by design, mesh traffic is precious.

Everything network-related is defensive: timeouts, quiet failure logs,
never an exception into the router.
"""
from __future__ import annotations

import logging
import re
import time
from typing import Any, Dict, Optional

from core.models import HandlerResult
from core.modules import ModuleSpec
from .base import Handler  # noqa: F401  (kept so plain handlers can copy this file)

log = logging.getLogger("meshtech-bot.modules.weather")

_HTTP_TIMEOUT = 10          # seconds per API call
_CACHE_SECONDS = 600        # reuse a forecast for 10 min (mesh can spam us)

# NWS shortForecast words that carry no information on a one-line
# conditions readout (everything NWS emits is technically a forecast, so
# "Sunny" just means "no weather happening").
_WEATHER_PLACE = re.compile(
    r"^(chance|slight chance|isolated|scattered|areas of|patches of|"
    r"slight chance|mostly|partly|then)\s+", re.IGNORECASE)
_WEATHER_WORDS = {
    "sunny", "clear", "mostly sunny", "mostly clear", "partly sunny",
    "partly cloudy", "mostly cloudy", "cloudy", "overcast", "fair",
    "chance", "", "none",
}


def _strip_forecast_words(text: str) -> str:
    """Drop leading forecast-filler ("Chance Rain Showers" -> "Rain Showers")."""
    prev = None
    while prev != text:
        prev = text
        text = _WEATHER_PLACE.sub("", text).strip()
    return text


def short_conditions(period: Dict[str, Any]) -> str:
    """One compact condition line from an NWS forecast period dict."""
    temp = period.get("temperature")
    unit = period.get("temperatureUnit") or "F"
    wind = (period.get("windSpeed") or "").replace(" mph", "mph")
    raw = (period.get("shortForecast") or "").strip()
    desc = "" if raw.lower() in _WEATHER_WORDS else _strip_forecast_words(raw)
    parts = []
    if temp is not None:
        parts.append(f"{temp}F" if unit.startswith("F") else f"{temp}{unit}")
    if wind:
        parts.append(f"wind {wind}")
    if desc:
        parts.append(desc)
    return " ".join(parts)


def format_weather(place: str, current_line: str, outlook: Optional[list] = None) -> str:
    """The mesh text. Brief = one line; x = + 3-day outlook lines."""
    head = (f"Weather {place} now: {current_line}" if place
            else f"Weather now: {current_line}")
    lines = [head]
    for label, line in (outlook or []):
        lines.append(f"{label}: {line}")
    return "\n".join(lines)


class WeatherModule(ModuleSpec):
    name = "weather"
    description = "Local weather"
    keywords = ["weather"]
    scope = "both"
    access = "public"
    require_prefix = True

    # ---- menu declaration (core/modules.py reads this) -------------------
    menu_description = "Current conditions and forecast for a US zip code."
    settings_fields = [
        {"key": "zip", "label": "Default ZIP code", "type": "text",
         "default": "", "help": "Used when someone types !weather without a zip"},
        {"key": "channel", "label": "Push channel", "type": "channel",
         "default": "", "help": "Where scheduled pushes go (leave empty for no push)"},
        {"key": "poll_minutes", "label": "Check every (minutes)", "type": "number",
         "default": 30, "help": "How often push mode checks for changes"},
    ]

    def __init__(self) -> None:
        self._cache: Dict[str, Any] = {}   # zip -> (ts, place, current, outlook)

    # ---------------------------------------------------------------- mesh

    async def handle(self, ctx) -> Optional[HandlerResult]:
        zip_code = ((ctx.args[0] if ctx.args else "") or "").strip() or \
            str(self.setting("zip", "") or "")
        if not zip_code:
            return HandlerResult(kind="text",
                                 data="Weather: no zip. Try  !weather 84321")
        if not zip_code.isdigit() or len(zip_code) != 5:
            return HandlerResult(kind="text", data="Weather: zip must be 5 digits")

        extended = ctx.verbosity == "full"
        try:
            place, current, outlook = await self._forecast(zip_code,
                                                           want_outlook=extended)
        except Exception as exc:
            log.info("weather lookup failed for %s: %s", zip_code, exc)
            return HandlerResult(kind="text", data="Weather: lookup failed, try later")
        return HandlerResult(
            kind="text",
            data=format_weather(place, current, outlook if extended else None))

    # ---------------------------------------------------------------- push

    def pulse_seconds(self) -> Optional[int]:
        # opt in only when the user configured a push channel
        return int(self.setting("poll_minutes", 30) or 30) * 60

    async def pulse(self) -> Optional[str]:
        """Push one line when the conditions summary for the default zip
        changes. No zip/channel configured -> nothing to do."""
        zip_code = str(self.setting("zip", "") or "")
        channel = str(self.setting("channel", "") or "")
        if not zip_code or not channel or not self.is_enabled():
            return None
        try:
            place, current, _ = await self._forecast(zip_code, want_outlook=False)
        except Exception as exc:
            log.debug("pulse weather lookup failed: %s", exc)
            return None
        last = self._cache.get("push_last")
        if last is not None and last == current:
            return None                      # nothing changed - stay quiet
        self._cache["push_last"] = current
        if last is None:
            return None                      # first observation: baseline only
        return f"Weather {place} now: {current}"

    # ---------------------------------------------------------------- api

    async def _forecast(self, zip_code: str, want_outlook: bool):
        """Returns (place, current_line, outlook_lines|None). Cached."""
        now = time.time()
        cached = self._cache.get(zip_code)
        if cached and now - cached[0] < _CACHE_SECONDS and \
                (not want_outlook or cached[3]):
            place, current, outlook = cached[1], cached[2], cached[3]
            return place, current, (outlook if want_outlook else None)

        import aiohttp
        async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=_HTTP_TIMEOUT)) as http:
            # zip -> coordinates
            async with http.get(f"https://api.zippopotam.us/us/{zip_code}") as r:
                if r.status != 200:
                    raise RuntimeError(f"zip lookup HTTP {r.status}")
                geo = await r.json(content_type=None)
            places = geo.get("places") or []
            if not places:
                raise RuntimeError("zip not found")
            place = ", ".join(x for x in (places[0].get("place name"),
                                          places[0].get("state abbreviation")) if x)
            lat, lon = places[0]["latitude"], places[0]["longitude"]

            # NWS: gridpoint -> forecast
            async with http.get(f"https://api.weather.gov/points/{lat},{lon}",
                                headers={"User-Agent": "meshtech-bot/0.1"}) as r:
                if r.status != 200:
                    raise RuntimeError(f"NWS points HTTP {r.status}")
                points = await r.json(content_type=None)
            forecast_url = (points.get("properties") or {}).get("forecast") or ""
            if not forecast_url:
                raise RuntimeError("NWS has no forecast for this point")
            async with http.get(forecast_url,
                                headers={"User-Agent": "meshtech-bot/0.1"}) as r:
                if r.status != 200:
                    raise RuntimeError(f"NWS forecast HTTP {r.status}")
                fc = await r.json(content_type=None)

        periods = ((fc.get("properties") or {}).get("periods") or [])
        if not periods:
            raise RuntimeError("NWS forecast empty")
        current = short_conditions(periods[0])

        outlook = None
        if want_outlook:
            outlook = []
            seen_days = set()
            for p in periods[1:]:
                name = p.get("name") or ""
                day = name.split(" ")[0] if name else ""
                if not name or name in seen_days or name in ("Tonight",):
                    continue
                if day and f"{day}Night" in seen_days:
                    continue
                seen_days.add(name)
                outlook.append((name, short_conditions(p)))
                if len(outlook) >= 3:
                    break

        self._cache[zip_code] = (now, place, current, outlook)
        return place, current, (outlook if want_outlook else None)
