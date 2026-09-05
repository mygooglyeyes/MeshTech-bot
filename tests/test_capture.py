"""Tests for core/capture.py - packet normalization and persistence."""
from __future__ import annotations

import json

from core.capture import PacketCapture, _hops_of, _json_safe, _sender_of, _snr_of
from core.store import Store


def _store(tmp_path) -> Store:
    return Store(str(tmp_path / "test.db"))


def _capture(tmp_path, settings_dict_extra=None):
    import yaml
    from core.config import load

    data = {
        "connection": {"host": "192.168.1.50", "port": 5000, "reconnect": False},
        "channels": [{"name": "#bot", "reply": True}],
        "dm": {"enabled": True, "admin_pubkey_prefixes": []},
        "storage": {
            "db_path": str(tmp_path / "test.db"),
            "capture_packets": True,
            "packet_raw_hex": False,
            "packet_jsonl": "",
            "packet_max_rows": 10000,
        },
        "replies": [],
    }
    if settings_dict_extra:
        data["storage"].update(settings_dict_extra)
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    settings = load(str(path))
    store = _store(tmp_path)
    capture = PacketCapture(store, lambda: settings)
    return capture, store


class FakeEventType:
    def __init__(self, name):
        self.name = name


def test_json_safe_handles_bytes_sets_and_enums():
    import enum

    class E(enum.Enum):
        OK = 1

    assert _json_safe(b"\x00\xab") == "00ab"
    assert _json_safe({1, 2}) == [1, 2]
    assert _json_safe({"a": b"\xff", "b": [{"c": (1, 2)}]}) == \
        {"a": "ff", "b": [{"c": [1, 2]}]}
    assert _json_safe(E.OK) == "OK"
    assert _json_safe(None) is None
    assert isinstance(_json_safe(object()), str)  # unknown objects become text


def test_field_extraction():
    payload = {
        "pubkey_prefix": "AABBCCDDEEFF",
        "path_len": 255,          # direct reception
        "SNR": -8.5,              # V3 frames use uppercase SNR
        "text": "hello",
    }
    assert _sender_of(payload) == "aabbccddeeff"
    assert _hops_of(payload) == 0
    assert _snr_of(payload) == -8.5
    assert _hops_of({"path_len": 3}) == 3
    assert _hops_of({}) is None
    assert _sender_of({"adv_name": "Alice"}) == "Alice"


def test_record_event_path_hash_size(tmp_path):
    """Path hash size (bytes per hop) is extracted and stored per frame."""
    capture, store = _capture(tmp_path)
    # RX_LOG_DATA carries the size directly
    capture.record_event(ts=1.0, event_type=FakeEventType("RX_LOG_DATA"),
                         payload={"path_hash_size": 2, "path_len": 6,
                                  "snr": 3.0})
    # contact frames carry the hash mode (size = mode + 1)
    capture.record_event(ts=2.0, event_type=FakeEventType("NEXT_CONTACT"),
                         payload={"out_path_hash_mode": 0, "adv_name": "Alice"})
    # flood-mode paths (-1) and pathless frames stay NULL
    capture.record_event(ts=3.0, event_type=FakeEventType("NEXT_CONTACT"),
                         payload={"out_path_hash_mode": -1})
    capture.record_event(ts=4.0, event_type=FakeEventType("OK"), payload={})
    rows = store.recent_packets(layer="decoded")
    by_type = {r["frame_type"]: r for r in rows}
    assert by_type["RX_LOG_DATA"]["path_hash_size"] == 2
    # both NEXT_CONTACT rows collapse in by_type, so compare them by ts
    contact_rows = sorted([r for r in rows if r["frame_type"] == "NEXT_CONTACT"],
                          key=lambda r: r["ts"])
    assert [r["path_hash_size"] for r in contact_rows] == [1, None]
    assert by_type["OK"]["path_hash_size"] is None

    # raw frames never carry a path hash size
    capture2, store2 = _capture(tmp_path, {"packet_raw_hex": True})
    capture2.record_raw(9.0, b"\x08\x00hi")
    assert store2.recent_packets(layer="raw")[0]["path_hash_size"] is None


