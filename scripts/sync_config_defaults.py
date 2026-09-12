#!/usr/bin/env python3
"""Sync documented default settings into the live config.yaml (deploy helper).

Brett's rule (2026-09-12): every documented setting should be EXPLICITLY
present in the box's /opt/meshtech-bot/config.yaml - no "just add a line".
The deploy never overwrites the live config (it holds unique secrets and
radio settings), so instead this script ADDS what is missing:

  - every documented setting key from config.example.yaml that is absent
    from the live file is inserted ACTIVE ("key: value"), with its
    explaining comment lines kept above it - including keys that are
    commented out in the example (that form documents the default);
  - sub-blocks (e.g. modules -> weather) are inserted under their own
    sub-header, whole, when the live file lacks them;
  - settings already present in the live file (at any level) are left
    byte-identical - never duplicated, never re-ordered;
  - secret-ish keys (password/token/secret/seed) are NEVER auto-inserted;
  - a section the live file lacks entirely is appended at the end;
  - a backup of the live config is written next to it before any change.

Only lowercase snake_case keys are treated as settings (prose lines like
"# Weather: ..." in the example are comments, not keys).

Usage:
    python3 scripts/sync_config_defaults.py \
        --example config.example.yaml --live /opt/meshtech-bot/config.yaml
    (add --dry-run to print without changing anything)

Exit codes: 0 = OK (synced or nothing to do), 1 = refused (error reported;
the deploy continues to config validation, which still gates the restart).
"""
from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

SECRET_SUBSTRINGS = ("password", "token", "secret", "seed")

KEY_LINE = re.compile(r"^(\s*)([a-z_][a-z0-9_]*):(.*)$")   # snake_case keys only
SECTION = re.compile(r"^([a-z_][a-z0-9_]*):\s*$")           # top-level header


def _is_secret(key: str) -> bool:
    k = key.lower()
    return any(s in k for s in SECRET_SUBSTRINGS)


def _clean_value(rest: str) -> str:
    """The value part after 'key:' with comments stripped - scalar defaults."""
    return rest.split(" #", 1)[0].strip()


def _ws(line: str) -> str:
    """The line's leading whitespace, verbatim - used to keep an inserted
    setting at its original nesting level."""
    return line[:len(line) - len(line.lstrip())]


@dataclass
class Setting:
    key: str
    line: str                       # the ACTIVE line to insert
    comments: list[str] = field(default_factory=list)


@dataclass
class Block:
    """A named sub-block: header line + child settings (one level deep)."""
    key: str
    header: str                     # e.g. "  weather:"
    children: list[Setting] = field(default_factory=list)


def parse_example(text: str) -> dict[str, list]:
    """section name -> ordered list[Setting | Block]."""
    sections: dict[str, list] = {}
    current: str | None = None
    pending_comments: list[str] = []
    skip_block = False              # inside a commented-out block (# alerts: ...)
    block: Block | None = None      # active sub-block being filled

    def close_block():
        nonlocal block
        if block is not None and current is not None and block.children:
            sections[current].append(block)
        block = None

    for raw in text.splitlines():
        line = raw.rstrip()

        if SECTION.match(line):                       # top-level header
            close_block()
            current = line.rstrip(":").strip()
            sections.setdefault(current, [])
            pending_comments = []
            skip_block = False
            continue

        if line.lstrip().startswith("#"):             # comment line
            body = line.lstrip()[1:].lstrip()
            m = KEY_LINE.match(body)
            if m and not body.lstrip().startswith("#"):
                key, value = m.group(2), _clean_value(m.group(3))
                if value == "":
                    close_block()
                    skip_block = True                 # commented-out block
                elif current is not None and not skip_block and not _is_secret(key):
                    # documented default: insert ACTIVE, original indent kept
                    e = Setting(key=key, line=f"{_ws(line)}{key}: {value}",
                                comments=pending_comments)
                    pending_comments = []
                    if block is not None:
                        block.children.append(e)
                    else:
                        sections[current].append(e)
                else:
                    pending_comments = []
            elif current is not None and not skip_block:
                pending_comments.append(line)         # prose for the next key
            continue

        m = KEY_LINE.match(line)
        if m:
            key, value = m.group(2), _clean_value(m.group(3))
            if value == "":                            # nested block header
                if current is None or not line.startswith((" ", "\t")):   # never a nested block at col 0
                    continue
                close_block()
                block = Block(key=key, header=line)
                pending_comments = []
                skip_block = False
                continue
            if current is None or _is_secret(key):
                pending_comments = []
                continue
            e = Setting(key=key, line=f"{_ws(line)}{key}: {value}",
                        comments=pending_comments)
            pending_comments = []
            if block is not None:
                block.children.append(e)
            else:
                sections[current].append(e)
            continue

        pending_comments = []      # list items / continuation lines: drop
    close_block()
    return sections


def live_key_names(live_text: str) -> set[str]:
    """Every snake_case key present ANYWHERE in the live file (active or
    commented) - a key present at any level is never auto-inserted."""
    keys: set[str] = set()
    for line in live_text.splitlines():
        stripped = line.strip()
        for candidate in (stripped,
                          stripped[1:].lstrip() if stripped.startswith("#") else ""):
            m = KEY_LINE.match(candidate)
            if m:
                keys.add(m.group(2))
    return keys


