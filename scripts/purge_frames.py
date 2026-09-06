#!/usr/bin/env python3
"""Remove companion bookkeeping frames from the packet database.

The bot no longer captures companion bookkeeping (NEXT_CONTACT contact-list
dumps, NO_MORE_MSGS, CURRENT_TIME) - it is bot<->companion housekeeping, not
mesh traffic, and it once made up ~88% of the packet history, crowding out
real frames. This tool cleans up history captured by older versions.

Safety first: it always writes a one-file backup of the database before
touching anything, unless you explicitly say --no-backup.

Usage:
    python scripts/purge_frames.py                  # backup + purge + vacuum
    python scripts/purge_frames.py --dry-run        # just show what would go
    python scripts/purge_frames.py --type RX_LOG_DATA   # purge a custom type
    python scripts/purge_frames.py --db data/bot.db     # explicit database

Run it while the bot is up or stopped; only the final VACUUM (space reclaim)
needs a quiet database, and the script tells you if it had to skip that.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

# Make ``core`` importable when run as ``python scripts/purge_frames.py``
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# Companion bookkeeping frame types purged by default - mirrors the bot's
# capture skip-list (see core/client.py _CAPTURE_SKIP).
DEFAULT_FRAME_TYPES = ("NEXT_CONTACT", "CONTACT", "NO_MORE_MSGS", "CURRENT_TIME")


def _fmt_size(nbytes: float) -> str:
    for unit in ("bytes", "KB", "MB", "GB"):
        if nbytes < 1024 or unit == "GB":
            return f"{nbytes:,.1f} {unit}" if unit != "bytes" else f"{nbytes:,.0f} {unit}"
        nbytes /= 1024
    return f"{nbytes:,.1f} GB"


def plan_purge(conn: sqlite3.Connection, frame_types: List[str],
               layer: str = "decoded") -> Dict[str, int]:
    """Counts per frame type that would be removed (no changes made)."""
    counts: Dict[str, int] = {}
    for ftype in frame_types:
        row = conn.execute(
            "SELECT COUNT(*) FROM packets WHERE layer = ? AND frame_type = ?",
            (layer, ftype)).fetchone()
        counts[ftype] = int(row[0])
    return counts


def purge_frames(db_path: str, frame_types: List[str],
                 layer: str = "decoded", backup: bool = True,
                 vacuum: bool = True, dry_run: bool = False) -> Dict:
    """Back up, delete the given frame types, optionally vacuum.

    Returns a summary dict; raises nothing - errors are reported via the
    ``error`` key so main() can print them friendlily.
    """
    summary: Dict = {"db_path": db_path, "dry_run": dry_run,
                     "purged": {}, "total_purged": 0}
    db_file = Path(db_path)
    if not db_file.is_file():
        summary["error"] = f"database not found: {db_path}"
        return summary
    summary["size_before"] = db_file.stat().st_size

    conn = sqlite3.connect(db_path, timeout=10.0)
    try:
        counts = plan_purge(conn, frame_types, layer)
        summary["purged"] = counts
        summary["total_purged"] = sum(counts.values())
        row = conn.execute("SELECT COUNT(*) FROM packets").fetchone()
        summary["rows_before"] = int(row[0])

        if dry_run or summary["total_purged"] == 0:
            summary["rows_after"] = summary["rows_before"]
            summary["size_after"] = summary["size_before"]
            return summary

        if backup:
            backup_path = (db_file.parent / "backups" /
                           f"pre-purge-{datetime.now():%Y%m%d-%H%M%S}.db")
            backup_path.parent.mkdir(parents=True, exist_ok=True)
            dest = sqlite3.connect(str(backup_path))
            try:
                conn.backup(dest)
            finally:
                dest.close()
            summary["backup_path"] = str(backup_path)

        with conn:
            for ftype, count in counts.items():
                if count:
                    conn.execute(
                        "DELETE FROM packets WHERE layer = ? AND frame_type = ?",
                        (layer, ftype))
        row = conn.execute("SELECT COUNT(*) FROM packets").fetchone()
        summary["rows_after"] = int(row[0])

        if vacuum:
            try:
                conn.execute("VACUUM")
                summary["vacuum_ok"] = True
            except sqlite3.OperationalError as exc:
                summary["vacuum_ok"] = False
                summary["vacuum_error"] = str(exc)
        summary["size_after"] = db_file.stat().st_size
    finally:
        conn.close()
    return summary


def _cli(db_path: str, frame_types: List[str], layer: str,
         backup: bool, vacuum: bool, dry_run: bool) -> int:
    print(f"Packet database: {db_path}")
    print(f"Frame types to purge (layer '{layer}'): {', '.join(frame_types)}")
    if dry_run:
        print("Dry run - nothing will be changed.\n")
    result = purge_frames(db_path, frame_types, layer=layer, backup=backup,
                          vacuum=vacuum, dry_run=dry_run)
    if "error" in result:
        print(f"ERROR: {result['error']}")
        return 1

    print()
    for ftype, count in result["purged"].items():
        print(f"  {ftype:<22} {count:>7} row(s) to remove" if dry_run
              else f"  {ftype:<22} {count:>7} row(s) removed")
    print(f"  {'-' * 38}")
    if dry_run:
        print(f"  {result['total_purged']:>7} row(s) would be removed "
              f"of {result['rows_before']} total")
        return 0
    if result["total_purged"] == 0:
        print("Nothing to purge - the database is already clean.")
        return 0
    print(f"  {result['total_purged']:>7} row(s) removed; "
          f"{result['rows_after']} remain")
    if "backup_path" in result:
        print(f"\nBackup written to: {result['backup_path']}")
    if result.get("vacuum_ok"):
        print(f"Space reclaimed: {_fmt_size(result['size_before'])} -> "
              f"{_fmt_size(result['size_after'])}")
    elif vacuum:
        print(f"\nNOTE: VACUUM skipped (database busy: {result.get('vacuum_error')}).")
        print("Space is freed anyway on next vacuum; to reclaim it now, stop the")
        print("bot (sudo systemctl stop meshtech-bot) and run this tool again.")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Purge companion bookkeeping frames from the packet database.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument("--config", default="config.yaml",
                        help="bot config.yaml (used for storage.db_path)")
    parser.add_argument("--db", default=None,
                        help="packet database file (overrides config.yaml)")
    parser.add_argument("--type", action="append", default=None,
                        metavar="FRAME_TYPE",
                        help="additional frame type to purge (repeatable)")
    parser.add_argument("--layer", choices=["decoded", "raw"], default="decoded",
                        help="which packet layer to purge")
    parser.add_argument("--no-backup", action="store_true",
                        help="skip the safety backup (not recommended)")
    parser.add_argument("--no-vacuum", action="store_true",
                        help="skip reclaiming disk space afterwards")
    parser.add_argument("--dry-run", action="store_true",
                        help="show what would be removed, change nothing")
    args = parser.parse_args(argv)

    db_path = args.db
    if not db_path:
        try:
            from core.config import load
            db_path = load(args.config).storage.db_path
        except Exception as exc:
            print(f"Could not read {args.config}: {exc}")
            print("Pass --db <path> to point at a packet database directly.")
            return 1

    frame_types = list(DEFAULT_FRAME_TYPES)
    for extra in args.type or []:
        if extra not in frame_types:
            frame_types.append(extra)

    return _cli(db_path, frame_types, args.layer,
                backup=not args.no_backup, vacuum=not args.no_vacuum,
                dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
