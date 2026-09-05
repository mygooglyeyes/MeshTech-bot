"""Shared data models.

These small dataclasses are the "vocabulary" used across the bot:
inbound radio messages, messages stored in the database, and the
structured result a handler returns before it is rendered to text.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class InboundMessage:
    """A message received from the mesh, normalised for the rest of the bot.

    kind          - "channel" or "dm"
    channel_name  - e.g. "#bot" (channel messages only)
    channel_idx   - device channel slot (0-7) as reported by the radio
    sender_prefix - 12 hex chars = first 6 bytes of the sender's public key
                    (direct messages only - channel messages have no sender)
    """

    kind: str
    text: str
    channel_name: Optional[str] = None
    channel_idx: Optional[int] = None
    sender_prefix: Optional[str] = None
    sender_ts: Optional[float] = None
    recv_ts: float = field(default_factory=lambda: _now())
    hops: Optional[int] = None
    snr: Optional[float] = None
    # Display name embedded in group-message text (unverified) - see
    # split_channel_text(). Populated by the router for channel messages.
    sender_name: Optional[str] = None


@dataclass
class MsgRecord:
    """One row of the local message log (direction in or out)."""

    kind: str                 # "channel" | "dm"
    direction: str            # "in" | "out"
    channel_name: Optional[str] = None
    sender_prefix: Optional[str] = None
    text: str = ""
    sender_ts: Optional[float] = None
    recv_ts: float = field(default_factory=lambda: _now())
    hops: Optional[int] = None
    snr: Optional[float] = None
    # Display name embedded in channel text ("Name: msg") - unverified, but
    # used to attribute channel traffic to a registry node for link-quality
    # history. Always None for DMs (which carry the cryptographic prefix).
    sender_name: Optional[str] = None


@dataclass
class HandlerResult:
    """What a handler returns: structured data plus an optional custom renderer.

    kind - "text" is the built-in kind: ``data`` is a plain string (may
           contain newlines). Handlers may define their own kinds and
           override ``Handler.render()``.
    """

    kind: str = "text"
    data: Any = None
    title: Optional[str] = None


# --------------------------------------------------------------------------
# Helpers to read fields out of meshcore event payloads defensively.
# The library and openHop's emulation evolve; missing keys must never crash
# the bot, they just become "unknown" (handled by the unknown_hops policy).
# --------------------------------------------------------------------------

def _now() -> float:
    import time
    return time.time()


# MeshCore's companion protocol caps advertised names at 32 chars
# (chars(32) in RESP_CODE_CONTACT) and the name embedded in group-message
# text is the same display name. 64 leaves headroom for gateway software
# while still rejecting bogus multi-hundred-char prefixes.
MAX_SENDER_NAME_LEN = 64


def split_channel_text(raw: str):
    """Split a received group/channel message into (sender_name, body).

    MeshCore embeds the (unverified) sender display name in group-message
    text: the payload is always ``<sender name>: <message body>`` (see the
    "Group text message" section of docs.meshcore.io/payloads). The name is
    only message text, so the split is heuristic and follows the format:

      * the separator is the first ``": "`` (colon + one space);
      * the name must be non-empty, on a single line, and at most
        MAX_SENDER_NAME_LEN characters (protocol advert names are
        chars(32));
      * only the FIRST colon-space splits - the body may contain colons
        and line breaks of its own.

    Plain text without such a prefix is returned unchanged
    (sender_name=None).
    """
    text = raw.strip()
    if not text:
        return None, text
    idx = text.find(": ")
    if 0 < idx <= MAX_SENDER_NAME_LEN:
        name = text[:idx].strip()
        if name and "\n" not in name and "\r" not in name:
            return name, text[idx + 2:].strip()
    return None, text


def payload_text(payload: dict) -> str:
    for key in ("text", "message"):
        value = payload.get(key)
        if isinstance(value, str):
            return value
    return ""


def payload_channel_idx(payload: dict) -> Optional[int]:
    value = payload.get("channel_idx", payload.get("channel_index"))
    return _as_int(value)


def payload_sender_prefix(payload: dict) -> Optional[str]:
    """Direct messages identify the sender by a 12-hex-char prefix."""
    value = payload.get("pubkey_prefix", payload.get("sender_prefix", payload.get("sender")))
    if isinstance(value, str) and value:
        return value.lower()
    # Some payloads carry the full public key
    value = payload.get("public_key")
    if isinstance(value, str) and len(value) >= 12:
        return value[:12].lower()
    return None


def payload_timestamp(payload: dict) -> Optional[float]:
    """Sender-provided timestamp (seconds). 0 / missing means unknown."""
    value = payload.get("timestamp", payload.get("sender_ts", payload.get("ts")))
    number = _as_float(value)
    if number is None or number <= 0:
        return None
    return number


def payload_snr(payload: dict) -> Optional[float]:
    return _as_float(payload.get("snr"))


def hops_from_payload(payload: dict) -> Optional[int]:
    """Best-effort hop count from a received-message payload.

    The protocol reports a "path length" byte on each message:
      * 0xFF means the packet arrived directly (no repeaters) -> 0 hops
      * otherwise it is an encoded hop/path figure - we take it as-is
        when it is a sane small number.
    Returns None when the value is absent or cannot be trusted.
    """
    value = None
    for key in ("hops", "path_len", "path_length", "num_hops", "hop_count"):
        candidate = _as_int(payload.get(key))
        if candidate is not None:
            value = candidate
            break
    if value is None:
        return None
    if value in (0xFF, 255):
        # Arrived directly from the sender -> zero hops
        return 0
    if 0 <= value <= 64:
        return value
    return None


def _as_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
