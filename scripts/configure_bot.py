#!/usr/bin/env python3
"""Interactive config editor for MeshTech-Bot (SSH / headless friendly).

Asks for the settings that matter - repeater IP + port, channels, admin
nodes, hop limit - and writes them into the existing config.yaml.

Design notes:
  * Answers are SPLICE-ed into the current file, not regenerated: any key
    you were not asked about (secrets, limits, logging, ...) is preserved
    byte-for-byte. If the file is missing a key entirely, it is only added
    when the answer differs from the default.
  * Channel edits keep extra per-channel keys (like secret_hex) when a
    channel keeps its name.
  * The result is validated with the bot's own config loader BEFORE the
    file is replaced, and a timestamped backup of the original is kept.

Run by manage.sh as the bot's service account (needs write access to the
config); it can also be used standalone:

    python3 scripts/configure_bot.py [path/to/config.yaml]
"""
from __future__ import annotations

import re
import shutil
import sys
import time
from pathlib import Path

try:
    import yaml
except ImportError:
    print("PyYAML is missing. Run this editor through manage.sh (option 1),",
          "which uses the bot's own Python - or install it with:")
    print("    pip install PyYAML")
    sys.exit(2)

ROOT = Path(__file__).resolve().parent.parent
PREFIX_RE = re.compile(r"^[0-9a-f]{12}$", re.IGNORECASE)


def _bot_root() -> Path:
    """Folder containing the bot's core/ package.

    Normally the repo this file lives in; a copy of this editor running
    outside a repo (a /tmp trial via manage.sh) borrows the installed bot's
    code so validation works there too.
    """
    if (ROOT / "core").is_dir():
        return ROOT
    installed = Path("/opt/meshtech-bot")
    if (installed / "core").is_dir():
        return installed
    return ROOT


# ---------------------------------------------------------------- input helpers
def ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default != "" else ""
    while True:
        answer = input(f" {prompt}{suffix}: ").strip()
        if answer:
            return answer
        if default != "":
            return default
        print("   -> please enter a value (Ctrl-C aborts without saving).")


def ask_int(prompt: str, default: int, lo: int, hi: int) -> int:
    while True:
        raw = ask(f"{prompt} ({lo}-{hi})", str(default))
        try:
            value = int(raw)
        except ValueError:
            print("   -> please type a number.")
            continue
        if lo <= value <= hi:
            return value
        print(f"   -> must be between {lo} and {hi}.")


def ask_yes_no(prompt: str, default: bool) -> bool:
    hint = "Y/n" if default else "y/N"
    while True:
        raw = input(f" {prompt} [{hint}]: ").strip().lower()
        if not raw:
            return default
        if raw in ("y", "yes"):
            return True
        if raw in ("n", "no"):
            return False
        print("   -> please answer y or n.")


def ask_prefixes(existing: list) -> list:
    print("   Admin nodes may run control commands (!diag, !reload, ...).")
    print("   Enter one 12-character key prefix per line; empty line to finish.")
    print("   (Leave the list empty to keep public commands only.)")
    items = list(existing)
    while True:
        if items:
            print("   current: " + ", ".join(items))
        raw = input("   admin prefix (empty line = done): ").strip()
        if not raw:
            if items or ask_yes_no("Continue with NO admin nodes?", not items):
                return items
            continue
        value = raw.lower().lstrip("0x") if raw.lower().startswith("0x") else raw.lower()
        if not PREFIX_RE.match(value):
            print("   -> needs exactly 12 hex characters, like a1b2c3d4e5f6.")
            continue
        if value not in items:
            items.append(value)


