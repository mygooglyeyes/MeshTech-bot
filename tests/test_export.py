"""Tests for scripts/export_packets.py - CSV dump + summary aggregations.

No radio or network needed: rows are written straight into a temp SQLite
database via the same Store the bot uses.
"""
from __future__ import annotations

import csv
from pathlib import Path

from core.store import Store
from scripts import export_packets

# Fixed epochs, one hour apart, so hourly summaries are deterministic and
# land in two distinct local-time hours regardless of the machine's timezone.
HOUR_A = 1_700_000_000.0
HOUR_B = HOUR_A + 3600.0


def _populate(db_path: str) -> None:
    store = Store(db_path)
    try:
        store.add_packet(HOUR_A, "decoded", "in", "CHANNEL_MSG_RECV",
                         sender="2b926f3ab12f", hops=1, snr=10.0,
                         channel_name="#bot", text="hello bot")
        store.add_packet(HOUR_A + 60, "decoded", "in", "CHANNEL_MSG_RECV",
                         sender="2b926f3ab12f", hops=1, snr=6.0,
                         channel_name="#bot", text="anyone around?")
        store.add_packet(HOUR_A + 120, "decoded", "in", "RX_LOG_DATA",
                         sender="4261018c370d", hops=6, snr=2.5,
                         path_hash_size=2)
        store.add_packet(HOUR_A + 180, "decoded", "in", "RX_LOG_DATA",
                         hops=None, snr=None, path_hash_size=1)
        store.add_packet(HOUR_B, "decoded", "out", "CHANNEL_MSG_SENT",
                         sender="a1b2c3d4e5f6", hops=0, snr=None,
                         channel_name="#bot", text="pong", path_hash_size=3)
        store.add_packet(HOUR_B + 60, "raw", "in", "CONTACT",
                         sender=None, hops=None, snr=None, size=146,
                         payload_json='{"type": "CONTACT"}')
        store.add_packet(HOUR_B + 120, "raw", "in", "LOG_DATA",
                         hops=None, snr=None, size=33)
    finally:
        store.close()


def _read(path: Path) -> list[list[str]]:
    with open(path, newline="", encoding="utf-8") as handle:
        return list(csv.reader(handle))


def test_full_dump(tmp_path):
    db = str(tmp_path / "packets.db")
    _populate(db)
    out = tmp_path / "out"
    result = export_packets.export_packets(db, str(out))
    assert result["rows"] == 7
    rows = _read(out / "packets.csv")
    header, body = rows[0], rows[1:]
    assert header == ["id", "ts_iso", "ts", "layer", "direction", "frame_type",
                      "sender", "hops", "path_hash_size", "snr",
                      "channel_name", "text", "size"]
    assert len(body) == 7
    # chronological (oldest first)
    assert float(body[0][2]) == HOUR_A
    assert float(body[-1][2]) == HOUR_B + 120
    # ts_iso is filled in and human readable
    assert body[0][1] and " " in body[0][1]
    # numeric fields survive
    decoded = [r for r in body if r[3] == "decoded"]
    assert {r[7] for r in decoded if r[7]} == {"0", "1", "6"}  # hops
    hash_sizes = {r[8] for r in decoded if r[8]}
    assert hash_sizes == {"1", "2", "3"}   # path hash size column populated
    # no payload_json column unless asked
    assert "payload_json" not in header


def test_full_dump_includes_payload_column(tmp_path):
    db = str(tmp_path / "packets.db")
    _populate(db)
    out = tmp_path / "out"
    result = export_packets.export_packets(db, str(out), include_payload=True)
    header = _read(out / "packets.csv")[0]
    assert "payload_json" in header


def test_layer_and_limit_filters(tmp_path):
    db = str(tmp_path / "packets.db")
    _populate(db)
    out = tmp_path / "out"
    # raw only
    result = export_packets.export_packets(db, str(out), layer="raw")
    assert result["rows"] == 2
    body = _read(out / "packets.csv")[1:]
    assert all(r[3] == "raw" for r in body)
    # newest 3 frames only
    result = export_packets.export_packets(db, str(out), limit=3)
    assert result["rows"] == 3
    body = _read(out / "packets.csv")[1:]
    assert len(body) == 3
    # newest frame is included, oldest is dropped
    assert float(body[-1][2]) == HOUR_B + 120
    assert float(body[0][2]) > HOUR_A


