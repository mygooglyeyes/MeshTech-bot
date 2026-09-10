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
import os
import struct
import time
from pathlib import Path
from typing import Awaitable, Callable, Optional

from .models import InboundMessage

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
PAYLOAD_TYPE_TXT_MSG = 0x00
PAYLOAD_TYPE_ADVERT = 0x03
PAYLOAD_TYPE_GRP_TXT = 0x04
ROUTE_TYPE_NAMES = {
    0: "transport-flood", 1: "flood", 2: "direct", 3: "transport-direct",
}


def _split_sender(content: str) -> tuple[Optional[str], str]:
    """'Name: body' -> (name, body) per the MeshCore group-text format.

    Same rule as core/models.split_channel_text, kept local so the MCP
    module never imports the models layer (no radio needed to test).
    """
    text = (content or "").strip()
    idx = text.find(": ")
    if 0 < idx <= 64:
        name = text[:idx].strip()
        if name and "\n" not in name and "\r" not in name:
            return name, text[idx + 2:].strip()
    return None, text


def _hops_from_packet(pkt) -> Optional[int]:
    """Hop count from the wire: 0 for direct routes, the encoded count
    for flood routes (None when it cannot be trusted)."""
    try:
        route = pkt.get_route_type()
        if route in (2, 3):                  # direct / transport-direct
            return 0
        count = pkt.get_path_hash_count()
        return count if 0 <= count <= 64 else None
    except Exception:
        return None


def _log_line(text: str, limit: int = 80) -> str:
    """One safe log line from attacker-controlled radio text."""
    return " ".join((text or "").split())[:limit]


# ---------------------------------------------------------------------------
# Advert name (the radio's user-chosen ID).
#
# The advert appdata holds MAX_ADVERT_DATA_SIZE = 96 bytes; the name is what
# remains after the flags byte (we set no location/features). Phones cap
# contact names around 32 chars, so stay inside both limits. Names are UTF-8
# - emojis welcome - and we cut at CHARACTER boundaries only: a cut inside a
# multi-byte emoji makes the whole name invalid UTF-8 and receivers drop it.
# ---------------------------------------------------------------------------
MAX_ADVERT_NAME_CHARS = 32
MAX_ADVERT_NAME_BYTES = 90


def sanitize_advert_name(name: str,
                         max_chars: int = MAX_ADVERT_NAME_CHARS,
                         max_bytes: int = MAX_ADVERT_NAME_BYTES) -> str:
    """The user's chosen radio name, made safe for the advert payload.

    Collapses whitespace/control characters (newlines would forge log
    lines), then fits the name within max_chars characters AND max_bytes
    of UTF-8, cutting only at character boundaries so emojis survive.
    Empty input stays empty - callers substitute their fallback.
    """
    cleaned = " ".join((name or "").split())
    if not cleaned:
        return ""
    out: list[str] = []
    used_bytes = 0
    used_chars = 0
    for ch in cleaned:
        ch_bytes = len(ch.encode("utf-8"))
        if used_chars >= max_chars or used_bytes + ch_bytes > max_bytes:
            break
        out.append(ch)
        used_chars += 1
        used_bytes += ch_bytes
    return "".join(out).strip()


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
        self.decoded = 0
        self.decrypt_fail = 0


# ---------------------------------------------------------------------------
# Identity file - the bot's OWN radio identity (Brett's decision 2026-09-09:
# full decode incl. DMs). One line of 64 hex chars = the MeshCore firmware
# key format ([32-byte scalar][32-byte nonce]); loadable by openHop tools
# too. Mode 600, lives in data/ next to the database. Never logged.
# ---------------------------------------------------------------------------
IDENTITY_FILE = "data/bot_radio_identity.txt"


