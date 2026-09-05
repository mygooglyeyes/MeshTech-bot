"""Tests for core/store.py - SQLite layer (uses temp files, no network)."""
from __future__ import annotations

import time

from core.models import MsgRecord
from core.store import Store


def _store(tmp_path) -> Store:
    return Store(str(tmp_path / "test.db"))


def test_migrations_idempotent_on_reopen(tmp_path):
    path = str(tmp_path / "test.db")
    Store(path).close()
    store = Store(path)  # reopening must not fail
    store.close()


def test_node_upsert_keeps_known_name(tmp_path):
    store = _store(tmp_path)
    now = time.time()
    store.upsert_node("aabbccddeeff00112233445566778899", name="Alice",
                      snr=-8.0, source="contact", ts=now)
    store.upsert_node("aabbccddeeff00112233445566778899", name="", snr=-6.0, ts=now + 10)
    node = store.get_node("aabbccddeeff")
    assert node["name"] == "Alice"          # empty name did not overwrite
    assert node["last_snr"] == -6.0         # newer reading did
    assert store.resolve_name("aabbccddeeff") == "Alice"
    assert store.find_node("alice")["prefix"] == "aabbccddeeff"
    store.close()


def test_message_log_and_totals(tmp_path):
    store = _store(tmp_path)
    now = time.time()
    store.add_message(MsgRecord(kind="channel", direction="in",
                                channel_name="#bot", text="hello",
                                sender_ts=now - 4, recv_ts=now, hops=1))
    store.add_message(MsgRecord(kind="channel", direction="in",
                                channel_name="#diagnostics", text="x",
                                sender_ts=now - 90, recv_ts=now - 30, hops=5))
    store.add_message(MsgRecord(kind="dm", direction="out",
                                sender_prefix="aabbccddeeff", text="reply",
                                recv_ts=now))
    totals = store.totals()
    assert totals["in_channel"] == 2
    assert totals["out_dm"] == 1
    assert totals["total"] == 3
    distribution = {row["hops"]: row["count"] for row in store.hop_distribution()}
    assert distribution == {"1": 1, "4+": 1}
    store.close()


def test_propagation_stats_ignore_skewed_timestamps(tmp_path):
    store = _store(tmp_path)
    now = time.time()
    store.add_message(MsgRecord(kind="dm", direction="in",
                                sender_prefix="aabbccddeeff", text="a",
                                sender_ts=now - 5, recv_ts=now))
    store.add_message(MsgRecord(kind="dm", direction="in",
                                sender_prefix="aabbccddeeff", text="b",
                                sender_ts=now + 99999, recv_ts=now))  # clock skew
    stats = store.propagation_stats(prefix="aabbccddeeff")
    assert stats["count"] == 2
    assert stats["delay_count"] == 1
    assert stats["delay_avg"] == 5.0
    store.close()


def test_route_history_only_records_changes(tmp_path):
    store = _store(tmp_path)
    store.add_route("aabbccddeeff", hops=2, summary="r1")
    store.add_route("aabbccddeeff", hops=2, summary="r1")
    store.add_route("aabbccddeeff", hops=3, summary="r1,r2")
    history = store.route_history("aabbccddeeff")
    assert len(history) == 2
    assert history[0]["hops"] == 3
    store.close()


def test_route_snr_change_records_new_snapshot(tmp_path):
    store = _store(tmp_path)
    store.add_route("aabbccddeeff", hops=2, summary="r1", snr=-8.0)
    store.add_route("aabbccddeeff", hops=2, summary="r1", snr=-8.0)  # no change
    store.add_route("aabbccddeeff", hops=2, summary="r1", snr=-6.5)  # SNR moved
    history = store.route_history("aabbccddeeff")
    assert len(history) == 2
    assert history[0]["snr"] == -6.5
    store.close()


def test_message_persists_sender_name(tmp_path):
    store = _store(tmp_path)
    store.add_message(MsgRecord(kind="channel", direction="in",
                                channel_name="#bot", text="Logan: hi",
                                sender_name="Logan", hops=1, recv_ts=1.0))
    rows = store.query_messages(kind="channel")
    assert rows[0]["sender_name"] == "Logan"
    store.close()


