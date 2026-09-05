#!/usr/bin/env python3
"""Export the captured packet database to CSV files for offline analysis.

Every run writes into a fresh timestamped folder (default:
``data/exports/packets-<YYYYmmdd-HHMMSS>/``):

  packets.csv              - every captured frame, one row per frame
  summary_hourly.csv       - frames per hour (decoded / raw / total)
  summary_frame_types.csv  - frame-type mix per layer (count + %)
  summary_hops.csv         - hop distribution of decoded frames
  summary_path_hash.csv    - path-hash size mix (1-byte / 2-byte / 3+ byte)
  summary_snr.csv          - average / min / max SNR per hour
  summary_senders.csv      - most active senders

The CSV files open straight in Excel / LibreOffice / Numbers and are easy
to load in pandas:

    import pandas as pd
    pkts = pd.read_csv("packets.csv")

Usage:
    python scripts/export_packets.py                  # everything, from config.yaml's db
    python scripts/export_packets.py --hours 24       # only the last 24 hours
    python scripts/export_packets.py --layer raw      # raw-layer frames only
    python scripts/export_packets.py --limit 10000    # newest 10,000 frames
    python scripts/export_packets.py --full           # also include the full payload JSON column
    python scripts/export_packets.py --db data/bot.db --out my_export
"""
from __future__ import annotations

import argparse
import csv
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

# Make ``core`` importable when run as ``python scripts/export_packets.py``
# (sys.path points at the script's folder, not the project root).
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

_PACKET_COLUMNS = ["id", "ts_iso", "ts", "layer", "direction", "frame_type",
                   "sender", "hops", "path_hash_size", "snr",
                   "channel_name", "text", "size"]

_HOP_BUCKETS: List[Any] = [0, 1, 2, 3, "4+"]

# CSV cells starting with these characters are interpreted as formulas by
# Excel/LibreOffice/Google Sheets. Mesh traffic (message text, sender
# names) is attacker-controlled, so such cells are neutralised with a
# leading apostrophe (CWE-1236). Numeric cells are unaffected.
_CSV_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _csv_cell(value: Any) -> Any:
    if isinstance(value, str) and value.startswith(_CSV_FORMULA_PREFIXES):
        return "'" + value
    return value


def _iso(ts: Optional[float]) -> str:
    if ts is None:
        return ""
    try:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
    except (OverflowError, OSError, ValueError):
        return ""


def _hour_key(ts: Optional[float]) -> str:
    """Local-time hour label, sortable as text: '2026-09-04 21:00'."""
    if ts is None:
        return ""
    try:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:00")
    except (OverflowError, OSError, ValueError):
        return ""


