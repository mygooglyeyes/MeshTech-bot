"""Modem feed - push every radio packet to meshtech-modem's feed port.

The modem (a separate program on the same box) re-broadcasts what it is
given to openHop, whose packet log and MQTT stream stay complete. The
feed port is localhost-only and password-protected; the password lives
in its own mode-600 file (never in config.yaml).

Wire format verified against meshtech-modem/modem.py (FeedClient.run):
- first frame after connect: the token, as raw bytes
  - modem answers 0x01 = accepted (v0.0.012+), 0x00 or close = rejected
- then each push: 0x01 | rssi(1B signed) | snr_x10(1B signed) |
  signal_rssi(1B signed) | length(2B LE) | raw bytes

Failure is designed in: modem down or wrong password means logging and
a slow retry - the bot's messaging is never affected. Nothing here
holds the radio; the MCP owns it and calls push().
"""
from __future__ import annotations

import asyncio
import logging
import socket
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger("meshtech-bot.modemfeed")

CONNECT_TIMEOUT = 5.0
PUSH_TIMEOUT = 10.0
RETRY_MIN_SECONDS = 2.0
RETRY_MAX_SECONDS = 30.0
MAX_PUSH_BYTES = 65536  # same guard the modem's feed reader uses


class FeedStats:
    """Dashboard chip counters."""

    def __init__(self) -> None:
        self.pushed = 0
        self.dropped = 0
        self.connected_since: Optional[float] = None


class ModemFeed:
    """Keeps one authenticated connection to the modem's feed port alive."""

    def __init__(self, service, queue: "asyncio.Queue[tuple[int, float, int, bytes]]"):
        self.service = service
        self.settings = service.settings
        self.queue = queue
        self.stats = FeedStats()
        self.connected = False
        self._stop = asyncio.Event()

    # ------------------------------------------------------------------ token

    def _token(self) -> str:
        """Feed password from its protected file - never from config.yaml."""
        path = self.settings.modem_feed.token_file
        if not path:
            return ""
        try:
            with open(path, "r", encoding="utf-8") as handle:
                return handle.readline().strip()
        except OSError:
            return ""

    # -------------------------------------------------------------- lifecycle

    async def run(self) -> None:
        cfg = self.settings.modem_feed
        if not cfg.enabled:
            return
        token = self._token()
        if not token:
            log.warning("Modem feed enabled but no token file readable (%s) - "
                        "feed disabled. Create it with the modem's "
                        "set-feed-token.sh, then copy it for the bot.",
                        cfg.token_file)
            return

        delay = RETRY_MIN_SECONDS
        warned_auth = False
        while not self.service.stop_requested and not self._stop.is_set():
            try:
                authed = await self._connect_once(token)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                authed = False
                log.debug("Feed connect: %s", exc)
            if authed:
                delay = RETRY_MIN_SECONDS
                warned_auth = False
                await self._pump()
            else:
                if self.service.stop_requested:
                    break
                if not warned_auth:
                    log.info("Modem feed not connected - will retry every %gs.",
                             delay)
                    warned_auth = True
                try:
                    await asyncio.sleep(delay)
                except asyncio.CancelledError:
                    raise
                delay = min(delay * 2, RETRY_MAX_SECONDS)
        log.info("Modem feed stopped.")

    async def _connect_once(self, token: str) -> bool:
        """One connect+auth attempt. True only when the modem said yes."""
        cfg = self.settings.modem_feed
        log.info("Modem feed connecting to %s:%s ...", cfg.host, cfg.port)
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(cfg.host, cfg.port), CONNECT_TIMEOUT)
        try:
            writer.write(token.encode("utf-8"))
            await asyncio.wait_for(writer.drain(), CONNECT_TIMEOUT)
            answer = await asyncio.wait_for(reader.readexactly(1), CONNECT_TIMEOUT)
        except Exception:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            raise
        if answer != b"\x01":
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            log.error("Modem feed REJECTED - wrong password? Check the bot's "
                      "%s against the modem's .feed_token.", cfg.token_file)
            return False
        self._writer = writer
        self.connected = True
        # TCP keepalive: if the modem dies without a clean close, the OS
        # probes the dead peer instead of letting writes buffer forever.
        try:
            sock = writer.get_extra_info("socket")
            if sock is not None:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 30)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 10)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 3)
        except OSError as exc:
            log.debug("keepalive not set: %s", exc)
        self.stats.connected_since = time.time()
        log.info("Modem feed connected (%s:%s)", cfg.host, cfg.port)
        self.service.feed.publish("modem_feed_up", {"host": cfg.host,
                                                    "port": cfg.port})
        return True

    async def _pump(self) -> None:
        """Send queued pushes until the connection drops.

        A DEAD LINK CANNOT BE SEEN BY WAITING ALONE: queue.get() sleeps
        until the next packet, and on a quiet mesh that can be minutes -
        during which a dropped connection goes unnoticed (seen live
        2026-09-10: 18 dark minutes, chip said "live" the whole time).
        Two guards fix that:
        - wait_for a 120 s cap on each queue wait -> after two quiet
          minutes we probe the link and re-dial if it is gone;
        - TCP keepalive on the socket -> a truly dead peer errors on
          the next write instead of silently buffering.
        """
        writer = self._writer
        idle_cap = 120.0
        try:
            while not self.service.stop_requested and not self._stop.is_set():
                # Slice the idle wait into short steps so a closing
                # connection is noticed within seconds, not after the
                # whole cap (also keeps tests off real-time waits).
                waited = 0.0
                item = None
                while waited < idle_cap:
                    try:
                        item = await asyncio.wait_for(self.queue.get(),
                                                      min(2.0, idle_cap - waited))
                        break
                    except asyncio.TimeoutError:
                        waited += 2.0
                        if writer.is_closing():
                            return
                if item is None:
                    if writer.is_closing():
                        break
                    continue               # quiet mesh; link still open
                rssi, snr, signal_rssi, data = item
                from core.mcp import encode_feed_push
                frame = encode_feed_push(rssi, snr, signal_rssi, data)
                writer.write(frame)
                await asyncio.wait_for(writer.drain(), PUSH_TIMEOUT)
                self.stats.pushed += 1
        except asyncio.TimeoutError:
            log.info("Modem feed stalled - reconnecting.")
        except (ConnectionResetError, BrokenPipeError, OSError):
            log.info("Modem feed down (connection lost).")
        except asyncio.CancelledError:
            raise
        finally:
            self.connected = False
            self.stats.connected_since = None
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
            self.service.feed.publish("modem_feed_down", {})

    def stop(self) -> None:
        self._stop.set()
