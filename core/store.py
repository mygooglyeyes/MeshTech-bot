"""SQLite storage.

Everything the bot remembers lives in one small database file:
  * nodes     - public keys + node names (friendly replies), last seen/SNR
  * messages  - every inbound/outbound message with hops, SNR, timestamps
                (source of propagation-time statistics)
  * routes    - snapshots of route information per node over time
  * overrides - runtime switches set from the web dashboard
  * meta      - schema version bookkeeping
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .models import MsgRecord

# Versioned migrations: (version, [sql statements]).
_MIGRATIONS: List[tuple] = [
    (1, [
        """
        CREATE TABLE IF NOT EXISTS nodes (
            pubkey       TEXT PRIMARY KEY,
            prefix       TEXT NOT NULL,
            name         TEXT,
            first_seen   REAL NOT NULL,
            last_seen    REAL NOT NULL,
            last_snr     REAL,
            lat          REAL,
            lon          REAL,
            source       TEXT,
            route_hops   INTEGER,
            route_summary TEXT
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_nodes_prefix ON nodes(prefix)",
        "CREATE INDEX IF NOT EXISTS idx_nodes_last_seen ON nodes(last_seen)",
        """
        CREATE TABLE IF NOT EXISTS messages (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            kind          TEXT NOT NULL,
            direction     TEXT NOT NULL,
            channel_name  TEXT,
            sender_prefix TEXT,
            text          TEXT,
            sender_ts     REAL,
            recv_ts       REAL NOT NULL,
            hops          INTEGER,
            snr           REAL
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_messages_recv ON messages(recv_ts)",
        "CREATE INDEX IF NOT EXISTS idx_messages_kind ON messages(kind)",
        "CREATE INDEX IF NOT EXISTS idx_messages_channel ON messages(channel_name)",
        "CREATE INDEX IF NOT EXISTS idx_messages_sender ON messages(sender_prefix)",
        """
        CREATE TABLE IF NOT EXISTS routes (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            node_prefix TEXT NOT NULL,
            hops        INTEGER,
            summary     TEXT,
            observed_at REAL NOT NULL
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_routes_node ON routes(node_prefix, observed_at)",
        """
        CREATE TABLE IF NOT EXISTS overrides (
            key   TEXT PRIMARY KEY,
            value TEXT
        )
        """,
        "CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)",
    ]),
    (2, [
        """
        CREATE TABLE IF NOT EXISTS packets (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            ts           REAL NOT NULL,
            layer        TEXT NOT NULL,       -- 'decoded' | 'raw'
            direction    TEXT NOT NULL,       -- 'in' | 'out'
            frame_type   TEXT NOT NULL,       -- e.g. CHANNEL_MSG_RECV_V3, CONTACT
            sender       TEXT,                -- pubkey prefix / node name
            hops         INTEGER,
            snr          REAL,
            channel_name TEXT,
            text         TEXT,
            size         INTEGER,             -- payload size in bytes (raw layer)
            payload_json TEXT                 -- full decoded fields (JSON)
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_packets_ts ON packets(ts)",
        "CREATE INDEX IF NOT EXISTS idx_packets_type ON packets(frame_type)",
        "CREATE INDEX IF NOT EXISTS idx_packets_layer ON packets(layer)",
    ]),
    (3, [
        # Channel messages carry only an embedded display name ("Name: body");
        # keep it so channel traffic can be attributed to registry nodes for
        # per-node link-quality history.
        "ALTER TABLE messages ADD COLUMN sender_name TEXT",
        # Route snapshots may carry the received SNR of the advert, so a
        # node's route history doubles as a link-quality timeline.
        "ALTER TABLE routes ADD COLUMN snr REAL",
    ]),
    (4, [
        # Size (in bytes) of each path hash the sender embedded in a frame -
        # how the sender addresses path nodes: 1-byte, 2-byte or 3+ byte
        # hashes (RX_LOG_DATA exposes it directly; contact frames carry the
        # hash mode, size = mode + 1). NULL when the frame carries no path.
        "ALTER TABLE packets ADD COLUMN path_hash_size INTEGER",
    ]),
    (5, [
        # Free-text annotation for a station, edited from the dashboard node
        # table ("note") - survives bot restarts like any other node data.
        "ALTER TABLE nodes ADD COLUMN note TEXT",
    ]),
]


def _payload_path_hash_size(payload_json: Optional[str]) -> Optional[int]:
    """Best-effort path hash byte size from a stored payload_json string.

    Returns the per-hop hash size in bytes (1..8) or None. Used to backfill
    rows captured before the column existed.
    """
    if not payload_json:
        return None
    try:
        import json
        data = json.loads(payload_json)
    except Exception:
        return None
    payload = data.get("payload") if isinstance(data, dict) else None
    if not isinstance(payload, dict):
        return None
    return _path_hash_size(payload)


def _path_hash_size(payload: Dict[str, Any]) -> Optional[int]:
    """Per-hop path hash size in bytes (1..8) from a decoded event payload.

    Sources, in order of preference:
      * ``path_hash_size``   - already in bytes (RX_LOG_DATA radio logs)
      * ``*_hash_mode``      - meshcore hash mode 0..3, size = mode + 1
                               (mode -1 = flood / no path -> None)
    """
    for key in ("path_hash_size", "hash_size", "path_hash_bytes"):
        value = payload.get(key)
        if isinstance(value, bool):
            continue
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        if 1 <= number <= 8:
            return number
    for key in ("out_path_hash_mode", "path_hash_mode", "hash_mode",
                "reply_path_hash_mode"):
        value = payload.get(key)
        if isinstance(value, bool):
            continue
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        if 0 <= number <= 3:
            return number + 1
    return None


def _now() -> float:
    return time.time()


class Store:
    """Thin, defensive wrapper around a SQLite database (stdlib only)."""

    def __init__(self, db_path: str):
        self.db_path = str(db_path)
        parent = Path(self.db_path).parent
        if str(parent) not in ("", "."):
            parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        # WAL's natural partner: fsync only at checkpoints, not per commit.
        # On a Pi's SD card this removes the per-message fsync stall while
        # staying crash-safe (worst case on power cut: the last commits are
        # lost, never a corrupted database).
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._migrate()

    # ------------------------------------------------------------------ schema

    def _migrate(self) -> None:
        version = self._conn.execute("PRAGMA user_version").fetchone()[0]
        for target, statements in _MIGRATIONS:
            if target <= version:
                continue
            with self._conn:
                for statement in statements:
                    self._conn.execute(statement)
                self._conn.execute(f"PRAGMA user_version={int(target)}")
            version = target
        # One-time backfill (idempotent): rows captured before the
        # path_hash_size column existed still carry the value inside
        # payload_json. A meta flag makes this survive interrupted runs.
        if version >= 4:
            self._backfill_path_hash_size()

    def _backfill_path_hash_size(self) -> None:
        """Fill path_hash_size from existing payload_json (best-effort)."""
        flag = "packets_path_hash_backfilled"
        try:
            done = self._conn.execute(
                "SELECT v FROM meta WHERE k = ?", (flag,)).fetchone()
            if done:
                return
        except Exception:
            return
        rows = []
        try:
            rows = self._conn.execute(
                "SELECT id, payload_json FROM packets WHERE path_hash_size IS NULL "
                "AND (payload_json LIKE '%path_hash_size%' "
                "     OR payload_json LIKE '%hash_mode%')").fetchall()
        except Exception:
            rows = []
        updates = []
        for r in rows:
            size = _payload_path_hash_size(r["payload_json"])
            if size is not None:
                updates.append((size, r["id"]))
        if updates:
            try:
                with self._conn:
                    self._conn.executemany(
                        "UPDATE packets SET path_hash_size = ? WHERE id = ?", updates)
            except Exception:
                return  # leave the flag unset so the next open retries
        try:
            with self._conn:
                self._conn.execute(
                    "INSERT OR REPLACE INTO meta (k, v) VALUES (?, '1')", (flag,))
        except Exception:
            pass

    def close(self) -> None:
        try:
            self._conn.close()
        except sqlite3.Error:
            pass

    # ------------------------------------------------------------------ nodes

    def upsert_node(self, pubkey: str, name: Optional[str] = None,
                    snr: Optional[float] = None, lat: Optional[float] = None,
                    lon: Optional[float] = None, source: Optional[str] = None,
                    route_hops: Optional[int] = None,
                    route_summary: Optional[str] = None,
                    ts: Optional[float] = None) -> None:
        """Add or refresh a node. Existing non-empty values are kept."""
        pubkey = pubkey.lower()
        ts = ts if ts is not None else _now()
        prefix = pubkey[:12]
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO nodes (pubkey, prefix, name, first_seen, last_seen,
                                   last_snr, lat, lon, source, route_hops, route_summary)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(pubkey) DO UPDATE SET
                    last_seen  = excluded.last_seen,
                    name       = CASE WHEN excluded.name IS NOT NULL
                                       AND trim(excluded.name) != ''
                                      THEN excluded.name ELSE nodes.name END,
                    last_snr   = CASE WHEN excluded.last_snr IS NOT NULL
                                      THEN excluded.last_snr ELSE nodes.last_snr END,
                    lat        = CASE WHEN excluded.lat IS NOT NULL
                                      THEN excluded.lat ELSE nodes.lat END,
                    lon        = CASE WHEN excluded.lon IS NOT NULL
                                      THEN excluded.lon ELSE nodes.lon END,
                    source     = CASE WHEN excluded.source IS NOT NULL
                                      THEN excluded.source ELSE nodes.source END,
                    route_hops = CASE WHEN excluded.route_hops IS NOT NULL
                                      THEN excluded.route_hops ELSE nodes.route_hops END,
                    route_summary = CASE WHEN excluded.route_summary IS NOT NULL
                                         THEN excluded.route_summary
                                         ELSE nodes.route_summary END
                """,
                (pubkey, prefix, name, ts, ts, snr, lat, lon, source,
                 route_hops, route_summary),
            )

    def get_node(self, key_or_prefix: str) -> Optional[Dict[str, Any]]:
        key = key_or_prefix.lower()
        row = self._conn.execute(
            "SELECT * FROM nodes WHERE pubkey = ? OR prefix = ? "
            "OR pubkey LIKE ? ORDER BY last_seen DESC LIMIT 1",
            (key, key, key + "%"),
        ).fetchone()
        return dict(row) if row else None

    def resolve_name(self, key_or_prefix: str) -> Optional[str]:
        node = self.get_node(key_or_prefix)
        if node and node.get("name"):
            return node["name"]
        return None

    def find_node(self, query: str) -> Optional[Dict[str, Any]]:
        """Find a node by prefix, full pubkey, or exact (case-insensitive) name."""
        node = self.get_node(query)
        if node is not None:
            return node
        row = self._conn.execute(
            "SELECT * FROM nodes WHERE lower(name) = lower(?) "
            "ORDER BY last_seen DESC LIMIT 1", (query.strip(),),
        ).fetchone()
        return dict(row) if row else None

    def list_nodes(self, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM nodes ORDER BY last_seen DESC"
        if limit:
            sql += f" LIMIT {int(limit)}"
        return [dict(r) for r in self._conn.execute(sql).fetchall()]

    def set_node_note(self, key_or_prefix: str, note: Optional[str]) -> bool:
        """Attach a free-text annotation to a node (dashboard note field).

        Empty/whitespace clears the note. Returns False when no node
        matches the prefix/key/name.
        """
        node = self.get_node(key_or_prefix)
        if node is None:
            return False
        note = (note or "").strip()[:200] or None
        with self._conn:
            self._conn.execute(
                "UPDATE nodes SET note = ? WHERE pubkey = ?",
                (note, node["pubkey"]))
        return True

    def node_count(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]

    # ------------------------------------------------------------------ messages

    def add_message(self, record: MsgRecord) -> None:
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO messages (kind, direction, channel_name, sender_prefix,
                                      text, sender_ts, recv_ts, hops, snr, sender_name)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (record.kind, record.direction, record.channel_name,
                 record.sender_prefix, record.text[:2000], record.sender_ts,
                 record.recv_ts, record.hops, record.snr, record.sender_name),
            )

    def query_messages(self, channel: Optional[str] = None, kind: Optional[str] = None,
                       max_hops: Optional[int] = None, limit: int = 50) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM messages WHERE 1=1"
        params: list = []
        if channel:
            sql += " AND channel_name = ?"
            params.append(channel)
        if kind:
            sql += " AND kind = ?"
            params.append(kind)
        if max_hops is not None:
            sql += " AND hops IS NOT NULL AND hops <= ?"
            params.append(int(max_hops))
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(int(limit))
        return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def message_count(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]

    def totals(self) -> Dict[str, int]:
        """Counts per kind/direction for dashboard + diag command."""
        out: Dict[str, int] = {}
        for row in self._conn.execute(
            "SELECT kind, direction, COUNT(*) AS n FROM messages GROUP BY kind, direction"
        ).fetchall():
            out[f"{row['direction']}_{row['kind']}"] = row["n"]
        out["total"] = sum(out.values())
        return out

    def hop_distribution(self) -> List[Dict[str, Any]]:
        """Count inbound messages per hop bucket (0,1,2,3,4+)."""
        buckets = {0: 0, 1: 0, 2: 0, 3: 0, "4+": 0}
        rows = self._conn.execute(
            "SELECT hops FROM messages WHERE hops IS NOT NULL AND direction='in'"
        ).fetchall()
        for row in rows:
            h = row["hops"]
            if h <= 3:
                buckets[h] += 1
            else:
                buckets["4+"] += 1
        return [{"hops": str(k), "count": v} for k, v in buckets.items() if v > 0]

    def per_channel_counts(self, hours: int = 24) -> List[Dict[str, Any]]:
        cutoff = _now() - hours * 3600
        rows = self._conn.execute(
            "SELECT channel_name, COUNT(*) AS n FROM messages "
            "WHERE recv_ts >= ? AND channel_name IS NOT NULL GROUP BY channel_name "
            "ORDER BY n DESC", (cutoff,),
        ).fetchall()
        return [dict(r) for r in rows]

    def propagation_stats(self, prefix: Optional[str] = None,
                          channel: Optional[str] = None,
                          max_age_days: int = 14) -> Dict[str, Any]:
        """Delay + hop stats for DM traffic from one node, or one channel.

        Propagation delay = arrival time - sender timestamp. Node clocks are
        not perfectly synced, so treat these as relative numbers/trends.
        """
        cutoff = _now() - max_age_days * 86400
        sql = ("SELECT sender_ts, recv_ts, hops FROM messages "
               "WHERE recv_ts >= ? AND direction='in' AND sender_ts IS NOT NULL")
        params: list = [cutoff]
        if prefix:
            sql += " AND sender_prefix = ?"
            params.append(prefix.lower())
        if channel:
            sql += " AND channel_name = ?"
            params.append(channel)
        rows = self._conn.execute(sql, params).fetchall()

        delays: List[float] = []
        hop_counts: Dict[str, int] = {}
        for row in rows:
            delay = row["recv_ts"] - row["sender_ts"]
            if 0 <= delay <= 86400 * max_age_days:
                delays.append(delay)
            h = row["hops"]
            bucket = str(h) if h is not None and h <= 4 else ("4+" if h is not None else "?")
            hop_counts[bucket] = hop_counts.get(bucket, 0) + 1

        out: Dict[str, Any] = {"count": len(rows), "delay_count": len(delays)}
        if delays:
            out["delay_min"] = min(delays)
            out["delay_max"] = max(delays)
            out["delay_avg"] = sum(delays) / len(delays)
        else:
            out["delay_min"] = out["delay_max"] = out["delay_avg"] = None
        out["hop_distribution"] = hop_counts
        return out

    # ------------------------------------------------------------------ routes

    def route_changed(self, prefix: str, hops: Optional[int],
                      summary: Optional[str], snr: Optional[float] = None) -> bool:
        """True when this route differs from the last stored one.

        A changed SNR also counts as a change (link quality moved), so SNR
        trends appear in the node's route/link history.
        """
        row = self._conn.execute(
            "SELECT hops, summary, snr FROM routes WHERE node_prefix = ? "
            "ORDER BY observed_at DESC, id DESC LIMIT 1", (prefix.lower(),),
        ).fetchone()
        if row is None:
            return True
        if row["hops"] != hops or row["summary"] != summary:
            return True
        if snr is not None and row["snr"] != snr:
            return True
        return False

    def add_route(self, prefix: str, hops: Optional[int], summary: Optional[str],
                  snr: Optional[float] = None, ts: Optional[float] = None) -> None:
        prefix = prefix.lower()
        if not self.route_changed(prefix, hops, summary, snr):
            return
        with self._conn:
            self._conn.execute(
                "INSERT INTO routes (node_prefix, hops, summary, snr, observed_at) "
                "VALUES (?,?,?,?,?)",
                (prefix, hops, summary, snr, ts if ts is not None else _now()),
            )

    def route_history(self, prefix: str, limit: int = 8) -> List[Dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM routes WHERE node_prefix = ? "
            "ORDER BY observed_at DESC, id DESC LIMIT ?",
            (prefix.lower(), int(limit)),
        ).fetchall()
        return [dict(r) for r in rows]

    def link_history(self, prefix: str, limit: int = 30) -> List[Dict[str, Any]]:
        """Per-node hop/SNR observations over time - the link-quality history.

        Sources, newest first:
          * dm      - inbound DMs from this node (solid: pubkey prefix)
          * channel - inbound channel messages whose embedded sender name
                      matches this node's registry name (best effort)
          * advert  - route snapshots from contact/advert syncs
        """
        node = self.get_node(prefix)
        if node is None:
            return []
        pfx = node["prefix"]
        name = node.get("name") or ""
        rows = self._conn.execute(
            """
            SELECT * FROM (
              SELECT recv_ts AS ts, hops, snr, 'dm' AS source
                FROM messages
               WHERE direction = 'in' AND sender_prefix = ?
                 AND (hops IS NOT NULL OR snr IS NOT NULL)
              UNION ALL
              SELECT recv_ts AS ts, hops, snr, 'channel' AS source
                FROM messages
               WHERE direction = 'in' AND kind = 'channel'
                 AND lower(sender_name) = lower(?)
                 AND (hops IS NOT NULL OR snr IS NOT NULL)
              UNION ALL
              SELECT observed_at AS ts, hops, snr, 'advert' AS source
                FROM routes
               WHERE node_prefix = ?
                 AND (hops IS NOT NULL OR snr IS NOT NULL)
            ) ORDER BY ts DESC, source LIMIT ?
            """,
            (pfx, name, pfx, int(limit)),
        ).fetchall()
        return [dict(r) for r in rows]

    def rxlog_copies(self, ts: float, hops: Optional[int], window: float = 2.5,
                     limit: int = 200) -> List[Dict[str, Any]]:
        """RX_LOG copies of the radio frame a just-decoded message arrived in.

        The companion logs every received radio frame (RX_LOG_DATA); a flood
        heard several times logs one row per hearing, and each hearing's
        ``path`` is the previous one plus one appended relay. Matching the
        decoded message (its hop count + arrival time) to one of those rows
        therefore exposes, per relay: the path order, the SNR of each heard
        retransmission and the arrival offsets. Returns parsed payloads sorted
        by time - empty when nothing in the log correlates.
        """
        import json as _json

        def _parse(row):
            try:
                payload = _json.loads(row[1])
            except Exception:
                return None
            inner = payload.get("payload") if isinstance(payload, dict) else payload
            if not isinstance(inner, dict):
                return None
            try:
                plen = int(inner["path_len"]) if inner.get("path_len") is not None else None
            except (TypeError, ValueError):
                plen = None
            try:
                snr = float(inner["snr"]) if inner.get("snr") is not None else None
            except (TypeError, ValueError):
                snr = None
            try:
                hsize = int(inner["path_hash_size"]) if inner.get("path_hash_size") is not None else 1
            except (TypeError, ValueError):
                hsize = 1
            return {"ts": row[0], "pkt_hash": inner.get("pkt_hash"),
                    "plen": plen, "snr": snr, "path": inner.get("path") or "",
                    "hash_size": hsize, "typename": inner.get("payload_typename")}

        lo, hi = ts - window, ts + window
        rows = self._conn.execute(
            "SELECT ts, payload_json FROM packets "
            "WHERE layer='decoded' AND frame_type='RX_LOG_DATA' "
            "AND ts BETWEEN ? AND ? ORDER BY ts LIMIT ?",
            (lo, hi, int(limit))).fetchall()
        parsed = [p for r in rows if (p := _parse(r)) is not None]
        if not parsed:
            return []
        # Anchor: the row that matches the decoded message (hop count + time).
        anchors = [p for p in parsed if hops is None or p["plen"] == hops]
        pool = anchors or parsed
        best = min(pool, key=lambda p: abs(p["ts"] - ts))
        if best.get("pkt_hash") is None:
            return [best]
        return [p for p in parsed if p["pkt_hash"] == best["pkt_hash"]]

    # ------------------------------------------------------------------ overrides

    def get_override(self, key: str) -> Optional[str]:
        row = self._conn.execute("SELECT value FROM overrides WHERE key = ?",
                                 (key,)).fetchone()
        return row["value"] if row else None

    def set_override(self, key: str, value: Optional[str]) -> None:
        with self._conn:
            if value is None:
                self._conn.execute("DELETE FROM overrides WHERE key = ?", (key,))
            else:
                self._conn.execute(
                    "INSERT INTO overrides (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, value),
                )

    def channel_reply_override(self, channel_name: str) -> Optional[bool]:
        raw = self.get_override(f"channel_reply:{channel_name}")
        if raw is None:
            return None
        return raw in ("1", "true", "yes")

    def set_channel_reply_override(self, channel_name: str, enabled: Optional[bool]) -> None:
        if enabled is None:
            self.set_override(f"channel_reply:{channel_name}", None)
        else:
            self.set_override(f"channel_reply:{channel_name}", "1" if enabled else "0")

    def global_mute(self) -> bool:
        return self.get_override("global_mute") in ("1", "true", "yes")

    def set_global_mute(self, muted: bool) -> None:
        self.set_override("global_mute", "1" if muted else "0")

    # ------------------------------------------------------------------ blocked nodes

    _BLOCK_PREFIX = "blocked:"

    def block_node(self, prefix: str) -> None:
        """Ignore all messages from this node (12-hex pubkey prefix)."""
        prefix = (prefix or "").lower().strip()
        if prefix:
            self.set_override(f"{self._BLOCK_PREFIX}{prefix}", "1")

    def unblock_node(self, prefix: str) -> None:
        prefix = (prefix or "").lower().strip()
        if prefix:
            self.set_override(f"{self._BLOCK_PREFIX}{prefix}", None)

    def is_blocked(self, prefix: Optional[str]) -> bool:
        if not prefix:
            return False
        return self.get_override(
            f"{self._BLOCK_PREFIX}{prefix.lower().strip()}") in ("1", "true", "yes")

    def blocked_prefixes(self) -> set:
        rows = self._conn.execute(
            "SELECT key FROM overrides WHERE key LIKE ?",
            (self._BLOCK_PREFIX + "%",)).fetchall()
        return {row["key"][len(self._BLOCK_PREFIX):] for row in rows}

    def blocked_nodes(self) -> List[Dict[str, Any]]:
        """Blocked prefixes joined with current registry names (if known)."""
        prefixes = self.blocked_prefixes()
        if not prefixes:
            return []
        rows = self._conn.execute(
            "SELECT pubkey, prefix, name FROM nodes WHERE prefix IN (%s)"
            % ",".join("?" * len(prefixes)), tuple(prefixes)).fetchall()
        by_prefix = {r["prefix"]: dict(r) for r in rows}
        return [{
            "prefix": prefix,
            "name": (by_prefix[prefix]["name"] if prefix in by_prefix
                     else None),
        } for prefix in sorted(prefixes)]

    # ------------------------------------------------------------------ misc

    # ------------------------------------------------------------------ packets

    def add_packet(self, ts: float, layer: str, direction: str, frame_type: str,
                   sender: Optional[str] = None, hops: Optional[int] = None,
                   snr: Optional[float] = None, channel_name: Optional[str] = None,
                   text: Optional[str] = None, size: Optional[int] = None,
                   payload_json: Optional[str] = None,
                   path_hash_size: Optional[int] = None,
                   max_rows: int = 200000) -> None:
        """Store one captured frame. Prunes to max_rows every 200 inserts.
        Returns the new row id (for JSONL cross-referencing)."""
        if not hasattr(self, "_packet_inserts"):
            self._packet_inserts = 0
        with self._conn:
            cursor = self._conn.execute(
                """
                INSERT INTO packets (ts, layer, direction, frame_type, sender,
                                     hops, snr, channel_name, text, size,
                                     payload_json, path_hash_size)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (ts, layer, direction, frame_type, sender, hops, snr,
                 channel_name, (text or "")[:2000], size, payload_json,
                 path_hash_size),
            )
        self._packet_inserts += 1
        if self._packet_inserts % 200 == 0:
            self._prune_packets(max_rows)
        return int(cursor.lastrowid)

    def _prune_packets(self, max_rows: int) -> None:
        with self._conn:
            self._conn.execute(
                "DELETE FROM packets WHERE id <= (SELECT MAX(id) FROM packets) - ?",
                (int(max_rows),),
            )

    def recent_packets(self, layer: Optional[str] = None,
                       limit: int = 50) -> List[Dict[str, Any]]:
        sql = "SELECT id, ts, layer, direction, frame_type, sender, hops, snr, " \
              "channel_name, text, size, path_hash_size FROM packets WHERE 1=1"
        params: list = []
        if layer in ("decoded", "raw"):
            sql += " AND layer = ?"
            params.append(layer)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(int(limit))
        return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def packet_count(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM packets").fetchone()[0]

    def packet_stats(self) -> Dict[str, Any]:
        """Summary counts for the dashboard/diag: total, per layer, per type."""
        out: Dict[str, Any] = {"total": self.packet_count()}
        by_layer: Dict[str, int] = {}
        by_type: Dict[str, int] = {}
        for row in self._conn.execute(
            "SELECT layer, frame_type, COUNT(*) AS n FROM packets "
            "GROUP BY layer, frame_type").fetchall():
            by_type[row["frame_type"]] = by_type.get(row["frame_type"], 0) + row["n"]
            by_layer[row["layer"]] = by_layer.get(row["layer"], 0) + row["n"]
        out["by_layer"] = by_layer
        out["by_type"] = dict(sorted(by_type.items(), key=lambda kv: -kv[1])[:20])
        return out

    def raw_packet_profile(self, max_frames: int = 5000) -> Dict[str, Any]:
        """Packet size + inter-frame timing profile of the companion link.

        Computed over the most recent ``max_frames`` raw-layer frames:
        size stats + distribution buckets, receive rate, and the gap
        (inter-arrival) distribution between consecutive frames.
        """
        rows = self._conn.execute(
            "SELECT ts, size, frame_type FROM packets WHERE layer='raw' "
            "ORDER BY id DESC LIMIT ?", (int(max_frames),)).fetchall()
        rows = list(reversed(rows))  # chronological
        out: Dict[str, Any] = {"frames": len(rows)}
        if len(rows) < 2:
            out["size"] = None
            out["gaps"] = None
            out["by_type"] = {}
            out["span_seconds"] = 0.0
            out["rate_fps"] = 0.0
            return out

        span = max(rows[-1]["ts"] - rows[0]["ts"], 0.001)
        out["span_seconds"] = round(span, 1)
        out["rate_fps"] = round(len(rows) / span, 3)

        sizes = [r["size"] for r in rows if r["size"] is not None]
        if sizes:
            bucket_edges = [("<32", 32), ("32-63", 64), ("64-127", 128),
                            ("128-255", 256), (">=256", None)]
            counts = {name: 0 for name, _ in bucket_edges}
            for s in sizes:
                for name, edge in bucket_edges:
                    if edge is None or s < edge:
                        counts[name] += 1
                        break
            out["size"] = {
                "min": min(sizes), "max": max(sizes),
                "avg": round(sum(sizes) / len(sizes), 1),
                "buckets": {name: {"count": n, "pct": round(100 * n / len(sizes), 1)}
                             for name, n in counts.items()},
            }
        else:
            out["size"] = None

        gaps = [rows[i]["ts"] - rows[i - 1]["ts"] for i in range(1, len(rows))]
        gaps = [g for g in gaps if g >= 0]
        if gaps:
            ordered = sorted(gaps)

            def percentile(p: float) -> float:
                return ordered[min(len(ordered) - 1, int(p / 100 * len(ordered)))]

            out["gaps"] = {
                "min": round(min(gaps), 4), "avg": round(sum(gaps) / len(gaps), 4),
                "p50": round(percentile(50), 4), "p95": round(percentile(95), 4),
                "max": round(max(gaps), 4),
            }
        else:
            out["gaps"] = None

        by_type: Dict[str, int] = {}
        for r in rows:
            by_type[r["frame_type"]] = by_type.get(r["frame_type"], 0) + 1
        out["by_type"] = dict(sorted(by_type.items(), key=lambda kv: -kv[1])[:12])
        return out

    def packet_analysis(self, hours: float = 24.0) -> Dict[str, Any]:
        """Analysis view over captured packets: traffic timeline, frame-type
        mix, hop distribution and SNR trend.

        The timeline/SNR buckets adapt to the requested window (5 min for
        <=1h, 30 min for <=6h, 1h for <=24h, 6h beyond) so any zoom shows
        a useful number of buckets.
        """
        hours = max(0.2, float(hours))
        cutoff = _now() - hours * 3600
        if hours <= 1:
            span = 300
        elif hours <= 6:
            span = 1800
        elif hours <= 24:
            span = 3600
        else:
            span = 21600
        out: Dict[str, Any] = {"hours": round(hours, 1),
                               "bucket_seconds": span}

        # --- per-bucket traffic timeline (decoded vs raw)
        rows = self._conn.execute(
            "SELECT CAST(ts / ? AS INTEGER) * ? AS bucket, layer, COUNT(*) AS n "
            "FROM packets WHERE ts >= ? GROUP BY bucket, layer",
            (span, span, cutoff)).fetchall()
        per_bucket: Dict[int, Dict[str, int]] = {}
        for row in rows:
            bucket_data = per_bucket.setdefault(row["bucket"],
                                                {"decoded": 0, "raw": 0})
            layer = row["layer"] if row["layer"] in ("decoded", "raw") else "decoded"
            bucket_data[layer] += row["n"]
        now = _now()
        first_bucket = int(cutoff // span) * span
        timeline: List[Dict[str, Any]] = []
        bucket = first_bucket
        while bucket <= now:
            row = per_bucket.get(bucket, {"decoded": 0, "raw": 0})
            timeline.append({"bucket": bucket, "decoded": row["decoded"],
                             "raw": row["raw"]})
            bucket += span
        out["timeline"] = timeline

        # --- frame-type mix per layer (top types + "other")
        out["mix_decoded"], out["decoded_total"] = self._frame_type_mix(
            "decoded", cutoff, cap=8)
        out["mix_raw"], out["raw_total"] = self._frame_type_mix(
            "raw", cutoff, cap=6)

        # --- hop distribution (decoded frames with hop counts)
        hop_rows = self._conn.execute(
            "SELECT hops FROM packets WHERE layer='decoded' AND hops IS NOT NULL "
            "AND ts >= ?", (cutoff,)).fetchall()
        buckets: Dict[Any, int] = {0: 0, 1: 0, 2: 0, 3: 0, "4+": 0}
        for row in hop_rows:
            h = row["hops"]
            if isinstance(h, int) and h <= 3:
                buckets[h] += 1
            else:
                buckets["4+"] += 1
        out["hops"] = [{"hops": str(k), "count": v} for k, v in buckets.items()
                        if v > 0]

        # --- SNR trend per bucket (avg/min/max over frames carrying SNR)
        snr_rows = self._conn.execute(
            "SELECT CAST(ts / ? AS INTEGER) * ? AS bucket, AVG(snr) avg_snr, "
            "MIN(snr) min_snr, MAX(snr) max_snr, COUNT(*) AS n "
            "FROM packets WHERE ts >= ? AND snr IS NOT NULL "
            "GROUP BY bucket ORDER BY bucket", (span, span, cutoff)).fetchall()
        snr_by_bucket = {r["bucket"]: r for r in snr_rows}
        snr_trend: List[Dict[str, Any]] = []
        bucket = first_bucket
        while bucket <= now:
            row = snr_by_bucket.get(bucket)
            if row is not None:
                snr_trend.append({
                    "bucket": bucket,
                    "avg": round(float(row["avg_snr"]), 2),
                    "min": round(float(row["min_snr"]), 2),
                    "max": round(float(row["max_snr"]), 2),
                    "count": row["n"],
                })
            bucket += span
        out["snr"] = snr_trend
        return out

    def _frame_type_mix(self, layer: str, cutoff: float,
                        cap: int) -> tuple:
        rows = self._conn.execute(
            "SELECT frame_type, COUNT(*) AS n FROM packets "
            "WHERE layer = ? AND ts >= ? GROUP BY frame_type ORDER BY n DESC",
            (layer, cutoff)).fetchall()
        items = [dict(r) for r in rows]
        total = sum(item["n"] for item in items)
        if len(items) > cap:
            top, rest = items[:cap], items[cap:]
            top.append({"frame_type": "other", "n": sum(r["n"] for r in rest)})
            items = top
        return items, total

    def path_hash_node_stats(self) -> Dict[str, Any]:
        """Path-hash usage across captured frames and named nodes.

        The bot records ``path_hash_size`` (bytes per path hash) on frames
        that carry a path: 1-byte, 2-byte or 3+ byte node hashes. Senders
        are not always labelled (RX_LOG_DATA often arrives anonymous), so
        this returns BOTH views and the caller decides what to report:

          * ``nodes``      - distinct senders bucketed by their dominant
                             (most frequent) path hash size
          * ``frames``     - raw frame counts per size (named or not)
        """
        rows = self._conn.execute(
            "SELECT sender, path_hash_size, COUNT(*) AS n FROM packets "
            "WHERE layer = 'decoded' AND path_hash_size IS NOT NULL "
            "GROUP BY sender, path_hash_size").fetchall()

        frames: Dict[int, int] = {}
        node_modes: Dict[str, int] = {}      # sender -> dominant size
        node_best: Dict[str, int] = {}       # sender -> count of that size
        for row in rows:
            size = int(row["path_hash_size"])
            frames[size] = frames.get(size, 0) + row["n"]
            sender = row["sender"]
            if not sender or not str(sender).strip():
                continue
            sender = str(sender).strip().lower()
            count = row["n"]
            if sender not in node_modes or \
                    count > node_best.get(sender, 0) or \
                    (count == node_best.get(sender, 0) and
                     size > node_modes.get(sender, 0)):
                node_modes[sender] = size
                node_best[sender] = count

        nodes: Dict[int, int] = {}
        for size in node_modes.values():
            nodes[size] = nodes.get(size, 0) + 1
        return {
            "frames_total": sum(frames.values()),
            "frames": frames,
            "node_total": len(node_modes),
            "nodes": nodes,
        }

    def stats_row(self) -> Dict[str, Any]:
        return {
            "nodes": self.node_count(),
            "messages": self.message_count(),
            "totals": self.totals(),
            "hop_distribution": self.hop_distribution(),
            "channels_24h": self.per_channel_counts(hours=24),
            "packets": self.packet_count(),
        }
