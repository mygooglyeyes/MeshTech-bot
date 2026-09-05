"""Configuration loading and validation.

Everything the bot needs to know lives in config.yaml.  This module reads
that file into typed Python objects and reports problems in plain English
so a non-programmer can fix the YAML themselves.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

DEFAULT_ADMIN_HELP = (
    "Add your node's public-key prefix (first 12 hex chars) under "
    "dm: admin_pubkey_prefixes in config.yaml. Ask '!nodes' or open the "
    "web dashboard to find prefixes."
)


class ConfigError(Exception):
    """Raised when config.yaml cannot be used; message is human readable."""


# --------------------------------------------------------------------------
# Typed settings
# --------------------------------------------------------------------------

@dataclass
class ConnCfg:
    host: str
    port: int = 5000
    reconnect: bool = True
    reconnect_min_seconds: float = 3.0
    reconnect_max_seconds: float = 60.0


@dataclass
class BotCfg:
    advertise_on_start: bool = True
    # Fail-closed: when the bot cannot identify who sent a message
    # (DM without a sender prefix, channel message with no embedded
    # sender name), stay silent. Set True to answer anyway.
    answer_unknown_senders: bool = False


@dataclass
class MeshCfg:
    max_inbound_hops: int = 0          # 0 = no limit
    unknown_hops: str = "ignore"       # "ignore" | "respond"
    # How to treat the sender name MeshCore embeds in channel-message text
    # ("Name: body"): "trust" (always strip), "smart" (strip only when the
    # full text would not already match), or "off" (never strip - for
    # gateways that relay messages without the embedded name).
    channel_sender_name: str = "trust"  # "trust" | "smart" | "off"


@dataclass
class ChannelCfg:
    name: str                          # e.g. "#bot"
    reply: bool = True
    secret_hex: Optional[str] = None   # override for private channels


@dataclass
class DmCfg:
    enabled: bool = True
    admin_pubkey_prefixes: List[str] = field(default_factory=list)


@dataclass
class VerbosityCfg:
    aliases_brief: List[str] = field(default_factory=list)              # compact is the default; no words needed
    aliases_full: List[str] = field(default_factory=lambda: ["x"])     # append "x" for the extended view
    channel_default: str = "brief"
    dm_default: str = "brief"

    def all_brief(self) -> List[str]:
        return _unique(self.aliases_brief + ["brief"])

    def all_full(self) -> List[str]:
        return _unique(self.aliases_full + ["full"])

    def level_for_token(self, token: str) -> Optional[str]:
        token = token.lower()
        if token in self.all_brief():
            return "brief"
        if token in self.all_full():
            return "full"
        return None


@dataclass
class StorageCfg:
    db_path: str = "data/bot.db"
    contact_refresh_minutes: int = 30
    # Packet capture: every decoded companion frame (and optionally the raw
    # wire frames) is stored for later traffic analysis.
    capture_packets: bool = True
    packet_raw_hex: bool = False   # also log the raw wire bytes of each frame
    packet_jsonl: str = "data/packets.jsonl"  # append-only analysis file; "" = off
    packet_max_rows: int = 200000  # keep the last N rows in the database


@dataclass
class ReplyRule:
    keywords: List[str] = field(default_factory=list)
    texts: List[str] = field(default_factory=list)


@dataclass
class LimitsCfg:
    min_interval_seconds: float = 3.0
    per_sender_seconds: float = 30.0
    max_reply_length: int = 133
    max_chunks: int = 6


@dataclass
class WebCfg:
    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 8081
    password: str = ""


@dataclass
class LogCfg:
    level: str = "INFO"
    file: str = ""
    timezone: str = "local"     # local | utc | IANA e.g. Europe/London
    tz_iana: Optional[str] = None


@dataclass
class Settings:
    connection: ConnCfg
    bot: BotCfg
    mesh: MeshCfg
    channels: List[ChannelCfg]
    dm: DmCfg
    verbosity: VerbosityCfg
    storage: StorageCfg
    replies: List[ReplyRule]
    limits: LimitsCfg
    web: WebCfg
    logging: LogCfg
    config_path: str = "config.yaml"
    warnings: List[str] = field(default_factory=list)
    raw: Dict[str, Any] = field(default_factory=dict)

    def channel_by_name(self, name: str) -> Optional[ChannelCfg]:
        for ch in self.channels:
            if ch.name == name:
                return ch
        return None

    def configured_channel_names(self) -> List[str]:
        return [ch.name for ch in self.channels]

    def is_admin_prefix(self, prefix: Optional[str]) -> bool:
        if not prefix:
            return False
        p = prefix.lower()
        return any(admin.lower().startswith(p) or p.startswith(admin.lower())
                   for admin in self.dm.admin_pubkey_prefixes)


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------

def load(config_path: str = "config.yaml") -> Settings:
    """Load and validate config.yaml. Raises ConfigError with clear text."""
    path = Path(config_path)
    if not path.is_file():
        raise ConfigError(
            f"Config file not found: {config_path}\n"
            "Create it by copying config.yaml from the project folder."
        )
    try:
        import yaml  # local import so the module can be imported w/o PyYAML
        with open(path, "r", encoding="utf-8") as handle:
            raw = yaml.safe_load(handle)
    except ImportError:
        raise ConfigError(
            "The PyYAML package is missing.\n"
            "Install it with:  pip install -r requirements.txt"
        )
    except Exception as exc:
        raise ConfigError(f"Could not read {config_path}: {exc}")

    if not isinstance(raw, dict):
        raise ConfigError("config.yaml must contain a mapping of settings (see the example file).")

    errors: List[str] = []
    warnings: List[str] = []

    # --- connection ---
    conn_raw = _section(raw, "connection", errors)
    host = _text(conn_raw, "host", "", errors, "connection.host",
                 required=True)
    if host and not _looks_like_host(host):
        warnings.append("connection.host does not look like an IP address or hostname - please check.")
    conn = ConnCfg(
        host=host,
        port=_int(conn_raw, "port", 5000, errors, "connection.port"),
        reconnect=_bool(conn_raw, "reconnect", True, errors, "connection.reconnect"),
        reconnect_min_seconds=_float(conn_raw, "reconnect_min_seconds", 3.0, errors, "connection.reconnect_min_seconds"),
        reconnect_max_seconds=_float(conn_raw, "reconnect_max_seconds", 60.0, errors, "connection.reconnect_max_seconds"),
    )
    if conn.reconnect_max_seconds < conn.reconnect_min_seconds:
        warnings.append("connection.reconnect_max_seconds is smaller than reconnect_min_seconds - swapped for you.")
        conn.reconnect_min_seconds, conn.reconnect_max_seconds = conn.reconnect_max_seconds, conn.reconnect_min_seconds

    # --- bot ---
    bot_raw = _section(raw, "bot", errors)
    bot = BotCfg(
        advertise_on_start=_bool(bot_raw, "advertise_on_start", True, errors, "bot.advertise_on_start"),
        answer_unknown_senders=_bool(bot_raw, "answer_unknown_senders", False, errors, "bot.answer_unknown_senders"),
    )

    # --- mesh ---
    mesh_raw = _section(raw, "mesh", errors)
    unknown = _text(mesh_raw, "unknown_hops", "ignore", errors, "mesh.unknown_hops")
    if unknown not in ("ignore", "respond"):
        errors.append(f"mesh.unknown_hops must be 'ignore' or 'respond' (found '{unknown}').")
        unknown = "ignore"
    sender_name = _text(mesh_raw, "channel_sender_name", "trust", errors,
                        "mesh.channel_sender_name")
    if sender_name not in ("trust", "smart", "off"):
        errors.append("mesh.channel_sender_name must be 'trust', 'smart' or 'off' "
                      f"(found '{sender_name}').")
        sender_name = "trust"
    mesh = MeshCfg(
        max_inbound_hops=max(0, _int(mesh_raw, "max_inbound_hops", 0, errors, "mesh.max_inbound_hops")),
        unknown_hops=unknown,
        channel_sender_name=sender_name,
    )

    # --- channels ---
    channels_raw = raw.get("channels")
    channels: List[ChannelCfg] = []
    if isinstance(channels_raw, list):
        seen: set = set()
        for i, item in enumerate(channels_raw):
            if not isinstance(item, dict):
                errors.append(f"channels[{i}] should be a mapping with a 'name'.")
                continue
            name = _text(item, "name", "", errors, f"channels[{i}].name", required=True)
            if not name.startswith("#"):
                name = "#" + name.lstrip("#")
            if name in seen:
                errors.append(f"Channel '{name}' is listed more than once.")
                continue
            seen.add(name)
            secret = item.get("secret_hex")
            channels.append(ChannelCfg(
                name=name,
                reply=_bool(item, "reply", True, errors, f"channels[{i}].reply"),
                secret_hex=secret if isinstance(secret, str) and secret else None,
            ))
    else:
        errors.append("config.yaml needs a 'channels' list - e.g. channels:\n  - name: \"#bot\"")

    # --- dm ---
    dm_raw = _section(raw, "dm", errors)
    admin_raw = dm_raw.get("admin_pubkey_prefixes", [])
    admin_prefixes: List[str] = []
    if isinstance(admin_raw, list):
        for item in admin_raw:
            if isinstance(item, str) and item.strip():
                admin_prefixes.append(item.strip().lower())
    if not admin_prefixes:
        warnings.append("dm.admin_pubkey_prefixes is empty - no node can run admin commands yet. " + DEFAULT_ADMIN_HELP)
    dm = DmCfg(enabled=_bool(dm_raw, "enabled", True, errors, "dm.enabled"),
               admin_pubkey_prefixes=admin_prefixes)

    # --- verbosity ---
    verb_raw = _section(raw, "verbosity", errors)
    channel_default = _text(verb_raw, "channel_default", "brief", errors, "verbosity.channel_default")
    dm_default = _text(verb_raw, "dm_default", "brief", errors, "verbosity.dm_default")
    if channel_default not in ("brief", "full"):
        errors.append(f"verbosity.channel_default must be 'brief' or 'full' (found '{channel_default}').")
        channel_default = "brief"
    if dm_default not in ("brief", "full"):
        errors.append(f"verbosity.dm_default must be 'brief' or 'full' (found '{dm_default}').")
        dm_default = "brief"
    verbosity = VerbosityCfg(
        aliases_brief=_string_list(verb_raw.get("aliases_brief", []), errors, "verbosity.aliases_brief"),
        aliases_full=_string_list(verb_raw.get("aliases_full", []), errors, "verbosity.aliases_full"),
        channel_default=channel_default,
        dm_default=dm_default,
    )

    # --- storage ---
    store_raw = _section(raw, "storage", errors)
    raw_hex = _bool(store_raw, "packet_raw_hex", False, errors, "storage.packet_raw_hex")
    capture_packets = _bool(store_raw, "capture_packets", True, errors, "storage.capture_packets")
    if raw_hex and not capture_packets:
        warnings.append("storage.packet_raw_hex is true but storage.capture_packets is false - "
                        "raw capture turned on anyway.")
        capture_packets = True
    storage = StorageCfg(
        db_path=_text(store_raw, "db_path", "data/bot.db", errors, "storage.db_path"),
        contact_refresh_minutes=max(1, _int(store_raw, "contact_refresh_minutes", 30, errors, "storage.contact_refresh_minutes")),
        capture_packets=capture_packets,
        packet_raw_hex=raw_hex,
        packet_jsonl=_text(store_raw, "packet_jsonl", "data/packets.jsonl", errors, "storage.packet_jsonl"),
        packet_max_rows=max(1000, _int(store_raw, "packet_max_rows", 200000, errors, "storage.packet_max_rows")),
    )

    # --- replies (canned) ---
    replies: List[ReplyRule] = []
    replies_raw = raw.get("replies")
    if isinstance(replies_raw, list):
        for i, item in enumerate(replies_raw):
            if not isinstance(item, dict):
                errors.append(f"replies[{i}] should be a mapping with 'keywords' and 'text'.")
                continue
            keywords = _string_list(item.get("keywords", []), errors, f"replies[{i}].keywords")
            if not keywords:
                errors.append(f"replies[{i}] needs at least one keyword.")
                continue
            text_value = item.get("text")
            if isinstance(text_value, str):
                texts = [text_value]
            elif isinstance(text_value, list):
                texts = [str(t) for t in text_value if isinstance(t, str) and t.strip()]
            else:
                texts = []
            if not texts:
                errors.append(f"replies[{i}] needs a 'text' string (or list of strings).")
                continue
            replies.append(ReplyRule(keywords=[k.lower() for k in keywords], texts=texts))
    else:
        errors.append("config.yaml needs a 'replies' list (can be empty: replies: [])")

    # --- limits ---
    limits_raw = _section(raw, "limits", errors)
    limits = LimitsCfg(
        min_interval_seconds=max(0.0, _float(limits_raw, "min_interval_seconds", 3.0, errors, "limits.min_interval_seconds")),
        per_sender_seconds=max(0.0, _float(limits_raw, "per_sender_seconds", 30.0, errors, "limits.per_sender_seconds")),
        max_reply_length=max(40, _int(limits_raw, "max_reply_length", 133, errors, "limits.max_reply_length")),
        max_chunks=max(1, _int(limits_raw, "max_chunks", 6, errors, "limits.max_chunks")),
    )

    # --- web ---
    web_raw = _section(raw, "web", errors)
    web = WebCfg(
        enabled=_bool(web_raw, "enabled", False, errors, "web.enabled"),
        host=_text(web_raw, "host", "127.0.0.1", errors, "web.host"),
        port=_int(web_raw, "port", 8081, errors, "web.port"),
        password=str(web_raw.get("password", "") or ""),
    )
    if web.enabled and web.host not in ("127.0.0.1", "localhost", "::1") and not web.password:
        warnings.append("web.host is not loopback and web.password is empty - the dashboard "
                        "will be open to your whole network. Set a password!")

    # --- logging ---
    log_raw = _section(raw, "logging", errors)
    tz = _text(log_raw, "timezone", "local", errors, "logging.timezone")
    tz_iana = None
    if tz not in ("local", "utc"):
        tz_iana = tz
        tz = "iana"
    log_cfg = LogCfg(
        level=_text(log_raw, "level", "INFO", errors, "logging.level").upper(),
        file=_text(log_raw, "file", "", errors, "logging.file"),
        timezone=tz,
        tz_iana=tz_iana,
    )
    if log_cfg.level not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
        errors.append(f"logging.level must be one of DEBUG, INFO, WARNING, ERROR, CRITICAL (found '{log_cfg.level}').")

    if errors:
        pretty = "\n".join(f"  - {e}" for e in errors)
        raise ConfigError(f"config.yaml has {len(errors)} problem(s):\n{pretty}")

    return Settings(
        connection=conn,
        bot=bot,
        mesh=mesh,
        channels=channels,
        dm=dm,
        verbosity=verbosity,
        storage=storage,
        replies=replies,
        limits=limits,
        web=web,
        logging=log_cfg,
        config_path=config_path,
        warnings=warnings,
        raw=raw,
    )


# --------------------------------------------------------------------------
# Small helpers used by the parser
# --------------------------------------------------------------------------

def _section(data: Dict[str, Any], name: str, errors: List[str]) -> Dict[str, Any]:
    value = data.get(name)
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    errors.append(f"'{name}' must be a section with settings below it.")
    return {}


def _text(data: Dict[str, Any], key: str, default: str, errors: List[str], where: str,
          required: bool = False) -> str:
    value = data.get(key, default)
    if value is None:
        value = default
    if not isinstance(value, str) or not value.strip():
        if required:
            errors.append(f"'{where}' is required (put the value in quotes).")
        return default if isinstance(default, str) else ""
    return value.strip()


def _int(data: Dict[str, Any], key: str, default: int, errors: List[str], where: str) -> int:
    value = data.get(key, default)
    try:
        return int(value)
    except (TypeError, ValueError):
        errors.append(f"'{where}' must be a whole number (found '{value}').")
        return default


def _float(data: Dict[str, Any], key: str, default: float, errors: List[str], where: str) -> float:
    value = data.get(key, default)
    try:
        return float(value)
    except (TypeError, ValueError):
        errors.append(f"'{where}' must be a number (found '{value}').")
        return default


def _bool(data: Dict[str, Any], key: str, default: bool, errors: List[str], where: str) -> bool:
    value = data.get(key, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ("true", "yes", "on", "1"):
        return True
    if isinstance(value, str) and value.strip().lower() in ("false", "no", "off", "0"):
        return False
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    errors.append(f"'{where}' must be true or false (found '{value}').")
    return default


def _string_list(value: Any, errors: List[str], where: str) -> List[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        errors.append(f"'{where}' must be a list of words in quotes.")
        return []
    result: List[str] = []
    for item in value:
        if isinstance(item, str) and item.strip():
            result.append(item.strip().lower())
    return result


def _unique(items: List[str]) -> List[str]:
    seen: set = set()
    out: List[str] = []
    for item in items:
        low = item.lower()
        if low not in seen:
            seen.add(low)
            out.append(low)
    return out


def _looks_like_host(host: str) -> bool:
    import re
    if re.match(r"^[\w.\-]+$", host) and " " not in host:
        return True
    return False


def sanitized_snapshot(settings: Settings) -> Dict[str, Any]:
    """Safe view of the config for the web dashboard (password masked)."""
    return {
        "connection": {
            "host": settings.connection.host,
            "port": settings.connection.port,
            "reconnect": settings.connection.reconnect,
        },
        "bot": {"advertise_on_start": settings.bot.advertise_on_start,
                 "answer_unknown_senders": settings.bot.answer_unknown_senders},
        "mesh": {"max_inbound_hops": settings.mesh.max_inbound_hops,
                 "unknown_hops": settings.mesh.unknown_hops,
                 "channel_sender_name": settings.mesh.channel_sender_name},
        "channels": [{"name": c.name, "reply": c.reply} for c in settings.channels],
        "dm": {"enabled": settings.dm.enabled,
               "admin_pubkey_prefixes": settings.dm.admin_pubkey_prefixes},
        "verbosity": {"channel_default": settings.verbosity.channel_default,
                      "dm_default": settings.verbosity.dm_default,
                      "aliases_brief": settings.verbosity.aliases_brief,
                      "aliases_full": settings.verbosity.aliases_full},
        "storage": {"db_path": settings.storage.db_path,
                    "contact_refresh_minutes": settings.storage.contact_refresh_minutes},
        "limits": {"min_interval_seconds": settings.limits.min_interval_seconds,
                   "per_sender_seconds": settings.limits.per_sender_seconds,
                   "max_reply_length": settings.limits.max_reply_length,
                   "max_chunks": settings.limits.max_chunks},
        "web": {"enabled": settings.web.enabled, "host": settings.web.host,
                "port": settings.web.port,
                "password": "set" if settings.web.password else ""},
        "logging": {"level": settings.logging.level, "file": settings.logging.file,
                    "timezone": settings.logging.timezone,
                    "tz_iana": settings.logging.tz_iana},
    }