def ask_channels(existing: list) -> list:
    print("   Existing channels: Enter keeps each one.")
    out: list = []
    # Phase 1 - keep or drop each existing channel (Enter = keep).
    for ch in existing:
        if ask_yes_no(f"   Keep channel {ch['name']}?", True):
            if ch.get("reply", True):
                prompt = f"     {ch['name']} currently answers, keep answering?"
            else:
                prompt = f"     {ch['name']} is listen-only, keep it that way?"
            reply = ask_yes_no(prompt, bool(ch.get("reply", True)))
            entry = {"name": ch["name"], "reply": reply}
            # keep everything else this channel had (secret_hex, ...)
            for key, value in ch.items():
                if key not in entry:
                    entry[key] = value
            out.append(entry)
        else:
            print(f"     {ch['name']} removed from the config.")
    # Phase 2 - add brand-new channels.
    while len(out) < 8 and ask_yes_no("   Add another channel?", False):
        name = ask("   new channel name (a # is added if missing)")
        if not name.startswith("#"):
            name = "#" + name
        if any(c["name"].lower() == name.lower() for c in out):
            print("   -> that channel is already in the list.")
            continue
        reply = ask_yes_no(f"     may the bot ANSWER on {name}?", True)
        out.append({"name": name, "reply": reply})
    if not out:
        print("   NOTE: with no channels the bot only logs direct messages.")
    return out


def ask_replies(existing: list) -> list:
    """Simple keyword replies (the no-code feature in config.yaml).

    Each rule: a list of keywords and the text the bot answers with
    (several texts = one is picked at random). Enter keeps what exists.
    """
    print("   Keyword replies: when one of the keywords appears in a message,")
    print("   the bot answers with your text. Enter keeps each existing rule.")
    out: list = []
    for rule in existing:
        keywords = ", ".join(str(k) for k in rule.get("keywords", []))
        texts = rule.get("text")
        if isinstance(texts, str):
            texts = [texts]
        texts = [str(t) for t in (texts or [])]
        shown = texts[0] if texts else "(no text)"
        more = f" (+{len(texts) - 1} more)" if len(texts) > 1 else ""
        if not ask_yes_no(f"   Keep reply '{keywords}' -> '{shown}'{more}?", True):
            print("     that reply will be removed.")
            continue
        out.append({"keywords": list(rule.get("keywords", [])),
                    "text": list(texts) if len(texts) > 1 else (texts[0] if texts else "")})
    while ask_yes_no("   Add a keyword reply?", False):
        raw = ask("   keywords, comma-separated (the bot matches lower-case)")
        keywords = [k.strip().lower() for k in raw.split(",") if k.strip()]
        if not keywords:
            print("   -> at least one keyword is needed - skipped.")
            continue
        texts = [ask("   reply text")]
        while ask_yes_no("   add another possible answer (picked at random)?", False):
            texts.append(ask("   reply text"))
        out.append({"keywords": keywords,
                    "text": texts if len(texts) > 1 else texts[0]})
    return out


# ---------------------------------------------------------------- splice helpers
def splice_scalar(text: str, dotted: str, py_value) -> str:
    """Set a scalar key inside its top-level section, preserving formatting.

    Replaces the existing line when present; otherwise inserts after the
    section header - but only when the value differs from what a fresh
    config.example.yaml would already say (kept in DEFAULTS below).
    """
    section, key = dotted.split(".", 1)
    new_line = f"  {key}: {py_value}"
    header = re.search(rf"(?m)^{re.escape(section)}:[ \t]*(?:#.*)?$", text)
    if header is None:
        raise SystemExit(f"config.yaml has no '{section}:' section - cannot set {dotted}")
    # the section's span: from its header to the next top-level line (or EOF)
    rest = text[header.end():]
    nxt = re.search(r"(?m)^\S", rest)
    sec_end = header.end() + (nxt.start() if nxt else len(rest))
    section_text = text[header.end():sec_end]

    line_re = re.compile(rf"(?m)^  {re.escape(key)}:.*$")
    if line_re.search(section_text):
        new_section = line_re.sub(new_line, section_text, count=1)
        return text[:header.end()] + new_section + text[sec_end:]
    if py_value == DEFAULTS.get(dotted, object()):
        return text  # example default; no need to write anything
    # insert as the first key of the section
    return text[:header.end()] + "\n" + new_line + text[header.end():]