def test_summary_hourly_and_mix(tmp_path):
    db = str(tmp_path / "packets.db")
    _populate(db)
    out = tmp_path / "out"
    export_packets.export_packets(db, str(out))
    assert (out / "summary_hourly.csv").is_file()
    assert (out / "summary_frame_types.csv").is_file()
    assert (out / "summary_hops.csv").is_file()
    assert (out / "summary_path_hash.csv").is_file()
    assert (out / "summary_snr.csv").is_file()
    assert (out / "summary_senders.csv").is_file()

    rows = _read(out / "summary_hourly.csv")
    header, body = rows[0], rows[1:]
    assert header == ["hour", "decoded", "raw", "total"]
    assert len(body) == 2  # two distinct hours
    assert sum(int(r[3]) for r in body) == 7
    decoded_total = sum(int(r[1]) for r in body)
    raw_total = sum(int(r[2]) for r in body)
    assert (decoded_total, raw_total) == (5, 2)

    rows = _read(out / "summary_frame_types.csv")
    body = rows[1:]
    counts = {(r[0], r[1]): int(r[2]) for r in body}
    assert counts[("decoded", "CHANNEL_MSG_RECV")] == 2
    assert counts[("decoded", "RX_LOG_DATA")] == 2
    assert counts[("raw", "CONTACT")] == 1
    assert counts[("raw", "LOG_DATA")] == 1
    pcts = {r[1]: float(r[3]) for r in body if r[0] == "raw"}
    assert pcts["CONTACT"] == 50.0
    assert pcts["LOG_DATA"] == 50.0


def test_summary_hops_snr_senders(tmp_path):
    db = str(tmp_path / "packets.db")
    _populate(db)
    out = tmp_path / "out"
    export_packets.export_packets(db, str(out))

    rows = _read(out / "summary_hops.csv")
    body = rows[1:]
    assert rows[0] == ["hops", "count", "pct_of_reported"]
    by_bucket = {r[0]: int(r[1]) for r in body}
    # decoded frames with hops: one at 0 (own send), two at 1 hop,
    # one at 6 (-> 4+); one frame reports none
    assert by_bucket == {"0": 1, "1": 2, "4+": 1}

    rows = _read(out / "summary_snr.csv")
    body = rows[1:]
    assert rows[0] == ["hour", "frames_with_snr", "avg_snr", "min_snr", "max_snr"]
    assert sum(int(r[1]) for r in body) == 3  # 10.0, 6.0, 2.5
    avgs = {r[0]: float(r[2]) for r in body}
    # hour A has 10.0+6.0+2.5 -> avg 6.17; hour B has no SNR frames
    assert avgs[export_packets._hour_key(HOUR_A)] == 6.17
    assert export_packets._hour_key(HOUR_B) not in avgs

    rows = _read(out / "summary_path_hash.csv")
    body = rows[1:]
    assert rows[0] == ["path_hash_bytes", "bucket", "frames",
                       "pct_of_frames_with_path"]
    # three decoded frames carry a path hash size: 1, 2 and 3 bytes
    assert {int(r[0]): int(r[2]) for r in body} == {1: 1, 2: 1, 3: 1}
    buckets = {r[0]: r[1] for r in body}
    assert buckets == {"1": "1 byte", "2": "2 bytes", "3": "3+ bytes"}
    assert all(float(r[3]) == round(100.0 / 3, 1) for r in body)  # even split

    rows = _read(out / "summary_senders.csv")
    body = rows[1:]
    assert rows[0] == ["sender", "frames"]
    assert body[0][0] == "2b926f3ab12f"  # most active first
    assert int(body[0][1]) == 2


def test_packets_only_skips_summaries(tmp_path):
    db = str(tmp_path / "packets.db")
    _populate(db)
    out = tmp_path / "out"
    result = export_packets.export_packets(db, str(out), include_summaries=False)
    assert len(result["files"]) == 1
    assert (out / "packets.csv").is_file()
    assert not (out / "summary_hourly.csv").exists()