def load_local_identity(identity_path: str = IDENTITY_FILE):
    """The bot's own LocalIdentity, created on first use.

    Returns None when pynacl/pymc_core is unavailable - decode then stays
    envelope-only and the bot never crashes over it.
    """
    try:
        from pymc_core.protocol.identity import LocalIdentity
    except Exception as exc:                     # pragma: no cover
        log.warning("Radio identity unavailable (pymc_core import failed: %s) "
                    "- packets stay envelope-only.", exc)
        return None
    path = Path(identity_path)
    seed = None
    if path.is_file():
        try:
            hex_str = path.read_text(encoding="utf-8").strip().splitlines()[0]
            raw = bytes.fromhex(hex_str)
            if len(raw) not in (32, 64):
                raise ValueError(f"expected 32 or 64 bytes, got {len(raw)}")
            seed = raw
        except Exception as exc:
            log.error("Radio identity file %s is unusable (%s) - a NEW key "
                      "would change the bot's mesh address. NOT generating; "
                      "fix or remove the file, then restart.", identity_path, exc)
            return None
    if seed is None:
        seed = os.urandom(64)                    # firmware-format key
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(seed.hex() + "\n")
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
            log.info("New radio identity created at %s - the bot's mesh "
                     "address is now fixed forever (back this file up).", path)
        except OSError as exc:
            log.error("Could not write radio identity file %s (%s) - "
                      "running WITHOUT a persistent key; DMs will not work "
                      "until this is fixed.", path, exc)
            return None
    try:
        identity = LocalIdentity(seed)
        log.info("Radio identity loaded: %s...",
                 identity.get_public_key().hex()[:16])
        return identity
    except Exception as exc:                     # pragma: no cover
        log.error("Radio identity could not be loaded (%s) - packets stay "
                  "envelope-only.", exc)
        return None


def _secret_bytes(secret: str) -> bytes:
    """Channel secret text -> key bytes, matching openHop's GroupTextHandler:
    hex when it parses as hex, utf-8 otherwise; padded/truncated to 32."""
    try:
        raw = bytes.fromhex(secret)
    except ValueError:
        raw = secret.encode("utf-8")
    if len(raw) > 32:
        raw = raw[:32]                    # openHop truncates oversize keys
    return raw                            # padding happens in derive_channel_keys


def derive_channel_keys(secret: str) -> tuple[int, bytes, bytes]:
    """(channel_hash, aes_key, hmac_key) - same derivation as the firmware
    and openHop's GroupTextHandler._derive_channel_keys."""
    import hashlib

    raw = _secret_bytes(secret)
    # MeshCore firmware convention (openHop _secret_bytes_for_hash): a 32-byte
    # secret whose SECOND HALF is all zeros is really a 128-bit key - hash
    # only the first 16 bytes. Checked on the ORIGINAL secret bytes, before
    # any padding, so a short text secret never falsely triggers it.
    if len(raw) >= 32 and raw[16:32] == b"\x00" * 16:
        raw = raw[:16]
    master = hashlib.sha256(raw).digest()
    return master[0], master[:16], master[16:32]


def decode_channel_payload(aes_key: bytes, hmac_key: bytes,
                           mac: bytes, ciphertext: bytes) -> Optional[bytes]:
    """Decrypt one GRP_TXT payload body; None when the HMAC does not match
    (normal for foreign channels - hash is only one byte)."""
    from pymc_core.protocol.crypto import CryptoUtils

    expected = CryptoUtils._hmac_sha256(hmac_key, ciphertext)[:2]
    if mac != expected:
        return None
    return CryptoUtils._aes_decrypt(aes_key, ciphertext)


def parse_channel_plaintext(plaintext: bytes) -> tuple[int, int, str]:
    """timestamp(4 LE), flags(1), text - openHop GroupTextHandler layout."""
    if len(plaintext) < 5:
        return 0, 0, ""
    timestamp = int.from_bytes(plaintext[:4], "little")
    flags = plaintext[4]
    raw = plaintext[5:].decode("utf-8", "replace").rstrip("\x00")
    return timestamp, flags, raw