def splice_channels(text: str, channels: list) -> str:
    """Replace the body of the channels: list with the new entries."""
    start = re.search(r"(?m)^channels:[ \t]*(?:#.*)?$", text)
    if start is None:
        raise SystemExit("config.yaml has no 'channels:' section")
    rest = text[start.end():]
    nxt = re.search(r"(?m)^\S", rest)  # first line of the NEXT section
    body_end = start.end() + (nxt.start() if nxt else len(rest))
    lines = []
    for ch in channels:
        reply = "true" if ch.get("reply", True) else "false"
        lines.append(f'  - name: "{ch["name"]}"')
        lines.append(f"    reply: {reply}")
        for key, value in ch.items():
            if key in ("name", "reply"):
                continue
            # block style gives 'key: value' on one line (flow style would
            # produce '{key: value}', which is invalid at this position)
            rendered = yaml.safe_dump({key: value}, default_flow_style=False).strip()
            lines.append(f"    {rendered}")
    replacement = ("\n".join(lines) + "\n") if lines else ""
    tail = text[body_end:]
    if tail.startswith("#"):
        tail = "\n" + tail  # keep the blank line before a section comment
    return text[:start.end()] + "\n" + replacement + tail


def _yaml_scalar(value) -> str:
    """Render one scalar as a single-line YAML value.

    yaml.safe_dump appends a document-end marker ('...') after scalar
    documents and wraps long/multi-line strings - both unusable here - so
    this takes the clean first line, and falls back to JSON (a strict
    subset of YAML) for anything that does not fit on one line.
    """
    import json
    if isinstance(value, (list, dict)):
        # containers must be inline (flow style) - block style would emit
        # '- item' lines, which are invalid after 'keywords:' on one line
        dumped = yaml.safe_dump(value, default_flow_style=True).strip()
        return dumped if "\n" not in dumped else json.dumps(value)
    dumped = [l for l in yaml.safe_dump(value, default_flow_style=False).splitlines()
              if l != "..."]
    if len(dumped) != 1:
        return json.dumps(value)
    return dumped[0]


def splice_replies(text: str, rules: list) -> str:
    """Replace the replies section with the new rules.

    Handles both spellings - a bare header ('replies:') with an indented
    list body, and an inline empty list ('replies: []'). An empty rule
    list is written back as 'replies: []' so the file stays unambiguous.
    """
    start = re.search(r"(?m)^replies:.*$", text)
    if start is None:
        raise SystemExit("config.yaml has no 'replies:' section")
    rest = text[start.end():]
    nxt = re.search(r"(?m)^\S", rest)  # next section or column-0 comment
    body_end = start.end() + (nxt.start() if nxt else len(rest))
    lines: list = []
    for rule in rules:
        keywords = _yaml_scalar([str(k) for k in rule.get("keywords", [])])
        lines.append("  - keywords: " + keywords)
        texts = rule.get("text")
        if isinstance(texts, str):
            texts = [texts]
        if len(texts) == 1:
            lines.append("    text: " + _yaml_scalar(texts[0]))
        else:
            lines.append("    text:")
            for t in texts:
                lines.append("      - " + _yaml_scalar(t))
    if not lines:
        return text[:start.start()] + "replies: []\n" + text[body_end:]
    tail = text[body_end:]
    if tail.startswith("#"):
        tail = "\n" + tail  # keep the blank line before a section comment
    # a block list needs a bare 'replies:' header - an inline 'replies: []'
    # followed by indented items would orphan the list
    return text[:start.start()] + "replies:\n" + "\n".join(lines) + "\n" + tail


DEFAULTS = {
    "connection.port": 5000,
    "mesh.max_inbound_hops": 0,
}


