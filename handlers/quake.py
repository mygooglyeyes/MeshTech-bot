"""quake - recent earthquakes from the USGS feed.

On demand (``!quake [zip]``) the module lists notable quakes within a
radius of the zip's point, most recent first. When enabled with a push
channel it also checks periodically and posts quakes that are new
since the last check (deduped by USGS event id) above a magnitude
floor.

Reply shape - one line per quake, newest first:

    M3.7 20 km E of Castle Rock, Washington - 2h ago

Data: earthquake.usgs.gov FDSN event query (free, no key). Distance is
great-circle km; the radius and magnitude floor are settings.
"""
from __future__ import annotations

import logging
import math
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from core.format import rel_time
from core.models import HandlerResult
from .base import Handler
from .weather import WeatherModule, _HTTP_TIMEOUT

log = logging.getLogger("meshtech-bot.quake")

_DEFAULT_RADIUS_KM = 500
_DEFAULT_MIN_MAG = 2.5
_LOOKBACK_DAYS = 7
_CACHE_SECONDS = 180          # on-demand replies cache this long


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two points, in km."""
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = (math.sin(dp / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2)
    return 2 * r * math.asin(math.sqrt(a))


def format_quake_line(props: Dict[str, Any], coords: List[float],
                      lat: float, lon: float,
                      now: Optional[float] = None) -> Optional[str]:
    """One compact line: 'M3.7 place - 14:32 - 2h ago' (distance when
    useful; local clock time of the quake, 24-hour)."""
    mag = props.get("mag")
    place = (props.get("place") or "").strip()
    ts = (props.get("time") or 0) / 1000.0     # USGS uses milliseconds
    if mag is None or not place:
        return None
    line = f"M{round(mag, 1)} {place}"
    if coords and len(coords) >= 2:
        dist = haversine_km(lat, lon, coords[1], coords[0])
        if dist > 40:                          # nearby quakes don't need it
            line += f" ({round(dist)}km)"
    if ts > 0:
        line += f" - {time.strftime('%H:%M', time.localtime(ts))}"
    if now:
        line += f" - {rel_time(ts, now)}"
    return line


def sort_quakes(features: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Newest first."""
    return sorted(features,
                  key=lambda f: f.get("properties", {}).get("time") or 0,
                  reverse=True)


def quake_ids(features: List[Dict[str, Any]]) -> set:
    ids = set()
    for f in features:
        qid = f.get("id")
        if qid:
            ids.add(qid)
    return ids


