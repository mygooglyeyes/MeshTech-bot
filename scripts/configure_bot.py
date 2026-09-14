#!/usr/bin/env python3
"""Interactive config editor for MeshTech-Bot (SSH / headless friendly).

Asks for the settings that matter - repeater IP + port, channels (with
private-channel keys), admin nodes, hop limit, dashboard reachability -
and writes them into the existing config.yaml.

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

    python3 scripts/configure_bot.py [--full] [path/to/config.yaml]

With --full it walks EVERY documented setting instead of the essentials
(commented-out keys show as "default unused"; giving a value uncomments
them).
"""
from __future__ import annotations

import getpass
import json
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
    if items == ["a1b2c3d4e5f6"]:
        print("   (that is the example PLACEHOLDER below - replace it with your own)")
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


def ask_secret(channel: str) -> str:
    """Ask for a private channel's key (hex); empty answer = no key."""
    while True:
        raw = input(f"     key for {channel} (hex characters, Enter = none): ").strip().lower()
        if not raw:
            return ""
        try:
            bytes.fromhex(raw)
        except ValueError:
            print("   -> that is not valid hex - try again, or Enter to skip.")
            continue
        return raw


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
            # a channel without a key is offered one; a keyed channel can
            # have its key replaced (fresh example files have no keys yet)
            if not entry.get("secret_hex"):
                if ask_yes_no(f"     is {ch['name']} a PRIVATE (keyed) channel?", False):
                    secret = ask_secret(ch["name"])
                    if secret:
                        entry["secret_hex"] = secret
            elif not ask_yes_no(f"     keep {ch['name']}'s private channel key?", True):
                secret = ask_secret(ch["name"])
                if secret:
                    entry["secret_hex"] = secret
                else:
                    entry.pop("secret_hex", None)
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
        entry = {"name": name, "reply": reply}
        if ask_yes_no(f"     is {name} a PRIVATE (keyed) channel?", False):
            secret = ask_secret(name)
            if secret:
                entry["secret_hex"] = secret
        out.append(entry)
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