def build_plan(example: dict[str, list],
               present: set[str]) -> dict[str, list]:
    """section -> ordered Setting/Block items to insert (filtered: every
    key they carry is absent from the live file)."""
    plan: dict[str, list] = {}
    for section, nodes in example.items():
        items: list = []
        for node in nodes:
            if isinstance(node, Setting):
                if node.key not in present:
                    items.append(node)
            else:
                children = [c for c in node.children if c.key not in present]
                if children:
                    items.append(Block(key=node.key, header=node.header,
                                       children=children))
        if items:
            plan[section] = items
    return plan


def _serialize(item) -> list[str]:
    if isinstance(item, Setting):
        return item.comments + [item.line]
    out = [item.header]
    for child in item.children:
        out.extend(child.comments)
        out.append(child.line)
    return out


def _section_spans(lines: list[str]) -> dict[str, tuple[int, int]]:
    """section name -> (start, end) line-index span in the live file."""
    starts = [i for i, l in enumerate(lines) if SECTION.match(l)]
    spans: dict[str, tuple[int, int]] = {}
    for idx, s in enumerate(starts):
        end = starts[idx + 1] if idx + 1 < len(starts) else len(lines)
        spans[SECTION.match(lines[s]).group(1)] = (s, end)
    return spans


def _sub_block_end(lines: list[str], sub_at: int, section_end: int) -> int:
    """First content line after the sub-header whose indent is <= the
    sub-header's (i.e. where the sub-block's children end)."""
    base = len(lines[sub_at]) - len(lines[sub_at].lstrip())
    for j in range(sub_at + 1, section_end):
        l = lines[j]
        if not l.strip() or l.strip().startswith("#"):
            continue
        if len(l) - len(l.lstrip()) <= base:
            return j
    return section_end


def apply_plan(live_text: str, plan: dict[str, list]) -> str:
    """Insert plan items into the live text via bottom-up line splices.

    Plain settings go right under their section header; blocks go under
    their existing sub-header's children, or at the end of the section
    when the live file lacks the sub-block; planned sections missing from
    the live file are appended at the end of the file."""
    lines = live_text.splitlines()
    spans = _section_spans(lines)

    inserts: list[tuple[int, list[str]]] = []   # (position, lines)
    consumed: set[str] = set()
    for sec, items in plan.items():
        if sec not in spans:
            continue
        consumed.add(sec)
        start, end = spans[sec]
        under_header: list[str] = []
        for item in items:
            if isinstance(item, Setting):
                under_header.extend(_serialize(item))
        if under_header:
            inserts.append((start + 1, under_header))
        for item in items:
            if not isinstance(item, Block):
                continue
            sub_at = None
            for j in range(start + 1, end):
                m = KEY_LINE.match(lines[j])
                if m and m.group(2) == item.key and _clean_value(m.group(3)) == "":
                    sub_at = j
                    break
            if sub_at is None:
                inserts.append((end, _serialize(item)))
            else:
                inserts.append((_sub_block_end(lines, sub_at, end),
                                _serialize(item)))

    merged: dict[int, list[str]] = {}
    for pos, ins in inserts:
        merged.setdefault(pos, []).extend(ins)
    for pos in sorted(merged, reverse=True):
        lines[pos:pos] = merged[pos]

    tail: list[str] = []
    for sec, items in plan.items():
        if sec in consumed:
            continue
        if tail:
            tail.append("")
        tail.append(f"{sec}:")
        for item in items:
            tail.extend(_serialize(item))
    lines.extend(tail)

    text = "\n".join(lines)
    if live_text.endswith("\n") or not live_text:
        text += "\n"
    return text


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Add documented default settings to the live config.yaml")
    ap.add_argument("--example", required=True)
    ap.add_argument("--live", required=True)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    example_path, live_path = Path(args.example), Path(args.live)
    for p in (example_path, live_path):
        if not p.is_file():
            print(f"[sync] config missing: {p}", file=sys.stderr)
            return 1

    plan = build_plan(parse_example(example_path.read_text(encoding="utf-8")),
                      live_key_names(live_path.read_text(encoding="utf-8")))
    if not plan:
        print("[sync] live config already has every documented setting - nothing to add")
        return 0

    def count(items: list) -> int:
        return sum(len(i.children) if isinstance(i, Block) else 1 for i in items)

    total = sum(count(items) for items in plan.values())
    print(f"[sync] adding {total} missing setting(s):")
    for section, items in plan.items():
        for item in items:
            keys = [c.key for c in item.children] if isinstance(item, Block) \
                else [item.key]
            print(f"[sync]   [{section}] {', '.join(keys)}")
    if args.dry_run:
        print("[sync] dry run - nothing changed")
        return 0

    backup = live_path.with_name(live_path.name + ".bak-sync")
    backup.write_text(live_path.read_text(encoding="utf-8"), encoding="utf-8")
    live_path.write_text(apply_plan(live_path.read_text(encoding="utf-8"), plan),
                         encoding="utf-8")
    print(f"[sync] live config updated (backup: {backup.name})")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except OSError as exc:
        print(f"[sync] failed: {exc}", file=sys.stderr)
        sys.exit(1)