def test_link_history_merges_sources(tmp_path):
    store = _store(tmp_path)
    key = "aabbccddeeff00112233445566778899"
    store.upsert_node(key, name="Alice", ts=1.0)
    # advert observation
    store.add_route("aabbccddeeff", hops=2, summary="r1", snr=-8.0, ts=10.0)
    # DM observation (hops only)
    store.add_message(MsgRecord(kind="dm", direction="in", sender_prefix="aabbccddeeff",
                                text="hi", hops=1, recv_ts=20.0))
    # channel observation attributed via embedded name (SNR only)
    store.add_message(MsgRecord(kind="channel", direction="in",
                                channel_name="#bot", text="Alice: hello",
                                sender_name="Alice", snr=-6.0, hops=0, recv_ts=30.0))
    # unrelated traffic must not leak in
    store.add_message(MsgRecord(kind="channel", direction="in",
                                channel_name="#bot", text="Bob: hi",
                                sender_name="Bob", snr=-5.0, recv_ts=40.0))
    history = store.link_history("aabbccddeeff")
    assert len(history) == 3
    # newest first
    assert [h["source"] for h in history] == ["channel", "dm", "advert"]
    assert history[0]["snr"] == -6.0
    assert history[1]["hops"] == 1
    assert history[2]["hops"] == 2
    assert store.link_history("000000000000") == []  # unknown node
    store.close()


def test_blocked_nodes_roundtrip(tmp_path):
    store = _store(tmp_path)
    # block by prefix; matching is case-insensitive, empty is ignored
    store.block_node("AABBCCDDEEFF")
    assert store.is_blocked("aabbccddeeff") is True
    assert store.is_blocked("000011112222") is False
    assert store.is_blocked("") is False
    assert store.blocked_prefixes() == {"aabbccddeeff"}
    # blocked_nodes joins registry names when known
    store.upsert_node("aabbccddeeff00112233445566778899", name="Alice")
    nodes = store.blocked_nodes()
    assert nodes == [{"prefix": "aabbccddeeff", "name": "Alice"}]
    # unblock
    store.unblock_node("aabbccddeeff")
    assert store.is_blocked("aabbccddeeff") is False
    assert store.blocked_prefixes() == set()
    assert store.blocked_nodes() == []
    store.close()


def test_packets_roundtrip(tmp_path):
    store = _store(tmp_path)
    store.add_packet(ts=100.0, layer="decoded", direction="in",
                     frame_type="CHANNEL_MSG_RECV_V3", sender="aabbccddeeff",
                     hops=2, snr=-7.5, channel_name="#bot", text="hello",
                     payload_json='{"payload": {}}')
    store.add_packet(ts=101.0, layer="raw", direction="in",
                     frame_type="CONTACT", size=42,
                     payload_json='{"raw_hex": "abcd"}')

    assert store.packet_count() == 2
    decoded = store.recent_packets(layer="decoded")
    assert len(decoded) == 1
    assert decoded[0]["sender"] == "aabbccddeeff"
    assert decoded[0]["hops"] == 2
    assert decoded[0]["snr"] == -7.5
    assert decoded[0]["channel_name"] == "#bot"
    assert "payload_json" not in decoded[0]   # kept out of list responses
    raw = store.recent_packets(layer="raw")
    assert len(raw) == 1 and raw[0]["size"] == 42
    assert store.recent_packets(limit=10)[0]["frame_type"] == "CONTACT"  # newest first
    store.close()


def test_packets_path_hash_size_roundtrip(tmp_path):
    store = _store(tmp_path)
    store.add_packet(ts=1.0, layer="decoded", direction="in",
                     frame_type="RX_LOG_DATA", hops=6, path_hash_size=2)
    store.add_packet(ts=2.0, layer="decoded", direction="in",
                     frame_type="OK")  # no path -> NULL
    rows = store.recent_packets(layer="decoded")
    by_type = {r["frame_type"]: r for r in rows}
    assert by_type["RX_LOG_DATA"]["path_hash_size"] == 2
    assert by_type["OK"]["path_hash_size"] is None
    store.close()


def test_path_hash_size_extraction_helpers():
    from core.store import _path_hash_size, _payload_path_hash_size
    # direct byte size wins (RX_LOG_DATA style)
    assert _path_hash_size({"path_hash_size": 2}) == 2
    assert _path_hash_size({"path_hash_size": 8}) == 8
    assert _path_hash_size({"path_hash_size": 0}) is None    # invalid size
    assert _path_hash_size({"path_hash_size": 9}) is None
    # hash mode is size - 1 (contact frames): 0 -> 1 byte, 1 -> 2 bytes...
    assert _path_hash_size({"out_path_hash_mode": 0}) == 1
    assert _path_hash_size({"path_hash_mode": 1}) == 2
    assert _path_hash_size({"hash_mode": 2}) == 3
    assert _path_hash_size({"path_hash_mode": 3}) == 4
    assert _path_hash_size({"out_path_hash_mode": -1}) is None  # flood / no path
    assert _path_hash_size({}) is None
    assert _path_hash_size({"path_hash_size": True}) is None   # bool is not a size
    # payload_json variants used for backfill
    assert _payload_path_hash_size('{"payload": {"path_hash_size": 2}}') == 2
    assert _payload_path_hash_size('{"payload": {"out_path_hash_mode": 1}}') == 2
    assert _payload_path_hash_size('{"payload": {}}') is None
    assert _payload_path_hash_size('not json') is None


