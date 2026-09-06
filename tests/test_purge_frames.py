"""Tests for scripts/purge_frames.py - the bookkeeping-frame cleanup tool.

Uses a temp SQLite database through the same Store the bot uses, so the
schema matches production exactly.
"""
from __future__ import annotations

from scripts.purge_frames import DEFAULT_FRAME_TYPES, purge_frames
from core.store import Store


def _store(tmp_path) -> Store:
    return Store(str(tmp_path / "purge.db"))


def _seed(store: Store) -> None:
    """11 rows: 4 bookkeeping (purge candidates), 7 real traffic."""
    rows = [
        # (ts, layer, frame_type, size)
        (1.0, "decoded", "NEXT_CONTACT", None),   # bookkeeping
        (2.0, "decoded", "NEXT_CONTACT", None),   # bookkeeping
        (3.0, "decoded", "NO_MORE_MSGS", None),   # bookkeeping
        (4.0, "decoded", "CURRENT_TIME", None),   # bookkeeping
        (5.0, "decoded", "RX_LOG_DATA", 60),
        (6.0, "decoded", "CHANNEL_MSG_RECV", 40),
        (7.0, "decoded", "CHANNEL_MSG_RECV", 44),
        (8.0, "decoded", "CHANNEL_MSG_RECV", 52),
        (9.0, "raw", "RX_LOG_DATA", 77),          # raw layer: never purged
        (10.0, "raw", "RX_LOG_DATA", 71),
        (11.0, "raw", "SYNC_MESSAGE", 65),
    ]
    for ts, layer, ftype, size in rows:
        store.add_packet(ts=ts, layer=layer, direction="in",
                         frame_type=ftype, size=size)


def test_default_types_match_bot_skip_list():
    # Keep the two lists in sync: everything captured-but-skipped by the
    # bot is purgeable, and the defaults never include real traffic types.
    from core.client import _CAPTURE_SKIP
    assert set(DEFAULT_FRAME_TYPES) <= _CAPTURE_SKIP
    for real in ("RX_LOG_DATA", "CHANNEL_MSG_RECV", "ADVERTISEMENT",
                 "NEW_CONTACT", "MESSAGES_WAITING"):
        assert real not in DEFAULT_FRAME_TYPES


def test_dry_run_changes_nothing(tmp_path):
    store = _store(tmp_path)
    _seed(store)
    db = str(tmp_path / "purge.db")
    result = purge_frames(db, list(DEFAULT_FRAME_TYPES), dry_run=True)
    assert result["total_purged"] == 4
    assert result["rows_after"] == result["rows_before"]  # untouched
    assert store.packet_count() == 11


def test_purge_removes_only_bookkeeping_rows(tmp_path):
    store = _store(tmp_path)
    _seed(store)
    db = str(tmp_path / "purge.db")
    result = purge_frames(db, list(DEFAULT_FRAME_TYPES))
    assert result["total_purged"] == 4
    assert result["rows_before"] == 11
    assert result["rows_after"] == 7
    assert store.packet_count() == 7
    # every survivor is a real-traffic row
    for row in store.recent_packets(limit=100):
        assert row["frame_type"] not in DEFAULT_FRAME_TYPES
    # a second run is a no-op
    again = purge_frames(db, list(DEFAULT_FRAME_TYPES))
    assert again["total_purged"] == 0


def test_purge_writes_backup_and_reclaims_space(tmp_path):
    store = _store(tmp_path)
    _seed(store)
    db = str(tmp_path / "purge.db")
    result = purge_frames(db, list(DEFAULT_FRAME_TYPES), vacuum=True)
    assert "backup_path" in result
    from pathlib import Path
    backup = Path(result["backup_path"])
    assert backup.is_file() and backup.stat().st_size > 0
    # the backup still holds the purged rows
    import sqlite3
    bcon = sqlite3.connect(str(backup))
    try:
        n = bcon.execute(
            "SELECT COUNT(*) FROM packets WHERE frame_type='NEXT_CONTACT'"
        ).fetchone()[0]
    finally:
        bcon.close()
    assert n == 2
    assert result["vacuum_ok"] is True
    assert result["size_after"] <= result["size_before"]


def test_purge_custom_type_only(tmp_path):
    store = _store(tmp_path)
    _seed(store)
    db = str(tmp_path / "purge.db")
    result = purge_frames(db, ["RX_LOG_DATA"], layer="decoded")
    assert result["total_purged"] == 1
    assert store.packet_count() == 10
    # the raw-layer RX_LOG_DATA rows survive a decoded-layer purge
    raw_left = [r for r in store.recent_packets(layer="raw", limit=100)]
    assert len(raw_left) == 3


def test_purge_missing_database_is_reported(tmp_path):
    result = purge_frames(str(tmp_path / "nope.db"), list(DEFAULT_FRAME_TYPES))
    assert "error" in result
    assert "not found" in result["error"]
