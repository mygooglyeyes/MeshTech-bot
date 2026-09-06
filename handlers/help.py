"""Help - lists the bot's commands and explains the modifier words."""
from __future__ import annotations

from typing import List, Optional

from core.format import fmt_table
from core.models import HandlerResult
from .base import Handler

# Byte budget for the brief help so it always fits one LoRa packet
# (MeshCore's per-message text limit). Kept in one place beside the
# formatter; the router's chunker would split it otherwise.
_BRIEF_BUDGET = 133


def _fit_words(prefix_parts: List[str], canned_words: str,
               suffix_parts: List[str]) -> str:
    """Join parts, shrinking the plain-word list until the line fits.

    ``prefix_parts`` and ``suffix_parts`` are fixed; the words list between
    them is truncated word-by-word (prefix preserved, order kept) and then
    dropped entirely if even that is not enough. Returns a line guaranteed
    within _BRIEF_BUDGET when the fixed parts fit; caller handles the rest.
    """
    sep_b = len(" | ".encode("utf-8"))
    fixed_b = sum(len(p.encode("utf-8")) for p in prefix_parts + suffix_parts)
    fixed_b += sep_b * max(0, len(prefix_parts) + len(suffix_parts) - 1)

    words_budget = _BRIEF_BUDGET - fixed_b - len("words: ".encode("utf-8")) \
        - (sep_b if prefix_parts else 0)
    words_out = ""
    for word in canned_words.split():
        candidate = (words_out + " " + word).strip()
        if len(candidate.encode("utf-8")) > max(0, words_budget):
            break
        words_out = candidate

    parts = list(prefix_parts)
    if words_out:
        parts.append("words: " + words_out)
    parts.extend(suffix_parts)
    return " | ".join(parts)


def format_brief_help(command_words: str, canned_words: str,
                      admin_hint: str) -> str:
    """One-packet brief help: commands + plain words + the x-modifier.

    The line opens with "My Commands - " followed by the bang-prefixed
    commands. No key:value label prefix: some mesh console clients
    render a leading label as their own stats widget instead of the
    text, so visible content always leads.

    Degradation ladder when space runs out: shrink the plain-word list,
    drop it, drop the admin hint, and only if the command list alone
    exceeds the packet (pathological) hard-ellipsize - the reply never
    exceeds one LoRa packet.
    """
    cmds = "My Commands - " + " ".join("!" + w
                                        for w in command_words.split())
    xhint = "add x for more (e.g. !pathx)"
    base = ([cmds, xhint] if command_words.strip() else [xhint])

    line = _fit_words(base, canned_words,
                      [admin_hint] if admin_hint else [])
    if len(line.encode("utf-8")) <= _BRIEF_BUDGET:
        return line

    # Words did not fit: drop them, keep the admin hint if it fits.
    parts = list(base)
    if admin_hint:
        parts.append(admin_hint)
    line = " | ".join(parts)
    if len(line.encode("utf-8")) <= _BRIEF_BUDGET:
        return line

    line = " | ".join(base)
    if len(line.encode("utf-8")) <= _BRIEF_BUDGET:
        return line

    # Last resort: the command list itself is too long - cut it to size.
    cmds_cut = cmds
    while cmds_cut and len((" | ".join([cmds_cut, xhint])).encode("utf-8")) \
            > _BRIEF_BUDGET:
        cmds_cut = cmds_cut[:-1]
    return " | ".join([cmds_cut, xhint]) if cmds_cut else xhint


class HelpHandler(Handler):
    name = "help"
    keywords = ["help"]
    description = "List commands and usage"
    scope = "both"
    access = "public"
    priority = 90

    async def handle(self, ctx) -> Optional[HandlerResult]:
        settings = ctx.service.settings
        kind = ctx.msg.kind

        # Per-keyword visibility: handlers may declare keyword_scope /
        # keyword_access overrides (e.g. !nodes DM-only, !path public).
        pairs = []  # (handler, keyword, scope, access)
        for handler in ctx.service.registry:
            if handler.name == "canned":
                continue  # listed separately as plain words
            scope_map = getattr(handler, "keyword_scope", {})
            access_map = getattr(handler, "keyword_access", {})
            for kw in handler.keywords:
                scope = scope_map.get(kw, handler.scope)
                if scope not in ("both", kind):
                    continue
                access = access_map.get(kw, handler.access)
                if access == "admin" and not ctx.is_admin:
                    continue
                pairs.append((handler, kw, scope, access))

        canned_keywords = [kw for rule in settings.replies for kw in rule.keywords]
        canned_list = " ".join(sorted(set(canned_keywords)))
        canned_words = canned_list or "(none)"

        if ctx.verbosity == "brief":
            command_words = " ".join(sorted({kw for _, kw, _, _ in pairs}))
            admin_hint = ("admin: reload shutdown diag up"
                          if ctx.is_admin else "")
            data = format_brief_help(command_words, canned_list, admin_hint)
            return HandlerResult(kind="text", data=data)

        rows = []
        for handler, kw, scope, access in sorted(pairs, key=lambda p: p[0].priority):
            where = ("DM only" if scope == "dm" else
                     "channel/DM" if scope == "both" else "channel")
            if access == "admin":
                where += " (admin)"
            rows.append(["!" + kw, handler.description, where])
        table = fmt_table(["Command", "What it does", "Where"], rows,
                          col_caps=[16, 46, 10])
        lines = list(table)
        lines.append("")
        lines.append("Append 'x' to most commands for the extended version "
                     "(e.g. !nodes x, !pathx, !path <node> x) - glued on is fine "
                     "(!pathx == !path x).")
        lines.append("Examples: !nodes   |   !nodes x   |   !pathx   |   !path K7ABC x")
        lines.append("Plain-word answers: " + canned_words)
        return HandlerResult(kind="text", data="\n".join(lines))