def parse_dm_plaintext(decrypted: bytes) -> tuple[int, int, int, str]:
    """timestamp(4 LE), flags, attempt/txt_type split, text - openHop
    TextMessageHandler layout."""
    if len(decrypted) < 5:
        return 0, 0, 0, ""
    timestamp = int.from_bytes(decrypted[:4], "little")
    flags = decrypted[4]
    return timestamp, flags, flags & 0x03, decrypted[5:].decode("utf-8", "replace").rstrip("\x00")


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
        # --- decode machinery (Brett's "full decode" decision) ----------
        self.identity = load_local_identity()
        self._channels_by_hash: dict[int, list[dict]] = {}
        self._rebuild_channel_table()
        # Recent packet hashes: MeshCore flood dedup (a flood heard over
        # several paths would otherwise hit the router once per hearing).
        self._seen_hashes: dict[str, float] = {}
        # Own-packet echo guard: the bot's src hash, plus a short window of
        # hashes of packets we just sent (the radio may hear our repeats).
        self._own_hash = (self.identity.get_public_key()[0]
                          if self.identity else None)
        self._recent_tx_hashes: dict[str, float] = {}
        # config channel name -> index in settings.channels (the slot number
        # the router uses as channel_idx for replies)
        self._channel_index: dict[str, int] = {}

    # ------------------------------------------------------------------ radio

    def _radio_kwargs(self) -> dict:
        """Driver arguments: pins from the board profile, settings from config."""
        mcp = self.settings.mcp
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
                await self._announce_self()
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

    async def _announce_self(self) -> None:
        """Send the bot's signed advert once the radio is up (when
        bot.advertise_on_start, the same switch companion mode uses).

        Without it nobody learns the bot's new radio address, so DMs to it
        could never be encrypted for us.
        """
        bot_cfg = getattr(self.settings, "bot", None)
        if bot_cfg is None or not bot_cfg.advertise_on_start:
            return
        if self.identity is None:
            return
        try:
            from pymc_core.protocol.packet_builder import PacketBuilder
            advert_name = (sanitize_advert_name(bot_cfg.display_name)
                           or "bot")
            pkt = PacketBuilder.create_self_advert(
                self.identity, advert_name, route_type="flood")
            await asyncio.sleep(2.0)            # let the driver settle
            if await self.send(pkt.write_to()):
                log.info("Self-advert sent - the bot is on the mesh as '%s'.",
                         advert_name)
        except Exception as exc:
            log.warning("Self-advert failed (non-fatal): %s", exc)

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

    # ------------------------------------------------------------- radio

    def _radio_kwargs(self) -> dict:
        """Driver arguments: pins from the board profile, settings from config."""
        mcp = self.settings.mcp
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
        # 2) the bot's own records + decode: raw capture, envelope log,
        #    then full decode (adverts / channel text / DMs) delivered to
        #    the message pipeline. Bench-test note: before this block the
        #    radio path fed only the packet log and raw capture - the
        #    dashboard's message/packet views stayed empty and the router
        #    got nothing to answer.
        try:
            self._ingest(rssi, snr, signal_rssi, data)
        except Exception as exc:
            log.debug("packet record failed: %s", exc)

    def _ingest(self, rssi: int, snr: float, signal_rssi: int, data: bytes) -> None:
        """One heard packet: record + decode + hand to the message pipeline."""
        self._record_packet(rssi, snr, signal_rssi, data)
        self._decode_and_deliver(rssi, snr, data)

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

    # ---------------------------------------------------------------- decode

    def _rebuild_channel_table(self) -> None:
        """Hash -> channel(s) map from the config's own channel list.

        The keys come from config.yaml (web console / config files own
        them, hard rule) - the bot just derives the SAME keys it already
        held in companion mode. The 1-byte channel hash means collisions
        between channels are expected; each candidate is tried (exactly
        what the firmware and openHop do).
        """
        table: dict[int, list[dict]] = {}
        index: dict[str, int] = {}
        for cfg_channel in getattr(self.settings, "channels", []) or []:
            if not cfg_channel.name:
                continue
            index[cfg_channel.name] = len(index)
            # Default secret matches the bot's own companion behaviour and
            # the phone apps: hashtag channels use the WELL-KNOWN derived
            # key sha256("#name") - the '#' matters (see core/client.py).
            secret = cfg_channel.secret_hex or cfg_channel.name
            try:
                channel_hash, aes_key, hmac_key = derive_channel_keys(secret)
            except Exception:
                continue
            table.setdefault(channel_hash, []).append({
                "name": cfg_channel.name, "secret": secret,
                "aes": aes_key, "hmac": hmac_key,
                "reply": bool(getattr(cfg_channel, "reply", True)),
            })
        self._channels_by_hash = table
        self._channel_index = index
        if table:
            log.info("Channel decode table: %d channel(s) keyed", len(table))

    @staticmethod
    def _packet_hash_hex(pkt) -> str:
        """MeshCore-style dedup hash of the PAYLOAD bytes (truncated hex).

        Mirrors PacketHashingUtils.calculate_packet_hash (payload_type byte
        + payload) without importing pymc_core, so dedup still works when
        the driver import fails.
        """
        try:
            import hashlib
            payload = pkt.get_payload()
            digest = hashlib.sha256()
            digest.update(bytes([pkt.get_payload_type() & 0xFF]))
            digest.update(payload)
            return digest.hexdigest()[:16]
        except Exception:
            return ""

    def _is_duplicate(self, pkt) -> bool:
        """MeshCore-style flood dedup (payload hash, 45 s window)."""
        key = self._packet_hash_hex(pkt)
        if not key:
            return False
        now = time.time()
        # one dict holds both memories; prune as we go
        for store, window in ((self._seen_hashes, 45.0),
                              (self._recent_tx_hashes, 120.0)):
            stale = [k for k, ts in store.items() if now - ts > window]
            for k in stale:
                store.pop(k, None)
        if key in self._recent_tx_hashes:
            return True                    # our own TX heard again (repeat)
        if key in self._seen_hashes:
            return True                    # flood repeat
        self._seen_hashes[key] = now
        return False

    def _decode_and_deliver(self, rssi: int, snr: float, data: bytes) -> None:
        """Parse + decrypt one packet and hand it to the bot's pipeline.

        Every failure path logs quietly and returns - a malformed or
        foreign packet must never break the receive loop.
        """
        if self.identity is None:
            return                          # envelope-only mode (logged at start)
        try:
            from pymc_core.protocol.packet import Packet
        except Exception:
            return
        pkt = Packet()
        try:
            pkt.read_from(data)
        except Exception as exc:
            log.debug("Malformed packet skipped: %s", exc)
            return
        payload_type = pkt.get_payload_type()
        self.stats.decoded += 1

        if self._own_hash is not None and len(pkt.payload) >= 2:
            src_hash = pkt.payload[1]
        else:
            src_hash = None

        # Adverts: verify signature, learn name/position, dedup, route on.
        if payload_type == PAYLOAD_TYPE_ADVERT:
            self._handle_advert(pkt, rssi, snr)
            return
        # Only the two text types carry something the router can act on.
        if payload_type not in (PAYLOAD_TYPE_TXT_MSG, PAYLOAD_TYPE_GRP_TXT):
            return
        if self._is_duplicate(pkt):
            return
        if src_hash == self._own_hash:
            log.debug("Own packet heard back - ignoring.")
            return
        if payload_type == PAYLOAD_TYPE_GRP_TXT:
            self._handle_group_text(pkt, rssi, snr)
        else:
            self._handle_text_message(pkt, rssi, snr)

    # -- adverts ---------------------------------------------------------

    def _handle_advert(self, pkt, rssi: int, snr: float) -> None:
        try:
            from pymc_core.protocol.utils import parse_advert_payload, decode_appdata
        except Exception:
            return
        payload = pkt.get_payload()
        try:
            parsed = parse_advert_payload(payload)
            pubkey_hex = parsed["pubkey"]
            timestamp = parsed["timestamp"]
            signature = bytes.fromhex(parsed["signature"])
            appdata = parsed["appdata"]
        except Exception as exc:
            log.debug("advert parse failed: %s", exc)
            return
        # Signature first: an advert is a signed claim of identity.
        try:
            from pymc_core.protocol.identity import Identity
            peer = Identity(bytes.fromhex(pubkey_hex))
            ts_bytes = struct.pack("<I", timestamp)
            if not peer.verify(bytes.fromhex(pubkey_hex) + ts_bytes + appdata,
                               signature):
                log.debug("advert signature invalid - ignored")
                return
        except Exception as exc:
            log.debug("advert verify failed: %s", exc)
            return
        decoded = decode_appdata(appdata)
        name = decoded.get("node_name") or decoded.get("name") or ""
        if not name:
            return
        now = time.time()
        key = f"advert:{pubkey_hex[:24]}:{name}"
        if key in self._seen_hashes and now - self._seen_hashes[key] < 45:
            return
        self._seen_hashes[key] = now
        self.service.store.upsert_node(
            pubkey=pubkey_hex, name=name,
            snr=snr,
            lat=decoded.get("latitude") or decoded.get("lat") or None,
            lon=decoded.get("longitude") or decoded.get("lon") or None,
            source="advert", ts=now)
        log.info("ADVERT %s (%s) rssi=%d snr=%.1f", name,
                 pubkey_hex[:12], rssi, snr)

    # -- channel text -----------------------------------------------------

    def _handle_group_text(self, pkt, rssi: int, snr: float) -> None:
        payload = pkt.get_payload()
        if len(payload) < 4:
            return
        channel_hash = payload[0]
        mac = payload[1:3]
        ciphertext = payload[3:]
        for channel in self._channels_by_hash.get(channel_hash, []):
            try:
                plaintext = decode_channel_payload(
                    channel["aes"], channel["hmac"], mac, ciphertext)
            except Exception:
                continue
            if plaintext is None:
                continue                 # HMAC mismatch - next candidate
            timestamp, _flags, content = parse_channel_plaintext(plaintext)
            sender_name, body = _split_sender(content)
            msg = InboundMessage(
                kind="channel", text=body,
                channel_name=channel["name"],
                channel_idx=self._channel_index.get(channel["name"]),
                sender_ts=float(timestamp) if timestamp else None,
                hops=_hops_from_packet(pkt), snr=snr)
            msg.sender_name = sender_name
            self._deliver("GRP_TXT", channel["name"], sender_name, msg)
            return                       # first validating candidate wins
        log.debug("GRP_TXT hash %02X: no channel key matched", channel_hash)

    # -- direct messages ---------------------------------------------------

    def _handle_text_message(self, pkt, rssi: int, snr: float) -> None:
        payload = pkt.get_payload()
        if len(payload) < 3 or self.identity is None:
            return
        src_hash = pkt.payload[1]
        body = payload[2:]               # skip dest_hash + src_hash
        for candidate in self._contact_candidates(src_hash):
            try:
                from pymc_core.protocol.crypto import CryptoUtils
                from pymc_core.protocol.identity import Identity
                peer = Identity(bytes.fromhex(candidate["pubkey"]))
                shared = peer.calc_shared_secret(
                    self.identity.get_private_key())
                decrypted = CryptoUtils.mac_then_decrypt(
                    shared[:16], shared, body)
            except Exception:
                continue                 # wrong key - try next candidate
            if decrypted is None:
                continue
            timestamp, flags, _attempt, text = parse_dm_plaintext(decrypted)
            txt_type = (flags >> 2) & 0x3F
            if txt_type != 0:
                return                   # CLI data / control - not chat
            prefix = candidate["pubkey"][:12].lower()
            msg = InboundMessage(
                kind="dm", text=text, sender_prefix=prefix,
                sender_ts=float(timestamp) if timestamp else None,
                hops=_hops_from_packet(pkt), snr=snr)
            self._deliver("TXT_MSG", prefix, prefix, msg)
            return
        self.stats.decrypt_fail += 1
        log.debug("TXT_MSG src %02X: no contact key matched", src_hash)

    def _contact_candidates(self, src_hash: int) -> list[dict]:
        """Known nodes whose pubkey starts with the sender's hash byte."""
        matches = []
        for node in self.service.store.list_nodes(limit=2000):
            pubkey = node.get("pubkey") or ""
            if len(pubkey) == 64:
                try:
                    if bytes.fromhex(pubkey)[0] == src_hash:
                        matches.append({"pubkey": pubkey})
                except ValueError:
                    continue
        return matches

    def _deliver(self, frame_type: str, label: str, sender: str,
                 msg: InboundMessage) -> None:
        """Log + publish + hand to the router (never raises)."""
        capture = getattr(self.service, "capture", None)
        if capture is not None:
            try:
                capture.record_event(
                    time.time(), frame_type,
                    {"text": msg.text, "channel": label,
                     "sender": sender, "hops": msg.hops, "snr": msg.snr},
                    attributes={"radio": "mcp-spi"},
                    channel_name=msg.channel_name)
            except Exception:
                pass
        log.info("IN %s %s: %s%s", frame_type, label,
                 _log_line(msg.text),
                 f" (hops={msg.hops})" if msg.hops is not None else "")
        handler = self._inbound
        if handler is None:
            return
        async def _run() -> None:
            try:
                await handler(msg)
            except Exception as exc:
                log.warning("Inbound handler error: %s", exc)
        self._loop.create_task(_run())

    # ------------------------------------------------------------- outbound

    async def send_channel(self, idx: int, text: str) -> bool:
        """Router adapter: one channel reply as a real radio packet."""
        channels = getattr(self.settings, "channels", []) or []
        if idx < 0 or idx >= len(channels):
            log.warning("Channel reply dropped: unknown slot %d", idx)
            return False
        name = channels[idx].name
        entry = next((c for lst in self._channels_by_hash.values() for c in lst
                      if c["name"] == name), None)
        if entry is None:
            log.warning("Channel reply dropped: %s is not keyed", name)
            return False
        pkt = self._build_group_packet(entry, text)
        if pkt is None:
            return False
        ok = await self.send(pkt)
        if ok:
            log.info("OUT %s: %s", name, _log_line(text))
        return ok

    async def send_dm(self, sender_prefix: str, text: str) -> bool:
        """Router adapter: one DM reply as a real radio packet."""
        if self.identity is None:
            log.warning("DM reply dropped: no radio identity.")
            return False
        node = self.service.store.get_node(sender_prefix)
        pubkey_hex = (node or {}).get("pubkey") or ""
        if len(pubkey_hex) != 64:
            log.warning("DM reply dropped: no full key stored for %s.", sender_prefix)
            return False
        try:
            from pymc_core.protocol.crypto import CryptoUtils
            from pymc_core.protocol.identity import Identity
            from pymc_core.protocol.packet_builder import PacketBuilder
            peer = Identity(bytes.fromhex(pubkey_hex))
            shared = peer.calc_shared_secret(self.identity.get_private_key())
            plaintext = PacketBuilder._pack_timestamp_data(
                int(time.time()), 0x00, text.encode("utf-8"))
            payload = (PacketBuilder._hash_bytes(
                            peer.get_public_key(), self.identity)
                       + CryptoUtils.encrypt_then_mac(shared[:16], shared, plaintext))
            header = (PAYLOAD_TYPE_TXT_MSG << 2) | 2      # direct route, version 1
            raw = bytes([header]) + payload
        except Exception as exc:
            log.warning("DM reply to %s could not be encrypted: %s", sender_prefix, exc)
            return False
        ok = await self.send(raw)
        if ok:
            log.info("OUT DM->%s: %s", sender_prefix, _log_line(text))
        return ok

    def _build_group_packet(self, entry: dict, text: str) -> Optional[bytes]:
        """One GRP_TXT packet - the firmware's exact encryption format."""
        try:
            from pymc_core.protocol.crypto import CryptoUtils
            from pymc_core.protocol.packet_builder import PacketBuilder
            channel_hash, aes_key, hmac_key = derive_channel_keys(entry["secret"])
            timestamp, flags = int(time.time()), 0x00
            content = f"{self.settings.bot.display_name or 'bot'}: {text}".encode("utf-8")
            plaintext = PacketBuilder._pack_timestamp_data(timestamp, flags, content)
            ciphertext = CryptoUtils._aes_encrypt(aes_key, plaintext)
            mac = CryptoUtils._hmac_sha256(hmac_key, ciphertext)[:2]
            payload = bytes([channel_hash]) + mac + ciphertext
            header = (PAYLOAD_TYPE_GRP_TXT << 2) | 1     # flood route, version 1
            return bytes([header]) + payload
        except Exception as exc:
            log.warning("Channel packet build failed: %s", exc)
            return None

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
        # Remember our own bytes briefly: if a repeat comes back over the
        # air we must not answer ourselves (the flood echo guard).
        key = self._bytes_hash(data)
        if key:
            self._recent_tx_hashes[key] = time.time()
        # TX loopback: the modem re-broadcasts this so openHop sees bot
        # traffic. Synthetic metadata like the modem's own design expects.
        try:
            self._push_queue.put_nowait((-100, 0.0, -100, bytes(data)))
        except asyncio.QueueFull:
            self.stats.dropped += 1
        return True

    @staticmethod
    def _bytes_hash(data: bytes) -> str:
        try:
            import hashlib
            return hashlib.sha256(bytes([data[0] >> 2 & 0xFF]) + data[1:]).hexdigest()[:16]
        except Exception:
            return ""

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
