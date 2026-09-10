"""MCP - Master Control Program: the bot's own radio.

The bot OWNS the PiMesh-1W v2 (an SX1262 radio on the Pi's SPI pins).
Nothing else touches the radio:

    antenna -> PiMesh v2 (SPI) -> MCP (this module)
                                    |-> every RX packet is handed to
                                    |   the bot's own handlers (decode,
                                    |   replies) via an inbound callback
                                    |-> every RX packet is also pushed to
                                    |   meshtech-modem's feed port, so
                                    |   openHop's log stays complete
                                    |-> every TX packet is looped back to
                                    |   the modem too (a radio never hears
                                    |   itself)

openHop is observer-only: it talks to the modem (port 5055), never to
the radio. The radio driver is openhop_core's proven SX1262Radio with
the PiMesh-1W v2 pin profile (from openHop's own radio-settings.json -
no guessed pins).

This module is defensive by construction, like the rest of the bot:
radio problems must never crash the bot or stall its messaging.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Awaitable, Callable, Optional

log = logging.getLogger("meshtech-bot.mcp")

# ---------------------------------------------------------------------------
# PiMesh-1W v2 pin profile (hard-coded by design decision, 2026-09-08).
# Source: openhop_repeater/radio-settings.json "pimesh-1w-v2" - the exact
# values openHop itself ships for this board. Flexible config is a FUTURE
# feature (TODOS.md); until then these numbers must not be edited casually.
# ---------------------------------------------------------------------------
PIMESH_1W_V2 = {
    "bus_id": 0,
    "cs_id": 0,
    "cs_pin": -1,          # hardware CS
    "reset_pin": 18,
    "busy_pin": 5,
    "irq_pin": 6,
    "txen_pin": -1,
    "rxen_pin": -1,
    "en_pin": 26,
    "use_dio3_tcxo": True,
    "use_dio2_rf": True,
    "preamble_length": 32,
}

MAX_LORA_PAYLOAD = 255

# MeshCore payload types (openhop_core protocol/constants.py values).
PAYLOAD_TYPE_NAMES = {
    0x00: "TXT_MSG", 0x01: "RESPONSE", 0x02: "ACK", 0x03: "ADVERT",
    0x04: "GRP_TXT", 0x05: "GRP_DATA", 0x06: "ANON_REQ", 0x07: "PATH",
    0x08: "TRACE", 0x0A: "MULTIPART", 0x0B: "CONTROL",
}
ROUTE_TYPE_NAMES = {
    0: "transport-flood", 1: "flood", 2: "direct", 3: "transport-direct",
}


def parse_envelope(data: bytes) -> dict:
    """Best-effort MeshCore packet header parse - no crypto, no imports.

    Mirrors pymc_core Packet.read_from()'s wire layout (header 1B |
    [transport codes 4B] | path_len 1B | path | payload). Enough to say
    WHAT a heard packet is (type, routing, hops) - never WHAT IT SAYS:
    payload text is encrypted with keys the bot deliberately does not
    hold (channel keys live in the web console / config files, hard rule).
    """
    info = {"payload_type": None, "route": None, "version": None,
            "hops": None, "hash_size": None}
    if not data:
        return info
    header = data[0]
    route = header & 0x03
    info["route"] = ROUTE_TYPE_NAMES.get(route, route)
    info["payload_type"] = (header >> 2) & 0x0F
    info["version"] = (header >> 6) & 0x03
    idx = 1
    if route in (0, 3):          # transport codes present on the wire
        idx += 4
    if idx >= len(data):
        return info
    path_len = data[idx]
    info["hash_size"] = (path_len >> 6) + 1
    info["hops"] = path_len & 0x3F
    return info

# Feed push wire format (verified against meshtech-modem modem.py,
# FeedClient.run): 0x01 | rssi (1B, signed) | snr_x10 (1B, signed) |
# signal_rssi (1B, signed) | length (2B LE) | raw bytes. TCP carries the
# integrity; no CRC on the feed wire.
FEED_HEADER = 0x01


def encode_feed_push(rssi: int, snr: float, signal_rssi: int, data: bytes) -> bytes:
    """One feed push frame, exactly what the modem's FeedClient parses."""
    length = len(data)
    if length > MAX_LORA_PAYLOAD:
        raise ValueError(f"radio payload too big for the feed: {length}B")
    return (
        bytes((
            FEED_HEADER,
            rssi & 0xFF,
            int(round(snr * 10)) & 0xFF,
            signal_rssi & 0xFF,
        ))
        + length.to_bytes(2, "little")
        + data
    )


class McpStats:
    """Counters the dashboard chip shows (pushed/dropped/heard/sent)."""

    def __init__(self) -> None:
        self.rx_count = 0
        self.tx_count = 0
        self.pushed = 0
        self.dropped = 0


