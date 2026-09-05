"""Packet capture - raw companion traffic, stored for later analysis.

Every frame the companion sends or the bot sends is recorded with its
frame type, hops, SNR, sender and timing:

* ``decoded`` layer - one row per decoded event (channel messages, DMs,
  adverts, paths, command responses). Comes from subscribing to every
  event type the meshcore library dispatches.
* ``raw`` layer (optional, ``storage.packet_raw_hex: true`` OR the runtime
  toggle - one row per wire frame, including the raw hex bytes. Comes from
  wrapping the library's reader entry point.

Rows land in the SQLite ``packets`` table (bounded by
``storage.packet_max_rows``) and are appended to a JSONL file
(``storage.packet_jsonl``) for later analysis in pandas:

    pd.read_json('data/packets.jsonl', lines=True)

Nothing here may crash the bot: every path is defensive and failures are
logged at debug level only.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, Optional

from .store import Store, _path_hash_size

log = logging.getLogger("meshtech-bot.capture")


class PacketCapture:
    """Normalizes and persists captured companion traffic."""

    def __init__(self, store: Store, settings_getter):
        self.store = store
        self._settings = settings_getter   # callable -> Settings (survives reload)
        self._jsonl_handle = None
        self._jsonl_path = None
        # Runtime switch for raw capture, set from the dashboard. None = fall
        # back to the config value; never persisted (resets on restart).
        self._raw_override = None

    # ------------------------------------------------------------------ state

    @property
    def enabled(self) -> bool:
        return bool(self._settings().storage.capture_packets)

    def _cfg(self):
        return self._settings().storage

    def set_raw_enabled(self, enabled: bool) -> None:
        """Turn raw capture on/off at runtime (not persisted)."""
        self._raw_override = bool(enabled)

    def raw_override(self) -> Optional[bool]:
        """The runtime override value, or None when only the config applies."""
        return self._raw_override

    def raw_enabled(self) -> bool:
        """Effective raw-capture state: runtime override, else the config flag."""
        if self._raw_override is not None:
            return self._raw_override
        return bool(self._cfg().packet_raw_hex)

    # ------------------------------------------------------------------ decoded

    def record_event(self, ts: float, event_type: Any,
                     payload: Dict[str, Any],
                     attributes: Optional[Dict[str, Any]] = None,
                     channel_name: Optional[str] = None) -> None:
        """Record one decoded frame/event from the meshcore dispatcher."""
        if not self.enabled:
            return
        if not isinstance(payload, dict):
            payload = {}
        frame_type = getattr(event_type, "name", str(event_type) if event_type else "EVENT")
        direction = "out" if frame_type == "MSG_SENT" else "in"
        row = {
            "ts": ts,
            "layer": "decoded",
            "direction": direction,
            "frame_type": frame_type,
            "sender": _sender_of(payload),
            "hops": _hops_of(payload),
            "snr": _snr_of(payload),
            "channel_name": channel_name or _channel_name_of(payload),
            "text": _text_of(payload),
            "size": None,
            "path_hash_size": _path_hash_size(payload),
            "payload_json": json.dumps({
                "payload": _json_safe(payload),
                "attributes": _json_safe(attributes or {}),
            }, separators=(",", ":")),
        }
        self._persist(row)

    # ------------------------------------------------------------------ raw

    def record_raw(self, ts: float, data: bytes) -> None:
        """Record one raw wire frame (optional; gated by packet_raw_hex or the
        runtime toggle)."""
        if not self.enabled or not self.raw_enabled():
            return
        try:
            from meshcore.packets import PacketType  # lazy: venv-only import
            name = PacketType(data[0]).name if data else "EMPTY"
        except Exception:
            name = "UNKNOWN"
        row = {
            "ts": ts,
            "layer": "raw",
            "direction": "in",
            "frame_type": name,
            "sender": None,
            "hops": None,
            "snr": None,
            "channel_name": None,
            "text": None,
            "size": len(data),
            "path_hash_size": None,
            "payload_json": json.dumps({
                "raw_hex": data.hex(),
                "len": len(data),
            }, separators=(",", ":")),
        }
        self._persist(row)

    # ------------------------------------------------------------------ store

    def _persist(self, row: Dict[str, Any]) -> None:
        try:
            row_id = self.store.add_packet(
                ts=row["ts"], layer=row["layer"], direction=row["direction"],
                frame_type=row["frame_type"], sender=row["sender"],
                hops=row["hops"], snr=row["snr"], channel_name=row["channel_name"],
                text=row["text"], size=row["size"], payload_json=row["payload_json"],
                path_hash_size=row["path_hash_size"],
                max_rows=self._cfg().packet_max_rows,
            )
        except Exception as exc:
            log.debug("packet store failed: %s", exc)
            return
        self._append_jsonl(row_id, row)

    # ------------------------------------------------------------------ jsonl

    def _append_jsonl(self, row_id: int, row: Dict[str, Any]) -> None:
        path = self._cfg().packet_jsonl
        if not path:
            return
        if self._jsonl_path != path:
            self._close_jsonl()
            try:
                Path(path).parent.mkdir(parents=True, exist_ok=True)
                self._jsonl_handle = open(path, "a", encoding="utf-8")
                self._jsonl_path = path
            except Exception as exc:
                log.debug("packet jsonl open failed (%s): %s", path, exc)
                self._jsonl_handle = None
                return
        try:
            line = {"id": row_id, "ts": row["ts"], "layer": row["layer"],
                    "direction": row["direction"], "frame_type": row["frame_type"],
                    "sender": row["sender"], "hops": row["hops"], "snr": row["snr"],
                    "channel_name": row["channel_name"], "text": row["text"],
                    "size": row["size"], "path_hash_size": row["path_hash_size"],
                    "payload": json.loads(row["payload_json"] or "{}")}
            self._jsonl_handle.write(json.dumps(line, separators=(",", ":")) + "\n")
            self._jsonl_handle.flush()
        except Exception as exc:
            log.debug("packet jsonl write failed: %s", exc)

    def _close_jsonl(self) -> None:
        if self._jsonl_handle is not None:
            try:
                self._jsonl_handle.close()
            except Exception:
                pass
            self._jsonl_handle = None
            self._jsonl_path = None

    def close(self) -> None:
        self._close_jsonl()

    # ------------------------------------------------------------------ queries

    def recent(self, layer: Optional[str] = None, limit: int = 50):
        return self.store.recent_packets(layer=layer, limit=limit)

    def stats(self) -> Dict[str, Any]:
        return self.store.packet_stats()

    def profile(self) -> Dict[str, Any]:
        """Packet size / inter-frame timing profile of the raw companion link."""
        return self.store.raw_packet_profile()


# --------------------------------------------------------------------------
# Field extraction helpers (defensive - the library evolves, missing keys
# simply become "unknown"). Mirrors the client's message extraction.
# --------------------------------------------------------------------------

def _sender_of(payload: Dict[str, Any]) -> Optional[str]:
    for key in ("pubkey_prefix", "sender_prefix"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value.lower()
    value = payload.get("public_key")
    if isinstance(value, str) and len(value) >= 12:
        return value[:12].lower()
    for key in ("adv_name", "name"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:64]
    return None


def _hops_of(payload: Dict[str, Any]) -> Optional[int]:
    for key in ("hops", "path_len", "path_length", "num_hops", "hop_count"):
        value = payload.get(key)
        try:
            number = int(value) if value is not None else None
        except (TypeError, ValueError):
            number = None
        if number is not None:
            break
    if number is None:
        return None
    if number in (0xFF, 255):
        return 0  # direct reception - no repeaters involved
    if 0 <= number <= 64:
        return number
    return None


def _snr_of(payload: Dict[str, Any]) -> Optional[float]:
    for key in ("snr", "SNR"):
        value = payload.get(key)
        try:
            number = float(value) if value is not None else None
        except (TypeError, ValueError):
            number = None
        if number is not None:
            return number
    return None


def _channel_name_of(payload: Dict[str, Any]) -> Optional[str]:
    value = payload.get("channel_name")
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace").rstrip("\x00")
    return value.strip() if isinstance(value, str) and value.strip() else None


def _text_of(payload: Dict[str, Any]) -> Optional[str]:
    for key in ("text", "message"):
        value = payload.get(key)
        if isinstance(value, str):
            return value[:2000]
    return None


def _json_safe(value: Any) -> Any:
    """Convert an event payload into JSON-serializable primitives.

    meshcore payloads occasionally carry bytes (paths, hashes) and sets;
    those become hex strings and sorted lists so nothing breaks json.dumps.
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, bytearray):
        return bytes(value).hex()
    if isinstance(value, (set, frozenset)):
        return sorted(_json_safe(v) for v in value)
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if hasattr(value, "isoformat"):   # datetime
        return value.isoformat()
    try:
        import enum
        if isinstance(value, enum.Enum):
            return value.name
    except Exception:
        pass
    try:
        return str(value)
    except Exception:
        return None


def now() -> float:
    return time.time()