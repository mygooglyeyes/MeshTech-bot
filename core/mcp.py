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
import hashlib
import logging
import os
import struct
import time
from pathlib import Path
from typing import Awaitable, Callable, Optional

from .models import InboundMessage, MsgRecord

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

# MeshCore payload types - VERIFIED byte-for-byte against openhop_core
# protocol/constants.py 2026-09-10 (the values below were off by one
# before, which silently dropped every real advert and channel text).
# Firmware DM text types (MeshCore BaseChatMesh::onPeerDataRecv): only the
# two chat types earn a delivery ACK; CLI data travels as its own exchange.
TXT_TYPE_PLAIN = 0
TXT_TYPE_SIGNED_PLAIN = 2
TXT_TYPE_CLI_DATA = 1
TXT_TYPE_CLI_COMMAND = 3

PAYLOAD_TYPE_NAMES = {
    0x00: "REQ", 0x01: "RESPONSE", 0x02: "TXT_MSG", 0x03: "ACK",
    0x04: "ADVERT", 0x05: "GRP_TXT", 0x06: "GRP_DATA",
    0x07: "ANON_REQ", 0x08: "PATH", 0x09: "TRACE", 0x0A: "MULTIPART",
    0x0B: "CONTROL", 0x0F: "RAW_CUSTOM",
}
PAYLOAD_TYPE_TXT_MSG = 0x02
PAYLOAD_TYPE_ACK = 0x03
PAYLOAD_TYPE_ADVERT = 0x04
PAYLOAD_TYPE_GRP_TXT = 0x05
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


def _path_len_byte(pkt) -> int:
    """The RAW encoded path_len byte of an arriving packet.

    Bits 6-7 say how many bytes each path hash uses (1-3); bits 0-5 the
    hop count. The bot echoes this byte back verbatim on routed replies
    so a node taught in 2-byte hashes is answered in 2-byte hashes - the
    per-hop size must match end to end, or intermediate hops cannot read
    the path at all.
    """
    try:
        return int(pkt.path_len) & 0xFF
    except Exception:
        return 0


def _hash_size_from_path_len(path_len_byte: int) -> int:
    """Bytes per path hop encoded in a path_len byte (1-3; 0 on nonsense)."""
    size = ((path_len_byte >> 6) & 0x03) + 1
    return size if size <= 3 else 0


def _encode_zero_hop_path_len(hash_size: int) -> int:
    """Encoded path_len byte for a ZERO-hop packet at a given hash size.

    Only bits 6-7 (the size) matter when there is no path; bits 0-5 are
    the hop count (0). Returns 0 (1-byte hashes) for nonsense sizes.
    """
    if hash_size not in (1, 2, 3):
        return 0
    return ((hash_size - 1) << 6) & 0xFF


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


def contact_share_string(pubkey_hex: str, name: str) -> str:
    """The MeshCore app's contact-sharing string for the bot (the FAQ's
    documented format, type=1 = chat node) - QR-encode it or paste it
    into the app's 'add contact' box to add LoganBot without waiting
    for the mesh to carry an advert.

    App-verified QR format (MeshCore docs/faq.md 7.5):
      meshcore://contact/add?name=<name>&public_key=<key>&type=<type>
    """
    from urllib.parse import quote
    key = (pubkey_hex or "").strip().lower()
    if len(key) != 64:
        return ""
    safe_name = quote(sanitize_advert_name(name) or "bot", safe="")
    return f"meshcore://contact/add?name={safe_name}&public_key={key}&type=1"


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


def _clamp32(scalar: bytes) -> bytes:
    """X25519 scalar clamping (RFC 7748 / MeshCore key_exchange.c):
    clear the 3 low bits, clear bit 255, set bit 254."""
    s = bytearray(scalar)
    s[0] &= 248
    s[31] &= 63
    s[31] |= 64
    return bytes(s)