def test_backfill_path_hash_size_from_payload(tmp_path):
    store = _store(tmp_path)
    store.add_packet(ts=1.0, layer="decoded", direction="in",
                     frame_type="RX_LOG_DATA",
                     payload_json='{"payload": {"path_hash_size": 3, "path_len": 4}}')
    store.add_packet(ts=2.0, layer="decoded", direction="in", frame_type="OK",
                     payload_json='{"payload": {}}')
    # simulate a pre-v4 / interrupted-migration database: the column exists
    # but rows are NULL although the payload carries the value, and the
    # backfill flag was never set.
    with store._conn:
        store._conn.execute("UPDATE packets SET path_hash_size = NULL")
        store._conn.execute("DELETE FROM meta WHERE k = 'packets_path_hash_backfilled'")
    store._backfill_path_hash_size()
    rows = store.recent_packets(layer="decoded")
    by_type = {r["frame_type"]: r for r in rows}
    assert by_type["RX_LOG_DATA"]["path_hash_size"] == 3
    assert by_type["OK"]["path_hash_size"] is None
    store.close()


def test_packets_prune(tmp_path):
    store = _store(tmp_path)
    for i in range(250):
        store.add_packet(ts=float(i), layer="decoded", direction="in",
                         frame_type="OK", max_rows=100)
    # Pruned to the newest 100 on the 200th insert, then 50 more added.
    assert store.packet_count() == 150
    assert store.recent_packets(limit=1)[0]["ts"] == 249.0  # newest survives
    store.close()


def test_packets_stats(tmp_path):
    store = _store(tmp_path)
    for i in range(3):
        store.add_packet(ts=float(i), layer="decoded", direction="in",
                         frame_type="OK")
    store.add_packet(ts=9.0, layer="raw", direction="in", frame_type="OK")
    stats = store.packet_stats()
    assert stats["total"] == 4
    assert stats["by_layer"] == {"decoded": 3, "raw": 1}
    assert stats["by_type"]["OK"] == 4
    store.close()


def test_raw_packet_profile_too_few_frames(tmp_path):
    store = _store(tmp_path)
    store.add_packet(ts=1.0, layer="raw", direction="in", frame_type="OK", size=10)
    profile = store.raw_packet_profile()
    assert profile["frames"] == 1
    assert profile["gaps"] is None and profile["size"] is None
    store.close()


def test_raw_packet_profile_sizes_and_gaps(tmp_path):
    store = _store(tmp_path)
    for ts, size in [(1.0, 20), (1.5, 50), (2.0, 100), (3.0, 300)]:
        store.add_packet(ts=ts, layer="raw", direction="in",
                         frame_type="CONTACT", size=size)
    profile = store.raw_packet_profile()
    assert profile["frames"] == 4
    assert profile["size"]["min"] == 20
    assert profile["size"]["max"] == 300
    assert profile["size"]["avg"] == 117.5
    assert profile["size"]["buckets"]["<32"]["count"] == 1
    assert profile["size"]["buckets"]["32-63"]["count"] == 1
    assert profile["size"]["buckets"]["64-127"]["count"] == 1
    assert profile["size"]["buckets"]["128-255"]["count"] == 0
    assert profile["size"]["buckets"][">=256"]["count"] == 1
    assert profile["span_seconds"] == 2.0
    assert profile["rate_fps"] == 2.0
    # gaps between consecutive frames: 0.5, 0.5, 1.0
    assert profile["gaps"]["min"] == 0.5
    assert profile["gaps"]["max"] == 1.0
    assert abs(profile["gaps"]["avg"] - 2 / 3) < 1e-3   # store rounds to 4 dp
    assert profile["gaps"]["p50"] == 0.5
    assert profile["gaps"]["p95"] == 1.0
    assert profile["by_type"]["CONTACT"] == 4
    store.close()


