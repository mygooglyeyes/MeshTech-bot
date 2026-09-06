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

    async def _module_push(self, module_name: str, text: str) -> None:
        """Post one module push to the module's configured channel."""
        client = self.client
        if client is None or not client.is_connected:
            log.debug("module %s push skipped: not connected", module_name)
            return
        cfg = self.settings.modules.get(module_name)
        channel = str(cfg.settings.get("channel", "") or "").strip()
        if not channel:
            return
        target = channel.lstrip("#").strip().casefold()
        idx = next((i for i, n in client.channel_names().items()
                    if n.lstrip("#").strip().casefold() == target), None)
        if idx is None:
            log.info("module %s push skipped: channel %s is not configured "
                     "on the companion", module_name, channel)
            return
        ok = await client.send_channel(idx, text)
        if ok:
            log.info("Module push [%s] -> %s: %s", module_name, channel,
                     text.splitlines()[0] if text else "")

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

    def status_snapshot(self) -> Dict:
        conn = None
        if self.client is not None:
            conn = {
                "connected": self.client.is_connected,
                "host": self.settings.connection.host,
                "port": self.settings.connection.port,
                "channels_seen": self.client.channel_names() if self.client else {},
            }
        return {
            "bot_name": "meshtech-bot",
            "version": version_stamp(),
            "uptime_seconds": self.uptime_seconds(),
            "config_file": self.settings.config_path,
            "connection": conn,
            "muted": self.store.global_mute(),
            "channels": self.effective_channel_states(),
            "db": self.store.stats_row(),
            "hop_limit": self.settings.mesh.max_inbound_hops,
            "started_at": self.started_at,
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