def _expand_firmware_key(seed32: bytes) -> bytes:
    """32-byte Ed25519 seed -> 64-byte MeshCore firmware key [scalar|nonce].

    Matches MeshCore firmware identity generation (SHA-512 expansion, scalar
    clamped BEFORE anything else): the public key is derived from the clamped
    scalar AND the ECDH uses the same clamped scalar, so what the bot
    advertises and what it computes with agree.

    This replaces the old os.urandom(64) generation whose raw scalar was
    unclamped: pubkey = raw*G but ECDH ran with clamped(raw) - the two
    disagree unless the raw bytes happen to be pre-clamped (p=1), which is
    exactly the v0.0.113 "HMAC failed against N stored key(s)" DM bug.
    """
    if len(seed32) != 32:
        raise ValueError("seed must be 32 bytes")
    digest = hashlib.sha512(seed32).digest()
    return _clamp32(digest[:32]) + digest[32:]


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
            if len(raw) == 64 and raw[:32] != _clamp32(raw[:32]):
                # Legacy os.urandom(64) key: advert pubkey (raw*G) and the
                # ECDH scalar (clamped) disagree, so DMs can never decrypt
                # in either direction. No code path can fix it in place -
                # the mesh address must change. Refuse rather than run
                # half-blind; deleting the file mints a proper key.
                log.warning(
                    "Radio identity %s has an UNCLAMPED scalar (legacy "
                    "os.urandom format) - DMs cannot work with it. Delete "
                    "the file and restart to mint a proper key (the bot's "
                    "mesh address will change; re-add contacts).",
                    identity_path)
                return None
            seed = raw
        except Exception as exc:
            log.error("Radio identity file %s is unusable (%s) - a NEW key "
                      "would change the bot's mesh address. NOT generating; "
                      "fix or remove the file, then restart.", identity_path, exc)
            return None
    if seed is None:
        # Proper firmware-format key: SHA-512 expand + CLAMP the scalar.
        # (os.urandom(64) was the v0.0.113 DM bug - see _expand_firmware_key.)
        seed = _expand_firmware_key(os.urandom(32))
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
    """(channel_hash, aes_key, hmac_key) - the firmware's group-text scheme
    (PacketBuilder.create_group_text_packet / GroupTextHandler's decrypt
    path), proven against live on-air traffic: AES key = the secret itself
    (first 16 bytes after zero-padding to 32), HMAC key = the full
    zero-padded 32 bytes, and the 1-byte channel hash is sha256 of the
    secret with the firmware's 128-bit rule: a secret whose second half is
    all zeros (e.g. the well-known sha256("#name")[:16] keys) hashes only
    its first 16 bytes."""
    import hashlib

    raw = _secret_bytes(secret)
    # Hash basis: zero-tail reduction on the ORIGINAL (pre-padding) bytes,
    # so a short text secret never falsely triggers it.
    basis = raw[:16] if (len(raw) >= 32 and raw[16:32] == b"\x00" * 16) else raw
    if len(raw) < 32:
        raw = raw + b"\x00" * (32 - len(raw))
    channel_hash = hashlib.sha256(basis).digest()[0]
    return channel_hash, raw[:16], raw


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
        self._advert_task: Optional[asyncio.Task] = None
        # --- decode machinery (Brett's "full decode" decision) ----------
        self.identity = load_local_identity()
        self._channels_by_hash: dict[int, list[dict]] = {}
        # config channel name -> index in settings.channels (the slot number
        # the router uses as channel_idx for replies). Declared BEFORE
        # _rebuild_channel_table() fills it - declaring it after the call
        # silently wiped the map (v0.0.101 bench test: every reply carried
        # channel_idx=None and the router crashed on send).
        self._channel_index: dict[str, int] = {}
        self._rebuild_channel_table()
        # Recent packet hashes: MeshCore flood dedup (a flood heard over
        # several paths would otherwise hit the router once per hearing).
        self._seen_hashes: dict[str, float] = {}
        # Own-packet echo guard: the bot's src hash, plus a short window of
        # hashes of packets we just sent (the radio may hear our repeats).
        self._own_hash = (self.identity.get_public_key()[0]
                          if self.identity else None)
        self._recent_tx_hashes: dict[str, float] = {}
        # Politeness gap between the bot's OWN packets (v0.0.106): the
        # driver's LBT defers to other stations, not to our own next
        # packet - without this, multi-chunk replies went out ~0.4 s
        # apart. Set to 0 only when the mesh demands it.
        self._last_tx_at: float = 0.0
        self._politeness = max(
            0.0, float(getattr(self.settings.mcp,
                               "inter_packet_politeness_seconds", 2.0)))
        # Serializes wait+TX so two concurrent sends cannot both sleep
        # past the gap and then transmit back-to-back anyway.
        self._tx_gate = asyncio.Lock()

    # ------------------------------------------------- client-interface shim
    # bot.py sets service.client = mcp in radio mode (the router's reply
    # interface). The status/dashboard code was written for the companion
    # client, so it also asks for the three members below - without them
    # every /api/status call raised AttributeError.

    @property
    def is_connected(self) -> bool:
        """Radio-mode 'connected' = the SPI radio is up and listening."""
        return self.is_running

    @property
    def own_name(self) -> str:
        """The bot's on-air name (the dashboard shows it in its place)."""
        bot_cfg = getattr(self.settings, "bot", None)
        return (sanitize_advert_name(bot_cfg.display_name) if bot_cfg else "") or ""

    def _advert_name(self) -> str:
        """Sanitized on-air name for adverts (never empty)."""
        return self.own_name or "bot"

    def channel_names(self) -> dict[int, str]:
        """Configured channels by slot index - the same indexing that
        send_channel() uses to turn a slot number back into a channel."""
        return {i: ch.name
                for i, ch in enumerate(getattr(self.settings, "channels", []) or [])
                if ch.name}

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
                self._spawn_advert_timer()
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

    def _spawn_advert_timer(self) -> None:
        """Periodic flood advert task (mcp.advert_interval_hours, 0 = off)."""
        hours = getattr(self.settings.mcp, "advert_interval_hours", 0.0)
        if not hours or hours <= 0 or self.identity is None:
            return
        async def _timer() -> None:
            interval = hours * 3600.0
            while not self.service.stop_requested:
                try:
                    await asyncio.sleep(interval)
                except asyncio.CancelledError:
                    raise
                if self.service.stop_requested or not self.is_running:
                    continue
                bot_cfg = getattr(self.settings, "bot", None)
                advert_name = (sanitize_advert_name(
                    bot_cfg.display_name if bot_cfg else "") or "bot")
                try:
                    from pymc_core.protocol.packet_builder import PacketBuilder
                    pkt = PacketBuilder.create_flood_advert(
                        self.identity, advert_name)
                    if await self.send(pkt.write_to()):
                        log.info("Periodic flood advert sent (every %gh).", hours)
                except Exception as exc:
                    log.warning("Periodic advert failed (non-fatal): %s", exc)
        self._advert_task = asyncio.create_task(
            _timer(), name="mcp-advert-timer")

    async def _announce_self(self) -> None:
        """Announce the bot once the radio is up (when
        bot.advertise_on_start, the same switch companion mode uses):
        a FLOOD advert (repeated across the mesh - this is how distant
        nodes learn the bot) followed by a DIRECT advert (zero hops -
        phones in radio range pick it up instantly, no repeater needed).

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
            await asyncio.sleep(2.0)            # let the driver settle
            flood = PacketBuilder.create_flood_advert(
                self.identity, advert_name)
            if await self.send(flood.write_to()):
                log.info("Flood advert sent - the bot is on the mesh as '%s'.",
                         advert_name)
            await asyncio.sleep(5.0)            # airtime gap between adverts
            direct = PacketBuilder.create_direct_advert(
                self.identity, advert_name)
            if await self.send(direct.write_to()):
                log.info("Direct advert sent (one hop, for nearby phones).")
            # Write the contact share string next to the identity so the
            # QR / paste string is easy to find on the box later.
            try:
                share = contact_share_string(
                    self.identity.get_public_key().hex(), advert_name)
                if share:
                    share_path = Path(IDENTITY_FILE).with_name(
                        "bot_contact_share.txt")
                    share_path.write_text(share + "\n", encoding="utf-8")
                    log.info("Contact share string saved to %s", share_path)
            except OSError:
                pass
        except Exception as exc:
            log.warning("Self-advert failed (non-fatal): %s", exc)

    async def _radio_up(self) -> None:
        from pymc_core.hardware.sx1262_wrapper import SX1262Radio

        kwargs = self._radio_kwargs()
        log.info("Radio init: PiMesh-1W v2 profile, %.3fMHz SF%d %gkHz %ddBm",
                 kwargs["frequency"] / 1e6, kwargs["spreading_factor"],
                 kwargs["bandwidth"] / 1000, kwargs["tx_power"])
        radio = SX1262Radio(**kwargs)
        try:
            ok = await self._loop.run_in_executor(None, radio.begin)
            if not ok:
                raise RuntimeError("radio.begin() returned False")
            # CAD thresholds for the radio's own LBT (mcp.cad_peak/cad_min,
            # Brett's openHop tuning for this board: 15/7). 0/0 leaves the
            # driver's defaults alone. Must succeed before first TX; a bad
            # value here is a config error, so fail loud rather than drift
            # silently onto different air sensitivity.
            cfg = self.settings.mcp
            self._apply_cad_thresholds(
                radio, int(getattr(cfg, "cad_peak", 0)),
                int(getattr(cfg, "cad_min", 0)))
            radio.set_rx_callback(self._on_radio_rx)
        except Exception:
            # v0.0.107: a failed init must not leave the radio object holding
            # the GPIO lines - the driver enforces one active instance, so an
            # abandoned radio makes every retry die with 'GPIO Pin already in
            # use' and the service crash-loops. Release the pins, then let
            # start()'s retry loop try again clean.
            try:
                radio.cleanup()
            except Exception as cleanup_exc:
                log.warning("Radio cleanup after failed init also failed: %s",
                            cleanup_exc)
            raise
        self.radio = radio
        log.info("Radio up - the MCP owns the air.")

    @staticmethod
    def _apply_cad_thresholds(radio, cad_peak: int, cad_min: int) -> None:
        """Program the CAD (listen-before-talk) detection thresholds.

        Duck-typed on purpose: the real driver exposes
        ``set_custom_cad_thresholds(peak, min)`` and tests pass a fake.
        0/0 keeps the driver's own per-SF defaults. Range errors raise -
        config validation already guards this, so a raise here means a
        hand-edited or stale config reached us some other way.
        """
        if not (0 <= cad_peak <= 31 and 0 <= cad_min <= 31):
            raise ValueError(
                f"CAD thresholds out of range 0-31: peak={cad_peak} min={cad_min}")
        if cad_peak or cad_min:
            radio.set_custom_cad_thresholds(cad_peak, cad_min)
            log.info("CAD thresholds applied: peak=%d min=%d", cad_peak, cad_min)
        else:
            log.info("CAD thresholds: driver defaults (config 0/0).")

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
            # Default secret matches the phone apps: hashtag channels use
            # the WELL-KNOWN 128-bit key sha256("#name")[:16] - the '#'
            # matters (see core/client.py). Kept as a hex STRING so
            # _secret_bytes() restores the exact key bytes.
            import hashlib
            secret = cfg_channel.secret_hex or hashlib.sha256(
                cfg_channel.name.encode("utf-8")).digest()[:16].hex()
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

        # Only TXT_MSG packets carry a destination hash in payload[1]; for
        # GRP_TXT payload[1] is a MAC byte, so the own-packet check below is
        # meaningful for DMs only (flood repeats are handled by _is_duplicate).
        if payload_type == PAYLOAD_TYPE_TXT_MSG and self._own_hash is not None \
                and len(pkt.payload) >= 2:
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
        if src_hash is not None and src_hash == self._own_hash:
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
        # Record the route the advert travelled (v0.0.111): DM replies back
        # to this node ride the same path. The flood-route packet carries the
        # hops it took; path bytes live in pkt.path - one byte per hop for
        # 1-byte-hash meshes (this firmware). 0 hops = direct neighbour: no
        # path needed, a path-less direct packet already reaches them.
        advert_hops = _hops_from_packet(pkt)
        route_hops = None
        route_path_hex = None
        route_path_len = None                 # RAW encoded byte (size bits in)
        if advert_hops is not None and advert_hops > 0:
            try:
                path_len_byte = _path_len_byte(pkt)
                path_bytes = bytes(pkt.path[:pkt.get_path_byte_len()])
                # Size-agnostic: accept ANY per-hop hash size (1, 2 or 3
                # bytes - the mesh is migrating to 2-byte). Consistency is
                # what matters: the path's byte length must equal
                # hops x size exactly as the sender encoded it.
                size = _hash_size_from_path_len(path_len_byte)
                if (size and len(path_bytes) == advert_hops * size
                        and len(path_bytes) <= 63 * size):
                    route_hops = advert_hops
                    route_path_hex = path_bytes.hex()
                    route_path_len = path_len_byte
            except Exception:
                pass                     # path untrustworthy - reply floods
        self.service.store.upsert_node(
            pubkey=pubkey_hex, name=name,
            snr=snr,
            lat=decoded.get("latitude") or decoded.get("lat") or None,
            lon=decoded.get("longitude") or decoded.get("lon") or None,
            source="advert", ts=now,
            route_hops=route_hops,
            route_summary=route_path_hex,
            route_path_len=route_path_len)
        log.info("ADVERT %s (%s) rssi=%d snr=%.1f%s", name,
                 pubkey_hex[:12], rssi, snr,
                 f" route={route_hops}h" if route_hops else "")

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
            # Deliver the FULL wire text ("Name: body") - the router owns
            # the sender-name split (mesh.channel_sender_name policy). The
            # companion path delivers raw text too; pre-stripping here made
            # the router split again and clobber the sender to "unknown",
            # so every channel command was silently ignored (bench test,
            # 2026-09-10). _deliver still shows the body in the log.
            msg = InboundMessage(
                kind="channel", text=content,
                channel_name=channel["name"],
                channel_idx=self._channel_index.get(channel["name"]),
                sender_ts=float(timestamp) if timestamp else None,
                hops=_hops_from_packet(pkt), snr=snr)
            name_for_log = _split_sender(content)[0] or "?"
            self._deliver("GRP_TXT", channel["name"], name_for_log, msg,
                          path_hash_size=(
                              _hash_size_from_path_len(_path_len_byte(pkt))
                              if (msg.hops or 0) > 0 else None))
            return                       # first validating candidate wins
        log.debug("GRP_TXT hash %02X: no channel key matched", channel_hash)

    # -- direct messages ---------------------------------------------------

    def _handle_text_message(self, pkt, rssi: int, snr: float) -> None:
        payload = pkt.get_payload()
        if len(payload) < 3 or self.identity is None:
            return
        dest_hash = pkt.payload[0]
        src_hash = pkt.payload[1]
        # Firmware behavior (BaseChatMesh): a flood TXT_MSG names its
        # destination by the first byte of the recipient's pubkey. If it
        # is not us, this DM is somebody else's conversation - skipping
        # it BEFORE any decrypt attempt keeps a hash collision with
        # another 0x97-addressed node from polluting our failure
        # diagnostics (the journal 'src 97' storm included such packets).
        if self._own_hash is not None and dest_hash != self._own_hash:
            log.debug("TXT_MSG for %02X (not us) - skipping.", dest_hash)
            return
        body = payload[2:]               # skip dest_hash + src_hash
        candidates = self._contact_candidates(src_hash)
        for candidate in candidates:
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
            # Delivery ACK (v0.0.111): phones mark a DM "failed" without one.
            # Recipe mirrors firmware BaseChatMesh::onPeerDataRecv -> sendAckTo
            # via openhop_core TextMessageHandler._calc_ack_hash:
            #   sha256(timestamp||flags||text||sender_pubkey)[:4]
            #   + ext_attempt byte + random byte (bytes 4-5 only make the ACK
            #   packet hash unique so mesh dedup never drops a legit ACK).
            self._schedule_dm_ack(pkt, candidate["pubkey"], shared,
                                  timestamp, flags, text)
            msg = InboundMessage(
                kind="dm", text=text, sender_prefix=prefix,
                sender_ts=float(timestamp) if timestamp else None,
                hops=_hops_from_packet(pkt), snr=snr)
            self._deliver("TXT_MSG", prefix, prefix, msg,
                          path_hash_size=(
                              _hash_size_from_path_len(_path_len_byte(pkt))
                              if (msg.hops or 0) > 0 else None))
            return
        self.stats.decrypt_fail += 1
        # INFO (v0.0.111): this used to hide at DEBUG, which turned "DM from
        # an unknown key" into a silent black hole - the sender saw nothing,
        # the journal showed nothing. Reaching here means the packet was
        # ADDRESSED TO US (v0.0.113 dest-hash gate), so the failure is real.
        if not candidates:
            # Their advert was never heard: we lack their full pubkey and
            # CANNOT decrypt. The usual cause - ask them to advert.
            log.info("TXT_MSG dest=us src=%02X: no known key - DM dropped "
                     "(decrypt_fail=%d). Ask the sender to advert (or !dm "
                     "from their side) so the bot learns their key.",
                     src_hash, self.stats.decrypt_fail)
            return
        # Candidates existed but every HMAC failed: the sender is NOT the
        # node we have stored for that hash byte (stale/wrong key pair on
        # one side). Say exactly what was tried so the next 'why no reply'
        # journal pull answers itself.
        log.info("TXT_MSG dest=us src=%02X: HMAC failed against %d stored "
                 "key(s) (%s) - sender is NOT who we think; both sides "
                 "must re-advert so the key pair refreshes.",
                 src_hash, len(candidates),
                 ", ".join(c["pubkey"][:6] for c in candidates))

    # -- DM delivery ACK --------------------------------------------------

    def _schedule_dm_ack(self, pkt, sender_pubkey_hex: str, shared: bytes,
                         timestamp: int, flags: int, text: str) -> None:
        """Queue a firmware-compatible delivery ACK for a decrypted DM.

        Phones show "sending..." until an ACK arrives; without this the
        MeshCore app gives up after 3 tries even when the bot heard the
        message (Brett's phone, 2026-09-12). Sent after TXT_ACK_DELAY
        (200 ms, matching firmware) via the radio's own politeness gate.
        Flood-arrived DMs are ACKed as a flood-routed discrete ACK - the
        sender may be reachable only the way their packet came in.
        """
        import hashlib as _hashlib
        import os as _os
        try:
            sender_pubkey = bytes.fromhex(sender_pubkey_hex)
            text_bytes = (text or "").encode("utf-8")
            basis = (int(timestamp).to_bytes(4, "little")
                     + bytes([flags & 0xFF]) + text_bytes + sender_pubkey)
            ack_bytes = _hashlib.sha256(basis).digest()[:4] \
                + b"\x00" + _os.urandom(1)
            is_flood = _hops_from_packet(pkt) not in (0,)  # flood or unknown
            header = (PAYLOAD_TYPE_ACK << 2) | (1 if is_flood else 2)
            raw = bytes([header, 0]) + ack_bytes
        except Exception as exc:
            log.debug("ACK build failed (non-fatal): %s", exc)
            return
        self._loop.create_task(self._send_ack_later(raw, sender_pubkey_hex))

    async def _send_ack_later(self, raw: bytes, sender_pubkey_hex: str) -> None:
        await asyncio.sleep(0.2)             # firmware TXT_ACK_DELAY
        if not self.is_running:
            return
        if await self.send(raw):
            log.info("ACK sent -> %s", sender_pubkey_hex[:12])

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
                 msg: InboundMessage,
                 path_hash_size: Optional[int] = None) -> None:
        """Log + publish + hand to the router (never raises).

        path_hash_size (bytes per path hop, when the frame carried a path)
        lands in the capture so the !2byte report reflects MCP-mode
        radio traffic, not just companion-mode captures.
        """
        capture = getattr(self.service, "capture", None)
        if capture is not None:
            try:
                capture.record_event(
                    time.time(), frame_type,
                    {"text": msg.text, "channel": label,
                     "sender": sender, "hops": msg.hops, "snr": msg.snr,
                     "path_hash_size": path_hash_size},
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
        if idx is None or idx < 0 or idx >= len(channels):
            log.warning("Channel reply dropped: unknown slot %r", idx)
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
            # Persist + publish like the companion path does, so the
            # dashboard log window shows bot sends too (bench test
            # 2026-09-10: replies went out but the log window had no
            # [out] row - only inbound rows were ever stored).
            self.service.store.add_message(MsgRecord(
                kind="channel", direction="out", channel_name=name,
                text=text, recv_ts=time.time()))
            self.service.feed.publish("message_out", {"kind": "channel",
                                                      "channel": name,
                                                      "text": text})
        return ok

    async def send_direct_advert(self) -> bool:
        """Router adapter: one DIRECT (local-only, zero-hop) advert.

        Lets the router advert the bot to the asking phone right before a
        DM thread starts (!dm, v0.0.111): the phone refreshes the bot's
        contact - key AND current routing - straight from the air.
        """
        if self.identity is None or not self.is_running:
            return False
        try:
            from pymc_core.protocol.packet_builder import PacketBuilder
            advert = PacketBuilder.create_direct_advert(
                self.identity, self._advert_name())
            ok = await self.send(advert.write_to())
            if ok:
                log.info("Direct advert sent (one hop, for nearby phones).")
            return ok
        except Exception as exc:
            log.warning("Direct advert failed (non-fatal): %s", exc)
            return False

    async def send_dm(self, sender_prefix: str, text: str) -> bool:
        """Router adapter: one DM reply as a real radio packet.

        v0.0.111: when the node store holds a taught path for the sender
        (from a PATH return after our advert), the DM rides that path -
        a path-less direct packet only reaches arm's-length neighbours,
        which is why Brett's repeater-range phone never saw replies.
        Flood-arrived DMs (or unknown paths) get the flood-routed form.
        """
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
            path, path_len = self._out_path_for(node)
            if path:
                header = (PAYLOAD_TYPE_TXT_MSG << 2) | 2  # direct route, version 1
                raw = (bytes([header, path_len]) + bytes(path) + payload)
                log.info("DM reply via stored path (%d hop(s), %d-byte hashes).",
                         path_len & 0x3F, _hash_size_from_path_len(path_len))
            else:
                header = (PAYLOAD_TYPE_TXT_MSG << 2) | 1  # flood route, version 1
                raw = bytes([header, 0]) + payload
                log.info("DM reply flood-routed (no stored path).")
        except Exception as exc:
            log.warning("DM reply to %s could not be encrypted: %s", sender_prefix, exc)
            return False
        ok = await self.send(raw)
        if ok:
            log.info("OUT DM->%s: %s", sender_prefix, _log_line(text))
            self.service.store.add_message(MsgRecord(
                kind="dm", direction="out", sender_prefix=sender_prefix,
                text=text, recv_ts=time.time()))
            self.service.feed.publish("message_out", {"kind": "dm",
                                                      "sender": sender_prefix,
                                                      "text": text})
        return ok

    @staticmethod
    def _out_path_for(node: Optional[dict]) -> tuple[list, int]:
        """Stored (path bytes, RAW encoded path_len) for a node, or ([], 0).

        The store keeps the route observed when the node's advert arrived
        (source='advert'), plus the raw encoded path_len byte (v0.0.112:
        bits 6-7 = per-hop hash size). A 0-hop advert means the node is a
        direct neighbour: a path-less direct packet already reaches it.
        Rows that predate the route_path_len column are 1-byte-hash paths
        (encoded byte == hop count). The reply echoes the encoded byte
        verbatim so a node taught in 2-byte hashes is answered in
        2-byte hashes.
        """
        if not node:
            return [], 0
        hops = node.get("route_hops")
        path_hex = node.get("route_summary") or ""   # path bytes (hex), see _handle_advert
        if hops is None or not path_hex:
            return [], 0
        try:
            path = list(bytes.fromhex(path_hex))
        except ValueError:
            return [], 0
        hops = int(hops)
        if not 1 <= hops <= 63:
            return [], 0
        stored_raw = node.get("route_path_len")       # may be None (old rows)
        if stored_raw is not None:
            size = _hash_size_from_path_len(int(stored_raw))
            if (size and len(path) == hops * size):
                return path, int(stored_raw) & 0xFF
            return [], 0                              # row inconsistent - flood
        if len(path) != hops:                         # legacy 1-byte-hash row
            return [], 0
        return path, hops & 0x3F

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
            return bytes([header, 0]) + payload          # 0x00 = path_len (flood)
        except Exception as exc:
            log.warning("Channel packet build failed: %s", exc)
            return None

    # ------------------------------------------------------------- radio TX

    async def send(self, data: bytes) -> bool:
        """Transmit over the real radio, then loop back to the modem feed.

        A politeness gap (mcp.inter_packet_politeness_seconds, default
        2 s) is waited BEFORE the packet goes out whenever the bot's own
        previous transmission ended less than that long ago. The driver
        still runs its own LBT/CAD check under its TX lock - the gap is
        the bot's courtesy on top, not a replacement. Zero at radio-down,
        so a queued reply never stalls at shutdown.
        """
        if not self.is_running or self.radio is None:
            log.warning("Radio not up - dropping TX (%dB).", len(data) if data else 0)
            return False
        # mesh.path_hash_size (v0.0.112): stamp our announced per-hop hash
        # size onto every ZERO-hop packet we originate (bits 6-7 of the
        # path_len byte). Routed packets (hop count > 0) carry the size the
        # destination taught us and are never restamped. data[1] is the
        # path_len byte for every packet this bot transmits (PacketBuilder
        # write_to() emits header | path_len | path | payload).
        if len(data) > 1 and (data[1] & 0x3F) == 0:
            stamp = _encode_zero_hop_path_len(
                getattr(getattr(self.settings, "mesh", None),
                        "path_hash_size", 1) or 1)
            if data[1] != stamp:
                data = bytes([data[0], stamp]) + data[2:]
        async with self._tx_gate:
            now = time.monotonic()
            since_last = now - self._last_tx_at
            if self._politeness > 0 and 0 <= since_last < self._politeness:
                wait = self._politeness - since_last
                log.info("Politeness gap: waiting %.1fs before TX (%dB).",
                         wait, len(data))
                await asyncio.sleep(wait)
            try:
                await self.radio.send(data)
            except Exception as exc:
                log.error("TX failed: %s", exc)
                return False
            self._last_tx_at = time.monotonic()
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
        """Hash over header + payload (NO path_len byte) - byte-for-byte the
        same recipe as _packet_hash_hex on RX, so an echoed TX matches its
        dedup entry and the bot never answers itself. Before this fix the
        two recipes differed by the path_len byte and could never match."""
        try:
            import hashlib
            return hashlib.sha256(bytes([(data[0] >> 2) & 0xFF])
                                  + data[2:]).hexdigest()[:16]
        except Exception:
            return ""

    # -------------------------------------------------------------- shutdown

    async def stop(self) -> None:
        self.is_running = False
        if self._advert_task is not None:
            self._advert_task.cancel()
            self._advert_task = None
        radio, self.radio = self.radio, None
        if radio is not None:
            try:
                await self._loop.run_in_executor(None, radio.cleanup)
            except Exception as exc:
                log.warning("Radio cleanup: %s", exc)
        log.info("Radio released.")