# ============================================================ full editor
# One prompt per documented setting - every scalar key the bot's config
# loader reads, whether the example file ships it active or commented out.
# Commented-out keys show "default unused": Enter leaves them unused (the
# file stays as-is); typing a value uncomments the line with that value.
# Structured settings keep their own editors and are deliberately NOT
# asked here: channels (the channel editor), admin nodes (the prefix
# editor), keyword replies (the replies editor), modules (the web
# console's Modules card owns their fields), and the per-channel
# intervals map. Order follows config.example.yaml.
# Each row: (section, key, kind, default, bounds_or_choices, help)
#   kind: bool | int | num | text | choice | secret
#   int/num carry (lo, hi); choice carries the allowed words.
FULL_FIELDS = [
    # --- connection: the one radio source (companion mode) ---
    ("connection", "host", "text", "", None,
     "LAN IP of the machine running the companion (repeater) software."),
    ("connection", "port", "int", 5000, (1, 65535),
     "Companion port - must match the companion's tcp_port setting."),
    ("connection", "reconnect", "bool", True, None,
     "Keep trying if the connection drops."),
    ("connection", "reconnect_min_seconds", "num", 3.0, (0.0, 3600.0),
     "Minimum wait between reconnect attempts (seconds)."),
    ("connection", "reconnect_max_seconds", "num", 60.0, (1.0, 3600.0),
     "Maximum wait between reconnect attempts (backoff cap)."),
    # --- bot: behaviour ---
    ("bot", "advertise_on_start", "bool", True, None,
     "Send an advert at startup so nodes can find the bot."),
    ("bot", "display_name", "text", "me", None,
     "Fallback name for replies when the companion's name is unknown."),
    ("bot", "answer_unknown_senders", "bool", False, None,
     "true = answer messages with no identifiable sender (rarely wise)."),
    ("bot", "sync_device_time", "bool", False, None,
     "Clock-sync the companion at startup (only for clockless firmware)."),
    ("bot", "command_prefix", "text", "!", None,
     "The symbol that starts a command. ONE symbol; : # @ refused. "
     "Change it on a quiet mesh and tell your users."),
    # --- mesh: hop limits and names ---
    ("mesh", "max_inbound_hops", "int", 0, (0, 7),
     "Ignore messages that travelled via MORE hops than this. 0 = no limit."),
    ("mesh", "unknown_hops", "choice", "ignore", ["ignore", "respond"],
     "When the hop count can't be read: ignore (safe) or respond anyway."),
    ("mesh", "channel_sender_name", "choice", "trust", ["trust", "smart", "off"],
     "Trust the sender name embedded in channel text: trust / smart / off."),
    # path hash size: the user types the mesh's 0/1/2 convention; the
    # file stores BYTES (value + 1). Typed choice:
    ("mesh", "path_hash_size", "pathhash", 2, [0, 1, 2],
     "Path hash size - counts from 0: 0 = 1 byte, 1 = 2 bytes (our mesh's "
     "setting), 2 = 3 bytes. Caution: a value the repeaters do not use "
     "yet gets the bot's adverts unrelayed by older 1-byte stations."),
    # --- dm ---
    ("dm", "enabled", "bool", True, None,
     "Answer direct messages at all."),
    # --- verbosity: reply detail levels ---
    ("verbosity", "channel_default", "choice", "brief", ["brief", "full"],
     "Default reply length in channels (append x for extended per request)."),
    ("verbosity", "dm_default", "choice", "brief", ["brief", "full"],
     "Default reply length in DMs."),
    # --- storage: database + packet capture ---
    ("storage", "db_path", "text", "data/bot.db", None,
     "SQLite database file (relative to the bot folder)."),
    ("storage", "contact_refresh_minutes", "int", 30, (1, 1440),
     "How often to refresh the node table from adverts."),
    ("storage", "capture_packets", "bool", True, None,
     "Store every decoded frame for traffic analysis."),
    ("storage", "packet_raw_hex", "bool", False, None,
     "Also store the raw wire bytes of each frame (bulkier)."),
    ("storage", "packet_jsonl", "text", "data/packets.jsonl", None,
     "Append-only JSONL analysis file. Empty string disables it."),
    ("storage", "packet_jsonl_max_bytes", "int", 67108864, (0, 10**12),
     "Rotate packets.jsonl at this size (bytes). 0 = grow forever."),
    ("storage", "packet_max_rows", "int", 200000, (1000, 10**9),
     "Keep the last N packet rows in the database."),
    # --- limits: keep the mesh happy ---
    ("limits", "min_interval_seconds", "num", 3.0, (0.0, 600.0),
     "Minimum gap between ANY two bot replies."),
    ("limits", "per_sender_seconds", "num", 30.0, (0.0, 3600.0),
     "Ignore repeat DM requests from the same node for this long."),
    ("limits", "reply_delay_seconds", "num", 2.0, (0.0, 60.0),
     "Wait BEFORE sending any reply's first packet (air politeness). "
     "0 = send at once."),
    ("limits", "channel_interval_seconds", "num", 0.0, (0.0, 3600.0),
     "At most ONE reply per channel every N seconds (0 = off)."),
    ("limits", "per_sender_channel_seconds", "num", 30.0, (0.0, 3600.0),
     "In channels, one node waits this long between bot answers "
     "(admins exempt; 0 = off)."),
    ("limits", "max_reply_length", "int", 133, (40, 233),
     "MeshCore text limit per message (the loader floors this at 40)."),
    ("limits", "max_chunks", "int", 6, (1, 20),
     "Max messages one reply may be split into."),
    # --- web: dashboard ---
    ("web", "enabled", "bool", True, None,
     "Run the web management dashboard."),
    ("web", "host", "text", "127.0.0.1", None,
     "127.0.0.1 = this machine only (recommended). 0.0.0.0 = LAN-reachable "
     "(set a real password!)."),
    ("web", "port", "int", 8081, (1, 65535),
     "Dashboard port."),
    ("web", "password", "secret", "", None,
     "LEGACY inline dashboard password (travels inside config.yaml). "
     "Preferred: sudo ./set-password.sh (writes the separate password file)."),
    ("web", "password_file", "text", "data/.dashboard_password", None,
     "File holding the dashboard password (first line). Recommended."),
    ("web", "developer_mode", "bool", False, None,
     "Let the dashboard's update popup click ANY branch (contributors)."),
    # --- radio: airtime statistics only (must MATCH the repeater) ---
    ("radio", "spreading_factor", "int", 7, (5, 12),
     "Must MATCH your repeater (statistics only, no radio setup)."),
    ("radio", "bandwidth_khz", "num", 62.5, (7.8, 500.0),
     "Must match the repeater (kHz)."),
    ("radio", "coding_rate_index", "int", 1, (1, 4),
     "1 = 4/5, 2 = 4/6, 3 = 4/7, 4 = 4/8. Must match the repeater."),
    ("radio", "preamble_symbols", "int", 32, (4, 64),
     "Must match the repeater."),
    # --- updates: newer-code check + web-console updates ---
    ("updates", "check_enabled", "bool", True, None,
     "The dashboard checks for newer code (read-only)."),
    ("updates", "check_hours", "num", 24.0, (0.25, 168.0),
     "How often to look, in hours (min 0.25 = 15 minutes)."),
    ("updates", "repo_url", "text", "", None,
     "Repository to check - point at your own fork if you run one."),
    ("updates", "clone_path", "text", "", None,
     "Your git clone path (e.g. /home/pi/meshtech-bot) to enable "
     "web-console updates. Empty = off. Also needs: sudo ./manage.sh webupdates."),
    # --- logging ---
    ("logging", "level", "choice", "INFO", ["DEBUG", "INFO", "WARNING", "ERROR"],
     "Log verbosity."),
    ("logging", "file", "text", "", None,
     "Log file path. Empty = console only (journalctl under systemd)."),
    ("logging", "timezone", "text", "local", None,
     "local | utc | IANA name such as Europe/London."),
    ("modem_feed", "queue_size", "int", 200, (10, 100000),
     "Buffer between radio RX and the modem feed (packets); overflow drops "
     "feed packets only, never radio traffic."),
    # --- mcp: the bot's own SPI radio ---
    ("mcp", "enabled", "bool", False, None,
     "true = the bot OWNS the PiMesh radio (connection: block is ignored)."),
    ("mcp", "frequency_hz", "int", 910525000, (400000000, 1000000000),
     "Band center used by this network (US 915 MHz)."),
    ("mcp", "tx_power_dbm", "int", 20, (-9, 20),
     "Hard legal ceiling 20 dBm (floor -9) - set lower per your local rules."),
    ("mcp", "spreading_factor", "int", 7, (5, 12),
     "Radio spreading factor."),
    ("mcp", "bandwidth_khz", "num", 62.5, (7.8, 500.0),
     "Radio bandwidth (kHz)."),
    ("mcp", "coding_rate_index", "int", 1, (1, 4),
     "Radio coding rate: 1 = 4/5."),
    ("mcp", "advert_interval_hours", "num", 24.0, (0.0, 720.0),
     "Re-flood the advert this often after the two start-up adverts. 0 = off."),
    ("mcp", "inter_packet_politeness_seconds", "num", 2.0, (0.0, 60.0),
     "Minimum gap between the bot's OWN radio packets. 0 = off."),
    ("mcp", "cad_peak", "int", 15, (0, 31),
     "Radio LBT sensitivity (peak), 0-31. 15/7 = Brett's receiving tune. "
     "0/0 = driver defaults."),
    ("mcp", "cad_min", "int", 7, (0, 31),
     "Radio LBT sensitivity (min), 0-31."),
    ("mcp", "precheck_cad_peak", "int", 22, (0, 31),
     "The bot's own clear-channel pre-check (peak). 22/10 = Semtech SF7 pair."),
    ("mcp", "precheck_cad_min", "int", 10, (0, 31),
     "The bot's own clear-channel pre-check (min)."),
    ("mcp", "clear_channel_wait_seconds", "num", 4.0, (0.0, 60.0),
     "How long to wait out a busy channel before transmitting (protects "
     "ACKs). 0 = off."),
    # --- modem_feed: radio packets -> meshtech-modem ---
    ("modem_feed", "enabled", "bool", False, None,
     "Push a copy of every radio packet to meshtech-modem's feed port."),
    ("modem_feed", "host", "text", "127.0.0.1", None,
     "Box running meshtech-modem."),
    ("modem_feed", "port", "int", 5056, (1, 65535),
     "The modem's FEED port (5055 is the companion's)."),
    ("modem_feed", "token_file", "text", "data/.modem_feed_token", None,
     "File holding the feed token (first line, mode 600) - never here."),
]