def test_packet_analysis_view(tmp_path):
    store = _store(tmp_path)
    now = time.time()
    # decoded frames inside the window
    for _ in range(2):
        store.add_packet(ts=now - 60, layer="decoded", direction="in",
                         frame_type="CONTACT", hops=2, snr=-8.0)
    store.add_packet(ts=now - 30, layer="decoded", direction="in",
                     frame_type="RX_LOG_DATA", hops=5, snr=4.0)
    # raw frames inside the window
    for _ in range(3):
        store.add_packet(ts=now - 45, layer="raw", direction="in",
                         frame_type="OK", size=10)
    # one frame outside the window (minimum window is 0.2 h = 720 s)
    store.add_packet(ts=now - 800, layer="decoded", direction="in",
                     frame_type="OLD", hops=0, snr=1.0)

    analysis = store.packet_analysis(hours=0.2)
    assert analysis["bucket_seconds"] == 300
    assert sum(b["decoded"] for b in analysis["timeline"]) == 3
    assert sum(b["raw"] for b in analysis["timeline"]) == 3
    assert analysis["decoded_total"] == 3       # OLD frame excluded
    assert analysis["raw_total"] == 3
    mix = {m["frame_type"]: m["n"] for m in analysis["mix_decoded"]}
    assert mix == {"CONTACT": 2, "RX_LOG_DATA": 1}
    hop_map = {h["hops"]: h["count"] for h in analysis["hops"]}
    assert hop_map == {"2": 2, "4+": 1}
    assert sum(p["count"] for p in analysis["snr"]) == 3
    store.close()


def test_path_hash_node_stats(tmp_path):
    store = _store(tmp_path)
    # unnamed RX_LOG frames (carry size, contribute only to the frame mix)
    for _ in range(3):
        store.add_packet(ts=1.0, layer="decoded", direction="in",
                         frame_type="RX_LOG_DATA", path_hash_size=1)
    for _ in range(2):
        store.add_packet(ts=1.0, layer="decoded", direction="in",
                         frame_type="RX_LOG_DATA", path_hash_size=2)
    # named senders: dominant size per sender decides the node counts
    for _ in range(4):
        store.add_packet(ts=2.0, layer="decoded", direction="in",
                         frame_type="RX_LOG_DATA", sender="logan",
                         path_hash_size=2)
    for _ in range(2):
        store.add_packet(ts=3.0, layer="decoded", direction="in",
                         frame_type="RX_LOG_DATA", sender="alice",
                         path_hash_size=2)
    for _ in range(5):
        store.add_packet(ts=4.0, layer="decoded", direction="in",
                         frame_type="RX_LOG_DATA", sender="bob",
                         path_hash_size=1)
    store.add_packet(ts=5.0, layer="decoded", direction="in",
                     frame_type="RX_LOG_DATA", sender="cara",
                     path_hash_size=3)

    stats = store.path_hash_node_stats()
    # frames: 1B x8 (3 unnamed + bob 5), 2B x8 (2 unnamed + logan 4 + alice 2),
    #         3B x1 (cara)
    assert stats["frames_total"] == 17
    assert stats["frames"] == {1: 8, 2: 8, 3: 1}
    # nodes: bob=1B, logan/alice=2B, cara=3B
    assert stats["node_total"] == 4
    assert stats["nodes"] == {1: 1, 2: 2, 3: 1}
    store.close()


def test_path_hash_node_stats_tie_prefers_larger_size(tmp_path):
    store = _store(tmp_path)
    # equal frame counts for 1B and 2B from the same sender -> 2B wins
    store.add_packet(ts=1.0, layer="decoded", direction="in",
                     frame_type="RX_LOG_DATA", sender="dana", path_hash_size=1)
    store.add_packet(ts=2.0, layer="decoded", direction="in",
                     frame_type="RX_LOG_DATA", sender="dana", path_hash_size=2)
    stats = store.path_hash_node_stats()
    assert stats["node_total"] == 1
    assert stats["nodes"] == {2: 1}
    store.close()


def test_stats_row_includes_packets(tmp_path):
    store = _store(tmp_path)
    store.add_packet(ts=1.0, layer="decoded", direction="in", frame_type="OK")
    assert store.stats_row()["packets"] == 1
    store.close()


def test_overrides_and_channel_reply(tmp_path):
    store = _store(tmp_path)
    assert store.channel_reply_override("#bot") is None
    store.set_channel_reply_override("#bot", False)
    assert store.channel_reply_override("#bot") is False
    store.set_channel_reply_override("#bot", None)
    assert store.channel_reply_override("#bot") is None
    store.set_global_mute(True)
    assert store.global_mute() is True
    store.set_global_mute(False)
    assert store.global_mute() is False
    store.close()
