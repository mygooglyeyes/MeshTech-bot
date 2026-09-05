"""Tests for core/config.py - loading and validation."""
from __future__ import annotations

import pytest
import yaml

from core.config import ConfigError, load, sanitized_snapshot


def test_loads_valid_config(make_config):
    path = make_config()
    settings = load(path)
    assert settings.connection.host == "192.168.1.50"
    assert settings.connection.port == 5000
    assert [c.name for c in settings.channels] == ["#bot", "#diagnostics"]
    assert settings.dm.admin_pubkey_prefixes == ["aabbccddeeff"]
    assert settings.mesh.max_inbound_hops == 2


def test_missing_file_raises(tmp_path):
    with pytest.raises(ConfigError):
        load(str(tmp_path / "nope.yaml"))


def test_channel_names_normalised_and_deduplicated(make_config):
    path = make_config({"channels": [{"name": "bot", "reply": True},
                                     {"name": "#bot", "reply": True}]})
    with pytest.raises(ConfigError) as exc:
        load(path)
    assert "listed more than once" in str(exc.value)


def test_bad_unknown_hops_rejected(make_config):
    path = make_config({"mesh": {"unknown_hops": "maybe"}})
    with pytest.raises(ConfigError):
        load(path)


def test_bad_channel_sender_name_rejected(make_config):
    path = make_config({"mesh": {"channel_sender_name": "sometimes"}})
    with pytest.raises(ConfigError):
        load(path)


def test_channel_sender_name_modes_accepted(make_config):
    for mode in ("trust", "smart", "off"):
        settings = load(make_config({"mesh": {"channel_sender_name": mode}}))
        assert settings.mesh.channel_sender_name == mode
    # default
    assert load(make_config()).mesh.channel_sender_name == "trust"


def test_empty_admin_prefix_is_warning_not_error(make_config):
    path = make_config({"dm": {"admin_pubkey_prefixes": []}})
    settings = load(path)
    assert settings.dm.admin_pubkey_prefixes == []
    assert any("admin_pubkey_prefixes" in w for w in settings.warnings)


def test_modifier_aliases_canonical(make_config):
    path = make_config({
        "verbosity": {"aliases_brief": ["tldr"], "aliases_full": ["lots"]},
    })
    settings = load(path)
    # canonical words brief/full always accepted even if not in the lists
    assert settings.verbosity.level_for_token("tldr") == "brief"
    assert settings.verbosity.level_for_token("brief") == "brief"
    assert settings.verbosity.level_for_token("lots") == "full"
    assert settings.verbosity.level_for_token("full") == "full"
    assert settings.verbosity.level_for_token("banana") is None


def test_sanitized_snapshot_masks_password(settings):
    snapshot = sanitized_snapshot(settings)
    assert snapshot["web"]["password"] == "set"
    assert "secret" not in str(snapshot)


def test_invalid_yaml_gives_friendly_error(tmp_path):
    bad = tmp_path / "config.yaml"
    bad.write_text("connection: [unclosed\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load(str(bad))


def test_check_writes_replies_as_lowercased(make_config):
    path = make_config({"replies": [{"keywords": ["Hello"], "text": "hi"}]})
    settings = load(path)
    assert settings.replies[0].keywords == ["hello"]