class QuakeModule(WeatherModule):
    """Shares the weather module's geocoder and HTTP timing."""
    name = "quake"
    keywords = ["quake"]
    description = "Recent earthquakes near an area (USGS)"
    scope = "both"
    access = "public"
    require_prefix = True
    priority = 81

    menu_description = ("Recent earthquakes (!quake [zip]) with an "
                        "optional push to one or more channels when new "
                        "ones are detected.")
    settings_fields = [
        {"key": "zip", "label": "Default ZIP code", "type": "text",
         "default": "", "help": "Used when someone types !quake without a zip"},
        {"key": "radius_km", "label": "Radius (km)", "type": "number",
         "default": "500", "help": "How far from the zip to look"},
        {"key": "min_mag", "label": "Minimum magnitude", "type": "number",
         "default": "2.5", "help": "Smallest quake to list or push (decimals ok, e.g. 2.5)"},
        {"key": "push_channels", "label": "Push channels", "type": "channels",
         "default": "", "help": "Comma-separated channels for new quakes, "
         "e.g. #novato, #alert (leave empty for no push)"},
        {"key": "check_minutes", "label": "Check every (minutes)", "type": "number",
         "default": "10", "help": "How often to look for new quakes when pushing is on. "
         "Leave blank for the default of 10 minutes."},
    ]

    def __init__(self) -> None:
        self._cache: Dict[str, Any] = {}      # zip -> (ts, place, features)
        self._seen: set = set()               # USGS event ids already pushed
        self._seen_primed = False

    # ---------------------------------------------------------------- mesh

    async def handle(self, ctx) -> Optional[HandlerResult]:
        zip_code = ((ctx.args[0] if ctx.args else "") or "").strip()
        if not zip_code:
            # No zip given: the module's own setting, else the weather
            # module's (set the default zip once, both modules benefit).
            zip_code = str(self.setting("zip", "") or "")
            if not zip_code:
                zip_code = await self._weather_zip()
        if not zip_code:
            return HandlerResult(kind="text",
                                 data="Quake: no zip. Try  !quake 84321")
        if not zip_code.isdigit() or len(zip_code) != 5:
            return HandlerResult(kind="text", data="Quake: zip must be 5 digits")

        try:
            place, features = await self._quakes_for(zip_code)
        except Exception as exc:
            log.info("quake lookup failed for %s: %s", zip_code, exc)
            return HandlerResult(kind="text", data="Quake: lookup failed, try later")

        if not features:
            return HandlerResult(kind="text",
                                 data=f"No quakes M{self._min_mag()}+ "
                                      f"within {self._radius_km()}km of {place} in 7d")

        now = time.time()
        extended = ctx.verbosity == "full"
        shown = features if extended else features[:3]
        lines: List[str] = []
        for f in shown:
            line = format_quake_line(f.get("properties", {}),
                                     (f.get("geometry") or {}).get("coordinates", []),
                                     *self._center(f))
            if line:
                lines.append(line)
        if not extended and len(features) > 3:
            lines.append(f"+{len(features) - 3} more - try !quakex")
        return HandlerResult(kind="text", data="\n".join(lines))

    # --------------------------------------------------------------- helpers

    def _radius_km(self) -> int:
        try:
            return max(10, min(2000, int(float(self.setting("radius_km",
                                                            _DEFAULT_RADIUS_KM)))))
        except (TypeError, ValueError):
            return _DEFAULT_RADIUS_KM

    def _min_mag(self) -> float:
        try:
            return max(0, min(9.9, float(self.setting("min_mag", _DEFAULT_MIN_MAG))))
        except (TypeError, ValueError):
            return _DEFAULT_MIN_MAG

    def _center(self, feature: Dict[str, Any]) -> Tuple[float, float]:
        """Reference point for distance display - the zip, cached on the
        feature fetch; falls back to the quake's own coords."""
        c = getattr(self, "_last_center", None)
        if c:
            return c[0], c[1]
        coords = (feature.get("geometry") or {}).get("coordinates", [])
        if len(coords) >= 2:
            return coords[1], coords[0]
        return 0.0, 0.0

    async def _weather_zip(self) -> str:
        """The weather module's zip, when that card has one set."""
        for handler in getattr(self.service, "registry", None) or []:
            if getattr(handler, "name", "") == "weather":
                try:
                    return str(handler.setting("zip", "") or "")
                except Exception:
                    return ""
        return ""

    # ---------------------------------------------------------------- push

    def _check_minutes(self) -> int:
        try:
            return max(2, min(120, int(str(self.setting("check_minutes", 10)))))
        except (TypeError, ValueError):
            return 10

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
            _place, features = await self._quakes_for(zip_code)
        except Exception as exc:
            log.info("quake push check failed: %s", exc)
            return None

        current = quake_ids(features)
        now = time.time()
        floor = self._min_mag()
        fresh: List[str] = []
        for f in features:
            if f.get("id") in self._seen:
                continue
            p = f.get("properties", {})
            if (p.get("mag") or 0) < floor:
                continue
            line = format_quake_line(p,
                                     (f.get("geometry") or {}).get("coordinates", []),
                                     *self._center(f), now=now)
            if line:
                fresh.append(line)
        if not self._seen_primed:
            self._seen_primed = True     # baseline only, no push
            self._seen = current
            return None
        self._seen |= current
        if not fresh:
            return None
        return "\n".join(fresh[:3])

    # ---------------------------------------------------------------- api

    async def _quakes_for(self, zip_code: str) -> Tuple[str, List[Dict[str, Any]]]:
        """(place, quake features within radius) for a zip, cached briefly."""
        now = time.time()
        hit = self._cache.get(zip_code)
        if hit and now - hit[0] < _CACHE_SECONDS:
            self._last_center = (hit[3], hit[4])
            return hit[1], hit[2]
        import aiohttp
        async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=_HTTP_TIMEOUT)) as http:
            place, lat, lon = await WeatherModule._geocode(http, zip_code)
            # The shared geocoder returns lat/lon as strings (it only ever
            # interpolates them into URLs); distance math needs numbers.
            lat, lon = float(lat), float(lon)
            start = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            import datetime as _dt
            start = (datetime.now(timezone.utc) - _dt.timedelta(days=_LOOKBACK_DAYS)) \
                .strftime("%Y-%m-%d")
            url = ("https://earthquake.usgs.gov/fdsnws/event/1/query?format=geojson"
                   f"&latitude={lat}&longitude={lon}"
                   f"&maxradiuskm={self._radius_km()}"
                   f"&minmagnitude={self._min_mag()}&starttime={start}&orderby=time")
            async with http.get(url) as r:
                if r.status != 200:
                    raise RuntimeError(f"USGS query HTTP {r.status}")
                data = await r.json(content_type=None)
        features = sort_quakes(data.get("features", []))
        self._cache[zip_code] = (now, place, features, lat, lon)
        self._last_center = (lat, lon)
        return place, features
