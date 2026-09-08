"""Mesh health - admin command for a quick offender check from the field.

    !health [x]   - top flood-score offenders (DM, admin)

Surface-only doctrine: the command reports, it never blocks. One compact
message; the extended version adds each offender's component stats.
"""
from __future__ import annotations

from typing import Optional

from core.format import rel_time
from core.meshhealth import mesh_health
from core.models import HandlerResult
from .base import Handler


class MeshHealthHandler(Handler):
    name = "meshhealth"
    keywords = ["health"]
    description = "Msh health / offendrs"
    scope = "dm"
    access = "admin"
    priority = 80

    async def handle(self, ctx) -> Optional[HandlerResult]:
        if not ctx.is_admin:
            return None                      # silent for non-admin senders
        extended = "x" in (ctx.args or []) or ctx.verbosity == "extended"
        report = mesh_health(self.service.store, limit=3)
        senders = report.get("senders") or []
        if not senders:
            return HandlerResult(kind="text",
                                 data="Mesh health: no traffic scored yet.")
        lines = []
        if extended:
            for r in senders:
                lines.append(
                    f"{r['name']} {r['score']}/100 - "
                    f"burst {r['burst']['per_min']}/min, "
                    f"share {r['share']['pct']}%, "
                    f"repeat {r['repeat']['count']}x")
        else:
            tops = " ".join(f"{r['name']} {r['score']}" for r in senders)
            lines.append(f"Mesh health: {tops}")
        lines.append("(report only - nothing is blocked automatically)")
        return HandlerResult(kind="text", data="\n".join(lines))