def export_packets(db_path: str, out_dir: str,
                   hours: Optional[float] = None,
                   layer: Optional[str] = None,
                   limit: Optional[int] = None,
                   include_payload: bool = False,
                   include_summaries: bool = True) -> Dict[str, Any]:
    """Dump the packets table to ``out_dir`` as CSVs.

    ``hours`` limits to the last N hours of traffic; ``layer`` to
    ``'decoded'`` or ``'raw'``; ``limit`` keeps only the N most recent
    frames. Returns ``{"rows": n, "files": [paths...]}``.
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    where: List[str] = []
    params: List[Any] = []
    if hours is not None:
        where.append("ts >= ?")
        params.append(datetime.now().timestamp() - float(hours) * 3600)
    if layer in ("decoded", "raw"):
        where.append("layer = ?")
        params.append(layer)
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""

    # The CSV is always written chronologically (oldest first). When a limit
    # is applied it must select the NEWEST frames first, so wrap that select
    # in a subquery and re-sort chronologically.
    if limit:
        params.append(int(limit))
        order_sql = "SELECT * FROM (SELECT %s FROM packets%s ORDER BY ts DESC, id DESC LIMIT ?) ORDER BY ts ASC, id ASC"
    else:
        order_sql = "SELECT %s FROM packets%s ORDER BY ts ASC, id ASC"

    columns = _PACKET_COLUMNS + (["payload_json"] if include_payload else [])
    select_cols = [c for c in columns if c != "ts_iso"]  # ts_iso is derived

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        cursor = conn.execute(
            order_sql % (", ".join(select_cols), where_sql),
            params)

        rows_written = 0
        hourly: Dict[str, Dict[str, int]] = {}
        type_counts: Dict[tuple, int] = {}
        layer_totals: Dict[str, int] = {}
        hop_counts: Dict[Any, int] = {b: 0 for b in _HOP_BUCKETS}
        hops_reported = 0
        snr_by_hour: Dict[str, List[float]] = {}
        sender_counts: Dict[str, int] = {}
        hash_sizes: Dict[int, int] = {}   # bytes-per-path-hash -> frames
        hash_frames = 0
        packet_path = out / "packets.csv"
        with open(packet_path, "w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(columns)
            for row in cursor:
                values = list(row)
                ts = row["ts"]
                # insert the human-readable timestamp right after the id
                values.insert(1, _iso(ts))
                writer.writerow([_csv_cell(v) for v in values])
                rows_written += 1
                if not include_summaries:
                    continue

                layer_name = row["layer"] if row["layer"] in ("decoded", "raw") \
                    else "decoded"
                frame_type = row["frame_type"] or ""
                type_counts[(layer_name, frame_type)] = \
                    type_counts.get((layer_name, frame_type), 0) + 1
                layer_totals[layer_name] = layer_totals.get(layer_name, 0) + 1

                hk = _hour_key(ts)
                if hk:
                    slot = hourly.setdefault(hk, {"decoded": 0, "raw": 0, "total": 0})
                    slot[layer_name] += 1
                    slot["total"] += 1

                if layer_name == "decoded" and row["hops"] is not None:
                    hops_reported += 1
                    h = row["hops"]
                    bucket: Any = h if h in _HOP_BUCKETS else "4+"
                    hop_counts[bucket] += 1

                if row["snr"] is not None:
                    snr_by_hour.setdefault(hk, []).append(float(row["snr"]))

                if row["sender"]:
                    sender_counts[str(row["sender"])] = \
                        sender_counts.get(str(row["sender"]), 0) + 1

                size_val = row["path_hash_size"]
                if isinstance(size_val, int) and size_val >= 1:
                    hash_sizes[size_val] = hash_sizes.get(size_val, 0) + 1
                    hash_frames += 1
    finally:
        conn.close()

    if not include_summaries:
        return {"rows": rows_written, "files": [str(packet_path)]}

    files: List[str] = [str(packet_path)]

    # --- frames per hour --------------------------------------------------
    summary_path = out / "summary_hourly.csv"
    with open(summary_path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["hour", "decoded", "raw", "total"])
        for hk in sorted(hourly):
            slot = hourly[hk]
            writer.writerow([hk, slot["decoded"], slot["raw"], slot["total"]])
    files.append(str(summary_path))

    # --- frame-type mix per layer ------------------------------------------
    summary_path = out / "summary_frame_types.csv"
    with open(summary_path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["layer", "frame_type", "count", "pct_of_layer"])
        ordered = sorted(type_counts.items(), key=lambda kv: (-kv[1], kv[0]))
        for (layer_name, frame_type), count in ordered:
            total = layer_totals.get(layer_name, 0) or 1
            writer.writerow([layer_name, frame_type, count,
                             round(100.0 * count / total, 1)])
    files.append(str(summary_path))

    # --- hop distribution (decoded frames that report hops) ------------------
    summary_path = out / "summary_hops.csv"
    with open(summary_path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["hops", "count", "pct_of_reported"])
        for bucket in _HOP_BUCKETS:
            count = hop_counts.get(bucket, 0)
            if count:
                writer.writerow([bucket, count,
                                 round(100.0 * count / (hops_reported or 1), 1)])
    files.append(str(summary_path))

    # --- SNR per hour --------------------------------------------------------
    summary_path = out / "summary_snr.csv"
    with open(summary_path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["hour", "frames_with_snr", "avg_snr", "min_snr", "max_snr"])
        for hk in sorted(snr_by_hour):
            values = snr_by_hour[hk]
            writer.writerow([hk, len(values),
                             round(sum(values) / len(values), 2),
                             round(min(values), 2), round(max(values), 2)])
    files.append(str(summary_path))

    # --- most active senders --------------------------------------------------
    summary_path = out / "summary_senders.csv"
    with open(summary_path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["sender", "frames"])
        for sender, count in sorted(sender_counts.items(),
                                    key=lambda kv: (-kv[1], kv[0])):
            writer.writerow([_csv_cell(sender), count])
    files.append(str(summary_path))

    # --- path hash size: how senders address path nodes ------------------------
    # bytes per path hash: 1 = 1-byte addresses, 2 = 2-byte, 3+ = longer
    summary_path = out / "summary_path_hash.csv"
    with open(summary_path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["path_hash_bytes", "bucket", "frames",
                         "pct_of_frames_with_path"])
        for size in sorted(hash_sizes):
            count = hash_sizes[size]
            bucket = "1 byte" if size == 1 else \
                ("2 bytes" if size == 2 else "3+ bytes")
            writer.writerow([size, bucket, count,
                             round(100.0 * count / (hash_frames or 1), 1)])
    files.append(str(summary_path))

    return {"rows": rows_written, "files": files}


def _cli(db_path: str, out_dir: str, hours: Optional[float],
         layer: Optional[str], limit: Optional[int],
         include_payload: bool, include_summaries: bool) -> int:
    print(f"Exporting packets from: {db_path}")
    result = export_packets(
        db_path=db_path, out_dir=out_dir, hours=hours, layer=layer,
        limit=limit, include_payload=include_payload,
        include_summaries=include_summaries)
    print(f"Exported {result['rows']} frames into {out_dir}:")
    for path in result["files"]:
        print(f"  - {Path(path).name}")
    if result["rows"] == 0:
        print("(no frames matched - check --hours / --layer / --limit)")
        return 1
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Export the captured packets table to CSV files.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument("--config", default="config.yaml",
                        help="bot config.yaml (used for storage.db_path)")
    parser.add_argument("--db", default=None,
                        help="packet database file (overrides config.yaml)")
    parser.add_argument("--out", default=None,
                        help="output folder (default: data/exports/packets-<timestamp>)")
    parser.add_argument("--hours", type=float, default=None,
                        help="only the last N hours of traffic")
    parser.add_argument("--layer", choices=["decoded", "raw"], default=None,
                        help="only decoded or raw frames")
    parser.add_argument("--limit", type=int, default=None,
                        help="only the N most recent frames")
    parser.add_argument("--full", action="store_true",
                        help="also write the full payload JSON column")
    parser.add_argument("--packets-only", action="store_true",
                        help="skip the summary CSV files")
    args = parser.parse_args(argv)

    db_path = args.db
    if not db_path:
        try:
            from core.config import load
            settings = load(args.config)
            db_path = settings.storage.db_path
        except Exception as exc:
            print(f"Could not read {args.config}: {exc}")
            print("Pass --db <path> to point at a packet database directly.")
            return 1

    if args.out:
        out_dir = args.out
    else:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        out_dir = str(Path(db_path).resolve().parent / "exports" /
                      f"packets-{stamp}")

    return _cli(db_path, out_dir, args.hours, args.layer, args.limit,
                args.full, not args.packets_only)


if __name__ == "__main__":
    sys.exit(main())