# ---------------------------------------------------------------- main flow
def main() -> int:
    config_path = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "config.yaml"
    if not sys.stdin.isatty():
        print("This editor is interactive - run it from manage.sh or a terminal.")
        return 2
    if not config_path.is_file():
        print(f"No config file at {config_path} - run install.sh first.")
        return 2

    original = config_path.read_text(encoding="utf-8")
    data = yaml.safe_load(original) or {}

    conn = data.get("connection") or {}
    mesh = data.get("mesh") or {}
    dm = data.get("dm") or {}
    channels = list(data.get("channels") or [])
    existing_admins = [str(p) for p in (dm.get("admin_pubkey_prefixes") or [])]
    existing_replies = list(data.get("replies") or [])

    print("=" * 66)
    print("  MeshTech-Bot configuration")
    print("  Press Enter to keep the value in [brackets]. Ctrl-C quits")
    print("  without changing anything.")
    print("=" * 66)

    host = ask("Repeater IP address (openHop companion host)",
               str(conn.get("host", "")))
    port = ask_int("Companion port", int(conn.get("port", 5000)), 1, 65535)
    channels_new = ask_channels(channels)
    admins = ask_prefixes(existing_admins)
    hop_limit = ask_int("Max hops to answer (0 = unlimited)",
                        int(mesh.get("max_inbound_hops", 0)), 0, 7)
    replies_new = ask_replies(existing_replies)

    text = original
    text = splice_scalar(text, "connection.host", f'"{host}"')
    text = splice_scalar(text, "connection.port", port)
    text = splice_scalar(text, "mesh.max_inbound_hops", hop_limit)
    text = splice_channels(text, channels_new)
    text = splice_replies(text, replies_new)

    # admin list: rewrite only the admin_pubkey_prefixes block
    if admins != existing_admins:
        entries = "\n".join(f'    - "{p}"' for p in admins) or "    []"
        block = re.compile(r"(?m)^  admin_pubkey_prefixes:.*?(?=^  \w|^$)", re.S)
        replacement = f"  admin_pubkey_prefixes:\n{entries}\n"
        if block.search(text):
            text = block.sub(replacement, text, count=1)
        else:
            text = splice_scalar(text, "dm.enabled", "true")
            text = text.replace("  dm.enabled: true",
                                "  dm.enabled: true\n  admin_pubkey_prefixes:\n" + entries, 1)

    print("\n " + "-" * 62)
    print("  New settings:")
    print(f"    repeater   : {host}:{port}")
    print(f"    channels   : " + (", ".join(
        f"{c['name']}{' (listen-only)' if not c.get('reply', True) else ''}"
        for c in channels_new) or "NONE"))
    print(f"    admins     : " + (", ".join(admins) or "none"))
    print(f"    hop limit  : {hop_limit if hop_limit else 'unlimited'}")
    print(f"    replies    : {len(replies_new)} keyword rule(s)")
    print(" " + "-" * 62)
    if not ask_yes_no("Save these settings (a backup of the old file is kept)?", True):
        print(" Aborted - nothing was changed.")
        return 1

    # validate with the bot's own loader BEFORE touching the file
    sys.path.insert(0, str(_bot_root()))
    check_path = config_path.with_suffix(".yaml.new")
    check_path.write_text(text, encoding="utf-8")
    try:
        from core.config import load  # noqa: E402
        load(str(check_path))
    except Exception as exc:
        check_path.unlink(missing_ok=True)
        print(f"\n Validation FAILED - the file was NOT changed:\n{exc}")
        return 1
    check_path.unlink(missing_ok=True)

    backup = config_path.with_name(
        config_path.name + ".bak-" + time.strftime("%Y%m%d-%H%M%S"))
    shutil.copy2(config_path, backup)
    config_path.write_text(text, encoding="utf-8")
    print(f" Saved. Previous config kept as {backup.name}.")
    print(" Config edits are picked up by a running bot within a minute;")
    print(" use menu option 4 (or 'sudo systemctl restart meshtech-bot')")
    print(" to apply them immediately.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n Aborted - nothing was changed.")
        sys.exit(130)