def test_record_event_normalizes_and_stores(tmp_path):
    capture, store = _capture(tmp_path)
    capture.record_event(
        ts=123.0,
        event_type=FakeEventType("CHANNEL_MSG_RECV_V3"),
        payload={"channel_idx": 1, "path_len": 2, "SNR": -6.0,
                 "text": "ping", "sender_ts": 100.0},
        channel_name="#bot",
    )
    rows = store.recent_packets(layer="decoded")
    assert len(rows) == 1
    row = rows[0]
    assert row["frame_type"] == "CHANNEL_MSG_RECV_V3"
    assert row["direction"] == "in"
    assert row["hops"] == 2
    assert row["snr"] == -6.0
    assert row["channel_name"] == "#bot"
    assert row["text"] == "ping"


def test_record_event_outbound_direction(tmp_path):
    capture, store = _capture(tmp_path)
    capture.record_event(ts=1.0, event_type=FakeEventType("MSG_SENT"),
                         payload={"type": 3}, channel_name="#bot")
    assert store.recent_packets(layer="decoded")[0]["direction"] == "out"


def test_raw_gated_by_config(tmp_path):
    capture, store = _capture(tmp_path)   # packet_raw_hex: False
    capture.record_raw(1.0, b"\x08\x00hello")
    assert store.packet_count() == 0

    capture2, store2 = _capture(tmp_path, {"packet_raw_hex": True})
    capture2.record_raw(2.0, b"\x08\x00hello")
    rows = store2.recent_packets(layer="raw")
    assert len(rows) == 1
    assert rows[0]["frame_type"] == "CHANNEL_MSG_RECV"
    assert rows[0]["size"] == 7
    # payload_json is excluded from list responses; verify through the DB directly
    raw = store2._conn.execute(
        "SELECT payload_json FROM packets WHERE layer='raw'").fetchone()[0]
    assert json.loads(raw)["raw_hex"] == "080068656c6c6f"


def test_raw_runtime_toggle_overrides_config(tmp_path):
    capture, store = _capture(tmp_path)   # packet_raw_hex: False
    assert capture.raw_enabled() is False
    assert capture.raw_override() is None

    # Turning it on at runtime records raw frames even with config off.
    capture.set_raw_enabled(True)
    assert capture.raw_enabled() is True
    assert capture.raw_override() is True
    capture.record_raw(1.0, b"\x08\x00hello")
    assert len(store.recent_packets(layer="raw")) == 1

    # Turning it back off stops recording again.
    capture.set_raw_enabled(False)
    assert capture.raw_enabled() is False
    assert capture.raw_override() is False
    capture.record_raw(2.0, b"\x08\x00bye")
    assert len(store.recent_packets(layer="raw")) == 1


def test_raw_override_respects_config_when_unset(tmp_path):
    # No runtime override: the config flag alone decides.
    capture, _ = _capture(tmp_path, {"packet_raw_hex": True})
    assert capture.raw_override() is None
    assert capture.raw_enabled() is True


def test_jsonl_append(tmp_path):
    jsonl_path = str(tmp_path / "packets.jsonl")
    capture, store = _capture(tmp_path, {"packet_jsonl": jsonl_path})
    capture.record_event(ts=1.0, event_type=FakeEventType("OK"), payload={})
    capture.record_event(ts=2.0, event_type=FakeEventType("CONTACT"),
                         payload={"adv_name": "Alice"})
    lines = jsonl_path and open(jsonl_path, encoding="utf-8").read().splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first["frame_type"] == "OK"
    assert first["ts"] == 1.0
    assert first["path_hash_size"] is None   # key is always present in the line
    second = json.loads(lines[1])
    assert second["sender"] == "Alice"
    assert second["payload"] == {"payload": {"adv_name": "Alice"},
                                 "attributes": {}}