_KEEP = object()          # sentinel: keep the file as-is for this key


def _is_commented_key(text: str, section: str, key: str) -> bool:
    """True when the key appears only as a commented-out line."""
    if re.search(rf"(?m)^\s+{re.escape(key)}:", text):
        return False
    return re.search(rf"(?m)^\s*#\s*{re.escape(key)}:", text) is not None


def _render_full_value(value, kind: str) -> str:
    """Render one answer as a single-line YAML value (splice-ready)."""
    if kind == "bool":
        return "true" if value else "false"
    if kind in ("int", "num", "choice", "pathhash"):
        return repr(value) if kind in ("int", "num") else str(value)
    return json.dumps(str(value))     # quoting survives names with spaces


def set_full_key(text: str, dotted: str, rendered: str) -> str:
    """Write one rendered key: replace its line, or uncomment/insert it.

    Existing active lines are replaced in place (any indent); commented-out
    or absent keys are written at the section's 2-space indent via
    splice_scalar. Raises SystemExit when the section header is missing.
    """
    section = dotted.split(".", 1)[0]
    if not re.search(rf"(?m)^{re.escape(section)}:[ \t]*(?:#.*)?$", text):
        raise SystemExit(f"no '{section}:' section in this file")
    return splice_scalar(text, dotted, rendered)


