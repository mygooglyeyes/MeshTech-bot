"""Weather module - the template for MeshTech-Bot add-on modules.

Two feeds, two jobs:

  Current conditions (on demand, the mesh asks us):
    !wx              - conditions RIGHT NOW at the module's default zip
    !wx 84321        - conditions right now for that zip
    !weather         - same command, longer name (alias)
    add x: !wxx      - adds a 3-day outlook
    Measured first: the bot reads the nearest reporting station's latest
    observation and marks it (obs KLGU). If no station has reported
    recently (over 90 minutes) it falls back to the NWS forecast block.

  Daily forecast (we push, once a day):
    posts the 3-day outlook to the configured channel at the configured
    local time (settings: channel + post_time, e.g. "07:00").

Data: National Weather Service api.weather.gov (free, no API key);
zip -> coordinates via zippopotam.us (free, no key). US coverage only.
The NWS "current" block is actually today's forecast, so filler words
(Sunny, Clear, Partly Sunny...) are stripped there - but a station
observation's description is a real measurement and passes through.

Everything network-related is defensive: timeouts, quiet failure logs,
never an exception into the router.
"""
from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from core.models import HandlerResult
from core.modules import ModuleSpec

log = logging.getLogger("meshtech-bot.modules.weather")

_HTTP_TIMEOUT = 10          # seconds per API call
_CACHE_SECONDS = 600        # reuse a forecast for 10 min (mesh can spam us)
_PULSE_CHECK_SECONDS = 60   # how often the daily-post pulse wakes up
_OBS_MAX_AGE_SECONDS = 90 * 60   # station readings older than this are stale
_OBS_CACHE_SECONDS = 120    # reuse an observation for 2 min


# --------------------------------------------------------------------------
# Pure helpers (unit-tested)
# --------------------------------------------------------------------------

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

_POST_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def _strip_forecast_words(text: str) -> str:
    """Drop leading forecast-filler ("Chance Rain Showers" -> "Rain Showers")."""
    prev = None
    while prev != text:
        prev = text
        text = _WEATHER_PLACE.sub("", text).strip()
    return text


def parse_post_time(value: str) -> Optional[Tuple[int, int]]:
    """'HH:MM' -> (hours, minutes) 24-hour local; None if not valid."""
    m = _POST_TIME_RE.match((value or "").strip())
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


def seconds_until(hours: int, minutes: int,
                  now: Optional[datetime] = None) -> int:
    """Seconds from `now` (local) until the next HH:MM wall-clock slot."""
    local = now or datetime.now()
    target = local.replace(hour=hours, minute=minutes, second=0, microsecond=0)
    if target <= local:
        target += timedelta(days=1)
    return max(30, int((target - local).total_seconds()))


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


def format_weather(place: str, current_line: str,
                   outlook: Optional[List[Tuple[str, str]]] = None,
                   station: Optional[str] = None) -> str:
    """The mesh text for on-demand conditions. Brief = one line;
    x = + 3-day outlook lines. ``station`` marks the line as measured
    ("obs KLGU") rather than forecast-derived."""
    tag = f" (obs {station})" if station else ""
    head = (f"Weather {place} now{tag}: {current_line}" if place
            else f"Weather now{tag}: {current_line}")
    lines = [head]
    for label, line in (outlook or []):
        lines.append(f"{label}: {line}")
    return "\n".join(lines)


def observation_conditions(props: Dict[str, Any]) -> Optional[str]:
    """One compact line from an NWS latest-observation payload.

    Observation values arrive in SI units (temperature in C, wind in
    km/h); both are converted to the module's F/mph house style. Any
    missing value is simply left out. Returns None when the observation
    carries nothing usable.
    """
    temp = (props.get("temperature") or {}).get("value")
    wind = (props.get("windSpeed") or {}).get("value")     # km/h
    desc = (props.get("textDescription") or "").strip()
    parts = []
    if temp is not None:
        parts.append(f"{round(temp * 9 / 5 + 32)}F")
    if wind is not None:
        parts.append(f"wind {round(wind * 0.621371)}mph")
    if desc:
        parts.append(desc)          # observed, not forecast: keep as-is
    return " ".join(parts) or None


