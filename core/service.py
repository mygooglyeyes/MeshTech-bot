"""BotService - the shared "application object".

Holds everything the running bot needs in one place so handlers, the router,
the radio client and the web dashboard can talk to each other without
circular imports:
    settings  - live configuration (reload() swaps it for a fresh read)
    store     - SQLite database
    feed      - live event hub for the dashboard
    client    - radio connection (attached after construction)
    router    - message routing (attached after construction)
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Callable, Dict, List, Optional

from .config import Settings, load as load_config, sanitized_snapshot
from .feed import FeedHub
from .store import Store
from .version import version_stamp

log = logging.getLogger("meshtech-bot.service")


class BotService:
    def __init__(self, settings: Settings, store: Store, feed: FeedHub):
        self.settings = settings
        self.store = store
        self.feed = feed
        self.client = None            # set by bot.py (core.client.RadioClient)
        self.router = None            # set by bot.py (core.router.Router)
        self.capture = None           # set by bot.py (core.capture.PacketCapture)
        self.registry: List = []      # sorted handler instances
        self.started_at: float = time.time()
        self.stop_requested = False
        self._stop_callback: Optional[Callable[[], None]] = None
        # Module pulse scheduler (core.modules.ModuleSpec.push): one asyncio
        # task per enabled module that opts in. Rebuilt on config reload.
        self._pulse_tasks: Dict[str, "asyncio.Task"] = {}
        # Airtime budgets (core.service.budget_check / person_budget_check):
        # _push_times holds every transmission (reply or push) for the total
        # windows; _person_times holds per-requester answer times for the
        # per-person windows; _last_push_at backs the total minimum gap.
        self._push_times: List[float] = []
        self._last_push_at: float = 0.0
        self._person_times: Dict[str, List[float]] = {}

    # ------------------------------------------------------------------ basics

    def uptime_seconds(self) -> float:
        return time.time() - self.started_at

    def set_stop_callback(self, callback: Callable[[], None]) -> None:
        self._stop_callback = callback

    # ------------------------------------------------------------------ actions

    def reload(self) -> str:
        """Re-read config.yaml and refresh handlers that depend on it."""
        settings = load_config(self.settings.config_path)
        old = self.settings
        self.settings = settings
        if self.router is not None:
            self.router.on_config_reload()
        # modules may have been enabled/disabled/reconfigured in the console
        for handler in self.registry:
            changed = getattr(handler, "on_settings_changed", None)
            if callable(changed):
                try:
                    changed()
                except Exception:
                    pass
        self.schedule_module_pulses()
        summary = (
            f"Config reloaded from {settings.config_path}: "
            f"{len(settings.channels)} channel(s), {len(self.registry)} handler(s), "
            f"max hops={settings.mesh.max_inbound_hops}."
        )
        if settings.warnings:
            summary += " Warnings: " + "; ".join(settings.warnings[:3])
        if old.web.enabled != settings.web.enabled:
            summary += " (web server change needs a restart to take effect)"
        log.info(summary)
        self.feed.publish("notice", {"text": summary})
        return summary

    def request_shutdown(self, reason: str = "requested") -> None:
        log.info("Shutdown requested: %s", reason)
        self.stop_requested = True
        self.feed.publish("notice", {"text": f"Shutdown {reason}"})
        self.stop_module_pulses()
        if self._stop_callback is not None:
            self._stop_callback()

    # ------------------------------------------------------------- modules

    def schedule_module_pulses(self) -> None:
        """(Re)start pulse pollers for enabled modules; called at startup
        and after every config reload."""
        self.stop_module_pulses()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return                       # not on the event loop yet (startup)
        for handler in self.registry:
            pulse = getattr(handler, "pulse", None)
            seconds_fn = getattr(handler, "pulse_seconds", None)
            if pulse is None or seconds_fn is None:
                continue
            try:
                seconds = int(seconds_fn() or 0)
            except Exception:
                seconds = 0
            if seconds < 30:
                continue                 # opt-in only, sane minimum
            name = getattr(handler, "name", "module")
            self._pulse_tasks[name] = loop.create_task(
                self._module_pulse_loop(handler, seconds))
            log.info("Module pulse started: %s (every %d min)",
                     name, max(1, seconds // 60))

    def stop_module_pulses(self) -> None:
        for name, task in list(self._pulse_tasks.items()):
            if not task.done():
                task.cancel()
        self._pulse_tasks.clear()

    async def _module_pulse_loop(self, handler, seconds: int) -> None:
        name = getattr(handler, "name", "module")
        try:
            await asyncio.sleep(10)      # let the link settle after startup
            while not self.stop_requested:
                try:
                    text = await handler.pulse()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.debug("module %s pulse failed: %s", name, exc)
                    text = None
                if text:
                    await self._module_push(name, text)
                # Re-ask every cycle: a module may schedule its next check
                # dynamically (e.g. weather's daily wall-clock post).
                try:
                    seconds = int(seconds_fn() or 0)
                except Exception:
                    seconds = 0
                if seconds < 30:
                    break                # module turned its pulse off
                await asyncio.sleep(seconds)
        except asyncio.CancelledError:
            pass

    # ---------------------------------------------------------------- budget

    def budget_check(self, kind: str, channel: str, sender: str, text: str,
                     exempt: bool = False, record: bool = True) -> bool:
        """TOTAL airtime budget: every transmission the bot makes - replies
        AND pushes combined.

        Returns True when the transmission may proceed (recording it unless
        ``record`` is False - the router's dispatch pre-filter checks
        without recording; the authoritative check+record happens under the
        send lock). Returns False when the budget dropped it, with the
        reason published to the activity feed.

        Rules (owned by the 'pushbudget' console card; disabling that card
        turns the budget off):
        - minimum gap between any two transmissions (default 30 s)
        - at most max_per_hour in any rolling hour (default 30)
        - at most max_per_day in any rolling day (default 250)
        Admin replies pass ``exempt=True`` - key-verified admins must stay
        answerable while the bot is being flooded, and their traffic does
        not consume the total budget.
        """
        budget = self.settings.modules.get("pushbudget")
        if exempt or not budget.enabled:
            return True
        now = time.time()
        gap = self._push_number(budget, "gap_seconds", 30.0)
        per_hour = self._push_number(budget, "max_per_hour", 30.0)
        per_day = self._push_number(budget, "max_per_day", 250.0)
        recent = [t for t in self._push_times if t > now - 86400.0]
        self._push_times = recent
        in_hour = sum(1 for t in recent if t > now - 3600.0)
        reason = None
        if gap > 0 and now - self._last_push_at < gap:
            reason = (f"total budget: less than {gap:.0f}s since the "
                      "last transmission")
        elif per_hour > 0 and in_hour >= per_hour:
            reason = (f"total budget: {per_hour:.0f} transmissions in "
                      "the last hour")
        elif per_day > 0 and len(recent) >= per_day:
            reason = (f"total budget: {per_day:.0f} transmissions in "
                      "the last day")
        if reason:
            log.info("budget dropped %s for %s: %s", kind, channel, reason)
            self.feed.publish("dropped", {
                "reason": reason,
                "kind": kind,
                "channel": channel,
                "sender": sender,
                "text": text,
            })
            return False
        if record:
            self._push_times.append(now)
            self._last_push_at = now
        return True

    def person_budget_check(self, identity: str, kind: str, channel: str,
                            text: str, record: bool = True) -> bool:
        """PER-PERSON reply budget: one requester may be answered at most
        person_gap_seconds apart, person_max_per_hour per rolling hour and
        person_max_per_day per rolling day (all owned by the 'pushbudget'
        card; the card off = limits off).

        Keyword replies only - pushes have no person. Returns True to
        proceed; False drops the reply with a feed notice. The router's
        dispatch pre-filter passes record=False; the authoritative
        check+record runs under the send lock.
        """
        budget = self.settings.modules.get("pushbudget")
        if not budget.enabled:
            return True
        now = time.time()
        gap = self._push_number(budget, "person_gap_seconds", 30.0)
        per_hour = self._push_number(budget, "person_max_per_hour", 5.0)
        per_day = self._push_number(budget, "person_max_per_day", 15.0)
        times = [t for t in self._person_times.get(identity, [])
                 if t > now - 86400.0]
        self._person_times[identity] = times
        in_hour = sum(1 for t in times if t > now - 3600.0)
        reason = None
        if gap > 0 and times and now - times[-1] < gap:
            reason = (f"per-person budget: less than {gap:.0f}s since this "
                      "person's last answer")
        elif per_hour > 0 and in_hour >= per_hour:
            reason = (f"per-person budget: {per_hour:.0f} answers for this "
                      "person in the last hour")
        elif per_day > 0 and len(times) >= per_day:
            reason = (f"per-person budget: {per_day:.0f} answers for this "
                      "person in the last day")
        if reason:
            log.info("person budget dropped %s for %s: %s", identity, channel,
                     reason)
            self.feed.publish("dropped", {
                "reason": reason,
                "kind": kind,
                "channel": channel,
                "sender": identity,
                "text": text,
            })
            return False
        if record:
            times.append(now)
        return True

    async def _module_push(self, module_name: str, text: str) -> None:
        """Post one module push to the module's configured channel(s).

        Reads 'push_channels' (list or comma/space string, e.g.
        '#novato, #alert'); a module may also deliver itself and return
        None from pulse() - both patterns are supported.

        Guardrails (both logged to the activity feed):
        - the reserved non-# 'Public' channel is never a push target
        - the push budget: pushes no closer than push_gap_seconds apart,
          at most push_max_per_hour per hour and push_max_per_day per day
          (limits owned by the 'pushbudget' module card; disabled card =
          budget off).
        """
        client = self.client
        if client is None or not client.is_connected:
            log.debug("module %s push skipped: not connected", module_name)
            return
        cfg = self.settings.modules.get(module_name)
        from .modules import parse_channel_list, PUBLIC_CHANNEL_NAMES
        channels = parse_channel_list(cfg.settings.get("push_channels", ""))
        # Config written by hand can bypass console validation - refuse the
        # reserved Public channel here too.
        if any(c.lower() in PUBLIC_CHANNEL_NAMES for c in channels):
            bad = [c for c in channels if c.lower() in PUBLIC_CHANNEL_NAMES]
            self.feed.publish("dropped", {
                "reason": f"push refused: {', '.join(bad)} is not a named "
                          "channel (the bot never posts to 'Public')",
                "kind": "module",
                "channel": module_name,
                "sender": module_name,
                "text": text,
            })
            return
        if not channels:
            return
        # -- total airtime budget (replies + pushes combined)
        if not self.budget_check("push", module_name, module_name, text):
            return
        for channel in channels:
            target = channel.lstrip("#").strip().casefold()
            idx = next((i for i, n in client.channel_names().items()
                        if n.lstrip("#").strip().casefold() == target), None)
            if idx is None:
                log.info("module %s push skipped: channel %s is not configured "
                         "on the companion", module_name, channel)
                continue
            ok = await client.send_channel(idx, text)
            if ok:
                log.info("Module push [%s] -> %s: %s", module_name, channel,
                         text.splitlines()[0] if text else "")

    @staticmethod
    def _push_number(cfg, key: str, default: float) -> float:
        """A module setting as a non-negative float, with fallback.

        An explicit 0 means OFF for that limit (unlike a falsy-value
        fallback, which would make 0 indistinguishable from unset)."""
        raw = cfg.settings.get(key)
        if raw is None or str(raw).strip() == "":
            return default
        try:
            value = float(str(raw))
            return value if value >= 0 else default
        except (TypeError, ValueError):
            return default

    # ------------------------------------------------------------------ channels

    def effective_channel_states(self) -> List[Dict]:
        """Configured channels + dashboard overrides => effective state."""
        states = []
        for channel in self.settings.channels:
            override = self.store.channel_reply_override(channel.name)
            enabled = channel.reply if override is None else override
            states.append({
                "name": channel.name,
                "configured_reply": channel.reply,
                "reply": enabled,
                "override": override,
            })
        return states

    def set_channel_reply(self, channel_name: str, enabled: Optional[bool]) -> Dict:
        """enabled None => forget override (fall back to config)."""
        self.store.set_channel_reply_override(channel_name, enabled)
        state = self.store.channel_reply_override(channel_name)
        self.feed.publish("notice", {
            "text": f"Channel {channel_name} reply {'on' if (state is not False) else 'off'}"
                    + (" (config default)" if state is None else "")
        })
        return {"name": channel_name, "override": state,
                "reply": state if state is not None
                else (self.settings.channel_by_name(channel_name).reply
                      if self.settings.channel_by_name(channel_name) else False)}

    # ------------------------------------------------------------------ status

    def budget_usage(self) -> Dict:
        """How close the bot is to its flood-guardrail caps, for the
        dashboard chip: total transmissions used of the hourly/daily caps,
        plus the busiest single person's usage of the per-person caps.

        The per-person layer only bounds replies, so "top person" is the
        requester with the most answers in the trailing day window.
        """
        budget = self.settings.modules.get("pushbudget")
        now = time.time()
        total_day = [t for t in self._push_times if t > now - 86400.0]
        total_hour = sum(1 for t in total_day if t > now - 3600.0)
        top_person, top_day, top_hour = "", 0, 0
        for ident, times in self._person_times.items():
            recent = [t for t in times if t > now - 86400.0]
            if len(recent) > top_day:
                top_person, top_day = ident, len(recent)
                top_hour = sum(1 for t in recent if t > now - 3600.0)
        return {
            "on": bool(budget.enabled),
            # total layer (replies + pushes combined)
            "total_hour": total_hour,
            "total_hour_cap": self._push_number(budget, "max_per_hour", 30.0),
            "total_day": len(total_day),
            "total_day_cap": self._push_number(budget, "max_per_day", 250.0),
            "gap": self._push_number(budget, "gap_seconds", 30.0),
            # per-person layer (busiest requester)
            "top_person": top_person,
            "top_hour": top_hour,
            "top_hour_cap": self._push_number(budget, "person_max_per_hour", 5.0),
            "top_day": top_day,
            "top_day_cap": self._push_number(budget, "person_max_per_day", 15.0),
        }

    def status_snapshot(self) -> Dict:
        conn = None
        if self.client is not None:
            conn = {
                "connected": self.client.is_connected,
                "host": self.settings.connection.host,
                "port": self.settings.connection.port,
                "channels_seen": self.client.channel_names() if self.client else {},
            }
        companion = ""
        if self.client is not None:
            companion = (getattr(self.client, "own_name", "") or "").strip(" \x00")
        return {
            "bot_name": "meshtech-bot",
            "companion_name": companion,
            "version": version_stamp(),
            "uptime_seconds": self.uptime_seconds(),
            "config_file": self.settings.config_path,
            "connection": conn,
            "muted": self.store.global_mute(),
            "channels": self.effective_channel_states(),
            "db": self.store.stats_row(),
            "hop_limit": self.settings.mesh.max_inbound_hops,
            "started_at": self.started_at,
            "budget": self.budget_usage(),
        }

    def config_snapshot(self) -> Dict:
        snap = sanitized_snapshot(self.settings)
        # show what the bot actually answers as: the live companion name when
        # the bot learned it, with the configured fallback noted as what it is
        own = (getattr(self.client, "own_name", "") or "").strip(" \x00")
        snap["bot"]["bot_name_effective"] = own or "(not connected)"
        snap["bot"]["bot_name_source"] = (
            "companion" if own
            else ("config fallback" if self.settings.bot.display_name
                  else "default 'me'"))
        return snap