def _choice_display(b) -> str:
    """One choice as displayed: 'value' or 'value = label'."""
    if isinstance(b, tuple):
        value, label = b
        return f"{value} = {label}" if label and str(label) != str(value) else str(value)
    return str(b)


def match_choice(raw: str, bounds):
    """Match a typed answer against a choice list (case-insensitive).

    Entries may be plain strings or (value, label) pairs; the canonical
    VALUE is always returned so the file stores what the loader expects
    (e.g. 'info' and 'INFO' both return 'INFO'). None = no match; the
    caller decides what an empty answer means.
    """
    raw = str(raw).strip().lower()
    if not raw:
        return None
    for b in bounds:
        value, label = (b if isinstance(b, tuple) else (b, b))
        value, label = str(value), str(label)
        if raw == value.lower() or raw == label.lower():
            return value
    return None


def _ask_full(kind: str, default, bounds, current, present: bool, commented: bool):
    """Ask one full-editor question. Returns _KEEP or the parsed answer."""
    if kind == "pathhash":
        # user-facing counting starts at 0; the file stores bytes (+1).
        shown = (current if present else default) - 1
        while True:
            raw = input(f"  value [0, 1, or 2] [{shown}]: ").strip()
            if not raw:
                return _KEEP
            if raw in ("0", "1", "2"):
                return int(raw) + 1
            print("   -> please type 0 (1 byte), 1 (2 bytes) or 2 (3 bytes).")
    if kind == "secret":
        raw = getpass.getpass("  value (typing hidden, Enter = skip): ").strip()
        return _KEEP if not raw else raw
    if kind == "text":
        if commented:
            raw = input("  value (Enter = leave unused): ").strip()
            return _KEEP if not raw else raw
        # Enter keeps the CURRENT value (or the example default when the
        # key is absent) - never an empty string (an empty file value
        # could disable features like logging.file).
        raw = input(f"  value [{current if present else default}]: ").strip()
        return _KEEP if not raw else raw
    if kind == "bool":
        return ask_yes_no("value", bool(current if present else default))
    if kind == "choice":
        wanted = str(current if present else default)
        while True:
            disp = " | ".join(_choice_display(b) for b in bounds)
            raw = input(f"  value [{disp}] [{wanted}]: ").strip()
            if not raw:
                raw = wanted
            canon = match_choice(raw, bounds)
            if canon is not None:
                return canon
            print("   -> please type one of: "
                  + ", ".join(str(b[0] if isinstance(b, tuple) else b) for b in bounds))
    lo, hi = bounds
    if kind == "int":
        return ask_int("value", int(current if present else default), lo, hi)
    while True:
        raw = ask(f"value ({lo}-{hi})", str(float(current if present else default)))
        try:
            value = float(raw)
        except ValueError:
            print("   -> please type a number.")
            continue
        if lo <= value <= hi:
            return value
        print(f"   -> must be between {lo} and {hi}.")


def run_full_editor(original: str, data: dict) -> tuple:
    """Walk EVERY documented setting; return (new_text, changed_keys)."""
    print("=" * 66)
    print("  FULL configuration - every documented setting, one by one")
    print("  Enter keeps the current value. Ctrl-C quits without changing")
    print("  anything.")
    print("  Keys marked (default unused) are commented out in the file:")
    print("  Enter leaves them unused; a value switches them on.")
    print("  NOT asked here (they keep their own editors): channels, admin")
    print("  nodes, keyword replies, modules (console card), per-channel")
    print("  interval overrides.")
    print("=" * 66)
    changed: list = []
    for section, key, kind, default, bounds, help_text in FULL_FIELDS:
        dotted = f"{section}.{key}"
        sec = data.get(section)
        current = sec.get(key) if isinstance(sec, dict) else None
        present = isinstance(sec, dict) and key in sec
        commented = _is_commented_key(original, section, key)
        print()
        print("-" * 66)
        tag = ""
        if commented:
            tag = "   (default unused - Enter leaves it off)"
        elif not present:
            tag = "   (not in file yet - default applies)"
        print(f"{dotted}{tag}")
        if help_text:
            print(f"  {help_text}")
        value = _ask_full(kind, default, bounds, current, present, commented)
        if value is _KEEP:
            continue
        if present and value == current:
            continue
        changed.append((dotted, _render_full_value(value, kind)))
    text = original
    for dotted, rendered in changed:
        try:
            text = set_full_key(text, dotted, rendered)
        except SystemExit as exc:
            print(f"  SKIPPED {dotted}: {exc}")
    return text, changed


