"""2byte - what share of the mesh uses 2-byte path hashes.

MeshCore devices address the nodes along a route with hashes of a fixed
size chosen by the sender: 1-byte, 2-byte or longer (3-4 byte). Every
captured frame that carries a path records its ``path_hash_size`` (bytes
per hop), so this handler answers "how many nodes use 2-byte paths?" from
the bot's own packet capture - as a single bar line of ASCII shading characters, e.g.::

    2-byte path nodes: [▓▓▓▓▓▓▓▒▒▒▒▒▒▒▒▒▒▒▒] 36% (8/22 of registered nodes)

  * 0xB2 = 178 = ▓ (dark shade) marks the percentage part
  * 0xB1 = 177 = ▒ (medium shade) fills the remainder up to 100%

Identity note: RX_LOG_DATA radio logs usually arrive WITHOUT a sender
label, while channel/advert frames name theirs - so the report counts
*named* nodes (per-sender dominant size) and falls back to the frame mix
when too few nodes are identified to be meaningful.
"""
from __future__ import annotations

from typing import Optional

from core.models import HandlerResult
from .base import Handler

# bar width in characters; each column is a slice of the percentage
_BAR_WIDTH = 20
# prefer the per-node basis once at least this many nodes are identified
_NODE_MIN_SAMPLE = 3

# CP437 shading glyphs: 0xB2 (178) = filled share, 0xB1 (177) = remainder
_FILL = "\u2593"   # ▓ dark shade
_EMPTY = "\u2592"  # ▒ medium shade


def _bar(pct: float, width: int = _BAR_WIDTH) -> str:
    """Bar for a percentage: 178/▓ for the share, 177/▒ up to 100%."""
    filled = min(width, max(0, round(pct / 100.0 * width)))
    return _FILL * filled + _EMPTY * (width - filled)


def format_2byte_report(stats: dict) -> str:
    """One line: ASCII bar + share of 2-byte path-hash users.

    Reads store.path_hash_node_stats() output.
    """
    frames_total = stats.get("frames_total", 0)
    if not frames_total:
        return "No path-hash data yet - capture builds as frames with routes arrive."

    node_total = stats.get("node_total", 0)
    if node_total >= _NODE_MIN_SAMPLE:
        basis = "nodes"
        two = stats.get("nodes", {}).get(2, 0)
        total = node_total
    else:
        basis = "frames"
        two = stats.get("frames", {}).get(2, 0)
        total = frames_total

    if not total:
        return "No path-hash data yet - capture builds as frames with routes arrive."
    pct = 100.0 * two / total
    basis_text = "of registered nodes" if basis == "nodes" else "by frames"
    return (f"2-byte path nodes: [{_bar(pct)}] {round(pct)}% "
            f"({two}/{total} {basis_text})")


class TwoByteHandler(Handler):
    name = "2byte"
    keywords = ["2byte"]
    description = "Share of nodes using 2-byte path hashes (ASCII bar)"
    scope = "both"
    access = "public"
    priority = 96

    async def handle(self, ctx) -> Optional[HandlerResult]:
        stats = ctx.store.path_hash_node_stats()
        return HandlerResult(kind="text",
                             data=format_2byte_report(stats))
