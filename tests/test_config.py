"""Tests for core/config.py - loading and validation."""
from __future__ import annotations

import pytest
import yaml

from core.config import ConfigError, load, sanitized_snapshot


def test_short_admin_prefix_warns(make_config):
    path = make_config({"dm": {"admin_pubkey_prefixes": ["abcd"]}})
    settings = load(path)
    assert any("12-character hex" in w for w in settings.warnings)


def test_nonhex_admin_prefix_warns(make_config):
    path = make_config({"dm": {"admin_pubkey_prefixes": ["zzzyyyxxxwww"]}})
    settings = load(path)
    assert any("12-character hex" in w for w in settings.warnings)


def test_open_dashboard_warns_without_password(make_config):
    path = make_config({"web": {"enabled": True, "host": "0.0.0.0",
                                "password": ""}})
    settings = load(path)
    assert any("Set a password" in w for w in settings.warnings)


def test_plaintext_dashboard_warns_with_password(make_config):
    path = make_config({"web": {"enabled": True, "host": "0.0.0.0",
                                "password": "topsecret"}})
    settings = load(path)
    assert any("plaintext" in w for w in settings.warnings)


def test_password_file_supplies_password(make_config, tmp_path):
    secrets_file = tmp_path / "dashboard.secret"
    secrets_file.write_text("  file-secret\n", encoding="utf-8")
    path = make_config({"web": {"enabled": True, "host": "127.0.0.1",
                                "password": "inline-legacy",
                                "password_file": str(secrets_file)}})
    settings = load(path)
    assert settings.web.password == "file-secret"
    # the file source suppresses the legacy-inline warning
    assert not any("inside config.yaml" in w for w in settings.warnings)


def test_env_password_wins_over_all(monkeypatch, make_config, tmp_path):
    secrets_file = tmp_path / "dashboard.secret"
    secrets_file.write_text("file-secret\n", encoding="utf-8")
    monkeypatch.setenv("MESHTECH_DASHBOARD_PASSWORD", "env-secret")
    path = make_config({"web": {"enabled": True, "host": "127.0.0.1",
                                "password": "inline-legacy",
                                "password_file": str(secrets_file)}})
    settings = load(path)
    assert settings.web.password == "env-secret"


def test_missing_password_file_warns_and_stays_unset(make_config, tmp_path):
    path = make_config({"web": {"enabled": True, "host": "127.0.0.1",
                                "password_file": str(tmp_path / "missing.txt")}})
    settings = load(path)
    assert settings.web.password == ""
    assert any("could not be read" in w for w in settings.warnings)


def test_empty_password_file_warns(make_config, tmp_path):
    secrets_file = tmp_path / "dashboard.secret"
    secrets_file.write_text("\n", encoding="utf-8")
    path = make_config({"web": {"enabled": True, "host": "127.0.0.1",
                                "password_file": str(secrets_file)}})
    settings = load(path)
    assert settings.web.password == ""
    assert any("is empty" in w for w in settings.warnings)


def test_channel_cadence_limits_parse(make_config):
    path = make_config({"limits": {"channel_interval_seconds": 20.0,
                                   "channel_intervals": {"#bot": 60, "test": 0}}})
    settings = load(path)
    assert settings.limits.channel_interval_seconds == 20.0
    # a missing '#' is normalised, values become floats
    assert settings.limits.channel_intervals == {"#bot": 60.0, "#test": 0.0}


def test_channel_cadence_default_off(make_config):
    settings = load(make_config())
    assert settings.limits.channel_interval_seconds == 0.0
    assert settings.limits.channel_intervals == {}


def test_channel_cadence_unknown_channel_warns(make_config):
    path = make_config({"limits": {"channel_intervals": {"#nope": 30}}})
    settings = load(path)
    assert any("not in the channels list" in w for w in settings.warnings)


def test_channel_cadence_bad_value_rejected(make_config):
    path = make_config({"limits": {"channel_intervals": {"#bot": "soon"}}})
    with pytest.raises(ConfigError):
        load(path)


def test_legacy_inline_password_warns(make_config):
    path = make_config({"web": {"enabled": True, "host": "127.0.0.1",
                                "password": "inline-secret"}})
    settings = load(path)
    assert settings.web.password == "inline-secret"  # legacy still works
    assert any("inside config.yaml" in w for w in settings.warnings)


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
    # Capture flags are exposed so the dashboard can show whether raw
    # packet capture is actually running.
    assert "packet_raw_hex" in snapshot["storage"]
    assert "capture_packets" in snapshot["storage"]


def test_invalid_yaml_gives_friendly_error(tmp_path):
    bad = tmp_path / "config.yaml"
    bad.write_text("connection: [unclosed\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load(str(bad))


def test_check_writes_replies_as_lowercased(make_config):
    path = make_config({"replies": [{"keywords": ["Hello"], "text": "hi"}]})
    settings = load(path)
    assert settings.replies[0].keywords == ["hello"]