# ---------------------------------------------------------------- main flow
def _validate_and_save(config_path: Path, text: str) -> int:
    """Validate with the bot's own loader, then write (backup first)."""
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


def _main_essentials(config_path: Path, original: str, data: dict) -> int:
    conn = data.get("connection") or {}
    mesh = data.get("mesh") or {}
    dm = data.get("dm") or {}
    channels = list(data.get("channels") or [])
    existing_admins = [str(p) for p in (dm.get("admin_pubkey_prefixes") or [])]
    existing_replies = list(data.get("replies") or [])
    web = data.get("web") or {}
    host_reachable = str(web.get("host", "127.0.0.1")) not in ("127.0.0.1", "localhost", "::1")

    print("=" * 66)
    print("  MeshTech-Bot configuration")
    print("  Press Enter to keep the value in [brackets]. Ctrl-C quits")
    print("  without changing anything.")
    print("=" * 66)

    host = ask("Repeater IP address (companion host)",
               str(conn.get("host", "")))
    port = ask_int("Companion port", int(conn.get("port", 5000)), 1, 65535)
    channels_new = ask_channels(channels)
    admins = ask_prefixes(existing_admins)
    hop_limit = ask_int("Max hops to answer (0 = unlimited)",
                        int(mesh.get("max_inbound_hops", 0)), 0, 7)
    replies_new = ask_replies(existing_replies)
    if ask_yes_no("Reach the dashboard from other machines (phone/laptop)?", host_reachable):
        web_host = "0.0.0.0"
    else:
        web_host = "127.0.0.1"
    if web_host != str(web.get("host", "127.0.0.1")):
        print("   dashboard password is separate from this file - set/change it with:")
        print("     sudo ./set-password.sh   (menu option 1 flow will remind you too)")

    text = original
    text = splice_scalar(text, "connection.host", f'"{host}"')
    text = splice_scalar(text, "connection.port", port)
    text = splice_scalar(text, "mesh.max_inbound_hops", hop_limit)
    if web_host != str(web.get("host", "127.0.0.1")):
        text = splice_scalar(text, "web.host", f'"{web_host}"')
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
    print(f"    dashboard  : {web_host}" + (" (this machine only)" if web_host == "127.0.0.1" else " (LAN)"))
    print(" " + "-" * 62)
    if not ask_yes_no("Save these settings (a backup of the old file is kept)?", True):
        print(" Aborted - nothing was changed.")
        return 1

    rc = _validate_and_save(config_path, text)
    if rc == 0:
        print()
        print(" Everything not asked here keeps the values from the example file,")
        print(" including the commented-out options - each shows its own explanation,")
        print(" so edit config.yaml by hand whenever you want to turn one on:")
        print("   command prefix, per-channel reply pacing, packet capture options,")
        print("   module settings (weather/alerts/quake, push budget), raw hex capture,")
        print("   radio stats, update checks, logging and more - all with comments.")
        print(" (Or use the FULL editor: menu option 2 / --full - it asks every key.)")
    return rc


def _main_full(config_path: Path, original: str, data: dict) -> int:
    text, changed = run_full_editor(original, data)
    if not changed:
        print("\n Nothing was changed.")
        return 0
    print()
    print(" " + "-" * 62)
    print("  New values:")
    for dotted, rendered in changed:
        shown = '"****"' if (dotted.endswith(".password") and rendered != '""') else rendered
        print(f"    {dotted} = {shown}")
    print(" " + "-" * 62)
    if not ask_yes_no("Save these settings (a backup of the old file is kept)?", True):
        print(" Aborted - nothing was changed.")
        return 1
    return _validate_and_save(config_path, text)


def main() -> int:
    argv = list(sys.argv[1:])
    full = "--full" in argv
    argv = [a for a in argv if a != "--full"]
    config_path = Path(argv[0]) if argv else ROOT / "config.yaml"
    if not sys.stdin.isatty():
        print("This editor is interactive - run it from manage.sh or a terminal.")
        return 2
    if not config_path.is_file():
        print(f"No config file at {config_path} - run install.sh first.")
        return 2

    original = config_path.read_text(encoding="utf-8")
    data = yaml.safe_load(original) or {}

    if full:
        return _main_full(config_path, original, data)
    return _main_essentials(config_path, original, data)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n Aborted - nothing was changed.")
        sys.exit(130)