class Mcp:
    """The radio owner: one driver instance, RX split, TX loopback.

    Constructed only when ``mcp.enabled`` is true. ``start()`` brings the
    radio up and begins feeding packets to both consumers. Every failure
    path logs and retries - never crashes the bot.
    """

    def __init__(self, service, inbound_handler: Callable[[bytes], Awaitable[None]],
                 push_queue: "asyncio.Queue[tuple[int, float, int, bytes]]"):
        self.service = service
        self.settings = service.settings
        self._inbound = inbound_handler          # bot's own RX path
        self._push_queue = push_queue            # modem feed's RX path
        self.radio = None                        # SX1262Radio once up
        self.stats = McpStats()
        self.is_running = False
        self._task: Optional[asyncio.Task] = None
        self._loop: Optional[asyncio.EventLoop] = None

    # ------------------------------------------------------------------ radio

    def _radio_kwargs(self) -> dict:
        """Driver arguments: pins from the board profile, settings from config."""
        mcp = self.settings.mcp
        import struct
        return {
            **PIMESH_1W_V2,
            "frequency": mcp.frequency_hz,
            "tx_power": mcp.tx_power_dbm,
            "spreading_factor": mcp.spreading_factor,
            "bandwidth": int(mcp.bandwidth_khz * 1000),
            "coding_rate": mcp.coding_rate_index + 4,  # config index 1 -> CR4/5
        }

    async def start(self) -> bool:
        """Bring the radio up; keep retrying quietly until it works."""
        self._loop = asyncio.get_running_loop()
        while not self.service.stop_requested:
            try:
                await self._radio_up()
                self.is_running = True
                return True
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.error("Radio init failed: %s - retrying in 30s", exc)
                try:
                    await asyncio.sleep(30)
                except asyncio.CancelledError:
                    raise
        return False

    async def _radio_up(self) -> None:
        from pymc_core.hardware.sx1262_wrapper import SX1262Radio

        kwargs = self._radio_kwargs()
        log.info("Radio init: PiMesh-1W v2 profile, %.3fMHz SF%d %gkHz %ddBm",
                 kwargs["frequency"] / 1e6, kwargs["spreading_factor"],
                 kwargs["bandwidth"] / 1000, kwargs["tx_power"])
        radio = SX1262Radio(**kwargs)
        ok = await self._loop.run_in_executor(None, radio.begin)
        if not ok:
            raise RuntimeError("radio.begin() returned False")
        radio.set_rx_callback(self._on_radio_rx)
        self.radio = radio
        log.info("Radio up - the MCP owns the air.")

    # ------------------------------------------------------------- radio RX

    def _on_radio_rx(self, data: bytes) -> None:
        """Called from the driver's IRQ background task for every packet."""
        if not data:
            return
        self.stats.rx_count += 1
        rssi = self.radio.last_rssi if self.radio else -100
        snr = self.radio.last_snr if self.radio else 0.0
        signal_rssi = self.radio.last_signal_rssi if self.radio else -100
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._split, rssi, snr, signal_rssi, data)

    def _split(self, rssi: int, snr: float, signal_rssi: int, data: bytes) -> None:
        """Fan one packet out to both consumers - neither can starve the other."""
        # 1) the modem feed (bounded queue; drops when full, by design)
        try:
            self._push_queue.put_nowait((rssi, snr, signal_rssi, data))
        except asyncio.QueueFull:
            self.stats.dropped += 1
            log.debug("Feed queue full - packet not pushed (radio RX unaffected)")
        # 2) the bot's own records: raw capture + the packet log, so the
        #    dashboard shows everything the radio hears (bench-test fix:
        #    both stayed empty before because only the companion path fed
        #    them).
        try:
            self._record_packet(rssi, snr, signal_rssi, data)
        except Exception as exc:
            log.debug("packet record failed: %s", exc)
        # 3) decoded delivery to the message pipeline is NOT wired yet:
        #    router.on_inbound expects decrypted text, and decrypting needs
        #    the channel keys - which live in config/console by design
        #    (hard rule: no channel handling in bot code). Design decision
        #    pending with Brett (TODOS.md). Envelope info lands in the
        #    packet log above; no more 'bytes has no attribute kind' spam.

    def _record_packet(self, rssi: int, snr: float, signal_rssi: int,
                       data: bytes) -> None:
        """One heard packet into the packet log: raw row + decoded envelope."""
        capture = getattr(self.service, "capture", None)
        if capture is None:
            return
        ts = time.time()
        try:
            capture.record_raw(ts, data)
        except Exception as exc:
            log.debug("raw capture failed: %s", exc)
        try:
            info = parse_envelope(data)
            type_name = PAYLOAD_TYPE_NAMES.get(info.get("payload_type"),
                                               "UNKNOWN")
            capture.record_event(
                ts, type_name,
                {"hops": info.get("hops"), "snr": snr},
                attributes={"route": info.get("route"),
                            "rssi": rssi, "signal_rssi": signal_rssi,
                            "size": len(data),
                            "hash_size": info.get("hash_size"),
                            "radio": "mcp-spi",
                            "note": "envelope only - payload encrypted"},
            )
        except Exception as exc:
            log.debug("envelope record failed: %s", exc)

    # ------------------------------------------------------------- radio TX

    async def send(self, data: bytes) -> bool:
        """Transmit over the real radio, then loop back to the modem feed."""
        if not self.is_running or self.radio is None:
            log.warning("Radio not up - dropping TX (%dB).", len(data) if data else 0)
            return False
        try:
            await self.radio.send(data)
        except Exception as exc:
            log.error("TX failed: %s", exc)
            return False
        self.stats.tx_count += 1
        # TX loopback: the modem re-broadcasts this so openHop sees bot
        # traffic. Synthetic metadata like the modem's own design expects.
        try:
            self._push_queue.put_nowait((-100, 0.0, -100, bytes(data)))
        except asyncio.QueueFull:
            self.stats.dropped += 1
        return True

    # -------------------------------------------------------------- shutdown

    async def stop(self) -> None:
        self.is_running = False
        radio, self.radio = self.radio, None
        if radio is not None:
            try:
                await self._loop.run_in_executor(None, radio.cleanup)
            except Exception as exc:
                log.warning("Radio cleanup: %s", exc)
        log.info("Radio released.")
