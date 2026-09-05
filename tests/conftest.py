"""Shared test fixtures. Tests need no radio or network access."""
from __future__ import annotations

import pathlib
import sys

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def base_settings_dict() -> dict:
    return {
        "connection": {"host": "192.168.1.50", "port": 5000, "reconnect": False},
        "bot": {"advertise_on_start": False, "answer_unknown_senders": False},
        "mesh": {"max_inbound_hops": 2, "unknown_hops": "ignore"},
        "channels": [
            {"name": "#bot", "reply": True},
            {"name": "#diagnostics", "reply": False},
        ],
        "dm": {"enabled": True, "admin_pubkey_prefixes": ["aabbccddeeff"]},
        "verbosity": {
            "aliases_brief": [],
            "aliases_full": ["x"],
            "channel_default": "brief",
            "dm_default": "brief",
        },
        "storage": {"db_path": "data/bot.db", "contact_refresh_minutes": 30,
                    "capture_packets": True, "packet_raw_hex": False,
                    "packet_jsonl": "", "packet_max_rows": 200000},
        "replies": [
            {"keywords": ["hello", "hi"], "text": ["Hello there!", "Hi!"]},
        ],
        "limits": {"min_interval_seconds": 0.0, "per_sender_seconds": 0.0,
                   "max_reply_length": 133, "max_chunks": 6},
        "web": {"enabled": False, "host": "127.0.0.1", "port": 8081,
                "password": "secret"},
        "logging": {"level": "WARNING", "file": "", "timezone": "utc"},
    }


@pytest.fixture
def make_config(tmp_path):
    """Factory: write a config.yaml file and return its path."""

    def _make(extra: dict | None = None, db_name: str = "bot.db") -> str:
        data = base_settings_dict()
        data["storage"]["db_path"] = str(tmp_path / db_name)
        if extra:
            _deep_update(data, extra)
        path = tmp_path / "config.yaml"
        path.write_text(yaml.safe_dump(data), encoding="utf-8")
        return str(path)

    return _make


def _deep_update(target: dict, extra: dict) -> None:
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _deep_update(target[key], value)
        else:
            target[key] = value


@pytest.fixture
def settings(make_config):
    from core.config import load
    return load(make_config())