def observation_age_seconds(props: Dict[str, Any],
                            now: Optional[datetime] = None) -> Optional[float]:
    """Age of an observation in seconds, or None when its timestamp is
    missing or unparseable (callers treat None as too old)."""
    raw = (props.get("timestamp") or "").strip()
    if not raw:
        return None
    try:
        ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    ref = now or datetime.now(timezone.utc)
    return max(0.0, (ref - ts).total_seconds())


def station_ids(features: List[Dict[str, Any]]) -> List[str]:
    """Station identifiers from an NWS station-list payload, nearest
    first (the API sorts by distance); blanks dropped."""
    ids = []
    for feature in features or []:
        sid = ((feature.get("properties") or {}).get("stationIdentifier")
               or "").strip()
        if sid:
            ids.append(sid)
    return ids


def format_forecast(place: str,
                    outlook: List[Tuple[str, str]]) -> str:
    """The mesh text for the daily push: place + one line per day."""
    lines = [f"Forecast {place}:"]
    for label, line in outlook:
        lines.append(f"{label}: {line}")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# The module
# --------------------------------------------------------------------------

class WeatherModule(ModuleSpec):
    name = "weather"
    description = "Current conditions on demand plus a daily forecast post."
    keywords = ["weather", "wx"]
    scope = "both"
    access = "public"
    require_prefix = True

    # ---- menu declaration (core/modules.py reads this) -------------------
    menu_description = ("Current conditions (!wx [zip]) and a daily "
                        "forecast post at a set time.")
    settings_fields = [
        {"key": "zip", "label": "Default ZIP code", "type": "text",
         "default": "", "help": "Used when someone types !wx without a zip"},
        {"key": "channel", "label": "Forecast channel", "type": "channel",
         "default": "", "help": "Where the daily forecast posts (leave empty for no post)"},
        {"key": "post_time", "label": "Forecast time", "type": "time12",
         "default": "07:00", "help": "Local time for the daily forecast post "
         "(12-hour picker; saved as 24-hour HH:MM)"},
    ]

    def __init__(self) -> None:
        self._cache: Dict[str, Any] = {}   # zip -> (ts, place, current, outlook)
        self._obs_cache: Dict[str, Any] = {}   # zip -> (ts, station, line)
        self._next_post: Optional[float] = None   # epoch of the next daily post

    def on_settings_changed(self) -> None:
        # Re-arm the daily timer: the next wake-up recomputes it from the
        # (possibly new) post_time.
        self._next_post = None

    # ---------------------------------------------------------------- mesh

    async def handle(self, ctx) -> Optional[HandlerResult]:
        zip_code = ((ctx.args[0] if ctx.args else "") or "").strip() or \
            str(self.setting("zip", "") or "")
        if not zip_code:
            return HandlerResult(kind="text",
                                 data="Weather: no zip. Try  !wx 84321")
        if not zip_code.isdigit() or len(zip_code) != 5:
            return HandlerResult(kind="text", data="Weather: zip must be 5 digits")

        extended = ctx.verbosity == "full"
        try:
            place, current, outlook, station = await self._conditions(
                zip_code, want_outlook=extended)
        except Exception as exc:
            log.info("weather lookup failed for %s: %s", zip_code, exc)
            return HandlerResult(kind="text", data="Weather: lookup failed, try later")
        return HandlerResult(
            kind="text",
            data=format_weather(place, current,
                                outlook if extended else None, station))

    # ------------------------------------------------- daily forecast push

    def post_time_parts(self) -> Optional[Tuple[int, int]]:
        return parse_post_time(str(self.setting("post_time", "07:00") or ""))

    def pulse_seconds(self) -> Optional[int]:
        # The scheduler re-asks after every cycle, so a steady 60 s cadence
        # is all we need: each wake-up checks the wall clock and only posts
        # when the configured HH:MM has arrived.
        if self.post_time_parts() is None:
            return 0                     # no valid post_time -> no daily post
        return _PULSE_CHECK_SECONDS

    async def pulse(self) -> Optional[str]:
        """Daily forecast post: fire once when the configured time arrives."""
        zip_code = str(self.setting("zip", "") or "")
        channel = str(self.setting("channel", "") or "")
        parts = self.post_time_parts()
        if not zip_code or not channel or not self.is_enabled() or parts is None:
            return None
        if self._next_post is None:
            # Fresh (re)load: arm the timer for the next slot, post nothing.
            self._next_post = seconds_until(*parts)
            return None
        if time.time() < self._next_post:
            return None
        self._next_post = None           # consumed; re-armed only on success
        try:
            place, _current, outlook = await self._forecast(zip_code,
                                                            want_outlook=True)
        except Exception as exc:
            log.info("daily forecast post failed: %s", exc)
            return None
        self._next_post = seconds_until(*parts)
        if not outlook:
            return None
        return format_forecast(place, outlook)

    # ---------------------------------------------------------------- api

    async def _conditions(self, zip_code: str, want_outlook: bool):
        """(place, line, outlook, station) for the on-demand reply.

        A fresh station observation wins when available (``station`` set);
        otherwise the reply falls back to the forecast-derived line. The
        daily forecast post does not come through here - it always uses
        the forecast feed.
        """
        station: Optional[str] = None
        line: Optional[str] = None
        place: Optional[str] = None
        now = time.time()
        obs = self._obs_cache.get(zip_code)
        if obs and now - obs[0] < _OBS_CACHE_SECONDS:
            station, line, place = obs[1], obs[2], obs[3]
        else:
            try:
                import aiohttp
                async with aiohttp.ClientSession(
                        timeout=aiohttp.ClientTimeout(total=_HTTP_TIMEOUT)) as http:
                    place, lat, lon = await self._geocode(http, zip_code)
                    found = await self._observation(http, lat, lon)
            except Exception as exc:
                log.info("observation lookup failed for %s: %s", zip_code, exc)
                found = None
            if found:
                station, line = found
                self._obs_cache[zip_code] = (now, station, line, place)

        if line is None:
            p, current, outlook = await self._forecast(zip_code,
                                                       want_outlook=want_outlook)
            return p, current, (outlook if want_outlook else None), None

        if not want_outlook:
            return place, line, None, station
        # observation + outlook: outlook still comes from the forecast feed
        try:
            _p, _current, outlook = await self._forecast(zip_code,
                                                         want_outlook=True)
        except Exception as exc:
            log.info("forecast unavailable, observation only: %s", exc)
            return place, line, None, station
        return place, line, outlook, station

    @staticmethod
    async def _geocode(http, zip_code: str) -> Tuple[str, str, str]:
        """zip -> (place label, lat, lon) via zippopotam.us."""
        async with http.get(f"https://api.zippopotam.us/us/{zip_code}") as r:
            if r.status != 200:
                raise RuntimeError(f"zip lookup HTTP {r.status}")
            geo = await r.json(content_type=None)
        places = geo.get("places") or []
        if not places:
            raise RuntimeError("zip not found")
        place = ", ".join(x for x in (places[0].get("place name"),
                                      places[0].get("state abbreviation")) if x)
        return place, places[0]["latitude"], places[0]["longitude"]

    @staticmethod
    async def _observation(http, lat, lon) -> Optional[Tuple[str, str]]:
        """Nearest reporting station's latest reading -> (station, line).

        Chain (all NWS, verified against the live API): gridpoint ->
        observationStations list (nearest first) -> each station's latest
        observation. Tries the closest few; returns None when none has
        reported within the freshness window (caller falls back to the
        forecast block)."""
        async with http.get(f"https://api.weather.gov/points/{lat},{lon}",
                            headers={"User-Agent": "meshtech-bot/0.1"}) as r:
            if r.status != 200:
                raise RuntimeError(f"NWS points HTTP {r.status}")
            points = await r.json(content_type=None)
        stations_url = ((points.get("properties") or {})
                        .get("observationStations") or "").strip()
        if not stations_url:
            raise RuntimeError("NWS has no station list for this point")
        async with http.get(stations_url,
                            headers={"User-Agent": "meshtech-bot/0.1"}) as r:
            if r.status != 200:
                raise RuntimeError(f"NWS stations HTTP {r.status}")
            stations = await r.json(content_type=None)
        for station in station_ids(stations.get("features"))[:3]:
            async with http.get(
                    f"https://api.weather.gov/stations/{station}/observations/latest",
                    headers={"User-Agent": "meshtech-bot/0.1"}) as r:
                if r.status != 200:
                    continue
                latest = await r.json(content_type=None)
            props = latest.get("properties") or {}
            age = observation_age_seconds(props)
            if age is None or age > _OBS_MAX_AGE_SECONDS:
                continue                       # stale station - try the next
            line = observation_conditions(props)
            if line:
                return station, line
        return None

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
            place, lat, lon = await self._geocode(http, zip_code)

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
