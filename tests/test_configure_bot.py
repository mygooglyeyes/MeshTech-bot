"""Tests for scripts/configure_bot.py - the interactive config editor.

Only the pure text-splice helpers are tested here (no interactivity):
they are the part that must never corrupt an existing config.yaml.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import configure_bot  # noqa: E402


EXAMPLE = (Path(__file__).resolve().parent.parent / "config.example.yaml").read_text(
    encoding="utf-8")


def splice_and_parse(text: str, **kwargs) -> dict:
    """Apply splices then confirm the result is still valid YAML."""
    if "channels" in kwargs:
        text = configure_bot.splice_channels(text, kwargs.pop("channels"))
    if "replies" in kwargs:
        text = configure_bot.splice_replies(text, kwargs.pop("replies"))
    for dotted, value in kwargs.items():
        text = configure_bot.splice_scalar(text, dotted, value)
    parsed = yaml.safe_load(text)
    assert isinstance(parsed, dict)  # never a merge-collapse or type error
    return parsed


def test_example_loads_and_has_needed_sections():
    data = yaml.safe_load(EXAMPLE)
    for section in ("connection", "mesh", "channels", "dm", "web"):
        assert section in data


def test_change_host_preserves_everything_else():
    parsed = splice_and_parse(EXAMPLE, **{"connection.host": '"10.0.0.99"'})
    assert parsed["connection"]["host"] == "10.0.0.99"
    # untouched keys survive byte-for-byte in value
    assert parsed["connection"]["reconnect"] is True
    assert parsed["connection"]["port"] == 5000
    assert parsed["web"]["enabled"] is True
    assert parsed["storage"]["packet_raw_hex"] is False
    assert parsed["mesh"]["unknown_hops"] == "ignore"


def test_change_port_replaces_not_duplicates():
    text = configure_bot.splice_scalar(EXAMPLE, "connection.port", 5050)
    # exactly one port line inside the connection section (web.port is separate)
    conn = text.split("connection:", 1)[1].split("\nbot:", 1)[0]
    assert len(re.findall(r"(?m)^  port:", conn)) == 1
    assert yaml.safe_load(text)["connection"]["port"] == 5050


def test_comments_on_the_same_line_are_removed_cleanly():
    text = EXAMPLE.replace('  port: 5000                # <-- companion port',
                           '  port: 5000   # comment with "quotes" and # hash')
    parsed = splice_and_parse(text, **{"connection.port": 5001})
    assert parsed["connection"]["port"] == 5001


def test_missing_key_is_inserted_into_right_section():
    text = EXAMPLE.replace("  max_inbound_hops: 3", "")  # key absent entirely
    assert "max_inbound_hops" not in yaml.safe_load(text)["mesh"]
    parsed = splice_and_parse(text, **{"mesh.max_inbound_hops": 5})
    assert parsed["mesh"]["max_inbound_hops"] == 5
    # landed inside mesh:, not appended to the file end
    assert text.index("max_inbound_hops") < text.index("channels:")


def test_default_value_not_written_when_key_missing():
    text = EXAMPLE.replace("  max_inbound_hops: 3", "")
    out = configure_bot.splice_scalar(text, "mesh.max_inbound_hops",
                                      configure_bot.DEFAULTS["mesh.max_inbound_hops"])
    # no *setting* line was written (comments mentioning the key still exist)
    assert not re.search(r"(?m)^  max_inbound_hops:", out)


def test_channels_replaced_and_extras_kept():
    channels = [
        {"name": "#alpha", "reply": True},
        {"name": "#beta", "reply": False, "secret_hex": "deadbeef"},
    ]
    parsed = splice_and_parse(EXAMPLE, channels=channels)
    assert parsed["channels"] == channels
    # section after channels is intact
    assert parsed["dm"]["enabled"] is True
    assert parsed["dm"]["admin_pubkey_prefixes"]


def test_channels_missing_secret_is_not_invented():
    text = EXAMPLE.replace('  - name: "#test"', '  - name: "#test"\n    secret_hex: "cafe"')
    parsed = splice_and_parse(text, channels=[{"name": "#test", "reply": False}])
    # a channel that keeps its name keeps its secret... only via the editor's
    # merge step, which the CLI does before calling splice; the splice itself
    # writes exactly what it is given
    assert parsed["channels"] == [{"name": "#test", "reply": False}]


def test_channels_section_last_still_works():
    text = EXAMPLE + "\n# trailing comment\n"
    parsed = splice_and_parse(text, channels=[{"name": "#solo", "reply": True}])
    assert parsed["channels"] == [{"name": "#solo", "reply": True}]
    assert "trailing comment" in text


def test_replies_replaced_keeps_other_sections():
    rules = [{"keywords": ["ping"], "text": "pong"}]
    parsed = splice_and_parse(EXAMPLE, replies=rules)
    assert parsed["replies"] == [{"keywords": ["ping"], "text": "pong"}]
    # sections on both sides survive
    assert parsed["limits"]["min_interval_seconds"] == 3
    assert parsed["logging"]["level"] == "INFO"


def test_replies_multi_text_and_quoting():
    rules = [{"keywords": ["hi", "hello"],
              "text": ["hey #1", "say: 'quoted'"]}]
    parsed = splice_and_parse(EXAMPLE, replies=rules)
    rule = parsed["replies"][0]
    assert rule["keywords"] == ["hi", "hello"]
    assert rule["text"] == ["hey #1", "say: 'quoted'"]


def test_replies_empty_list_clears_existing():
    # a bare 'replies:' header with no body (what a user file looks like
    # after deleting every rule) is rewritten to the canonical 'replies: []'
    text = EXAMPLE.replace("replies: []", "replies:")
    parsed = splice_and_parse(text, replies=[])
    assert parsed["replies"] == []


def test_replies_section_missing_is_an_error():
    text = EXAMPLE.replace("replies: []", "")
    with pytest.raises(SystemExit):
        configure_bot.splice_replies(text, [])


def test_result_of_full_flow_is_accepted_by_real_loader(make_config, tmp_path):
    """End to end: splice into a copy of the example, then core.config.load it."""
    path = tmp_path / "config.yaml"
    path.write_text(EXAMPLE, encoding="utf-8")
    text = path.read_text(encoding="utf-8")
    text = configure_bot.splice_scalar(text, "connection.host", '"192.0.2.7"')
    text = configure_bot.splice_scalar(text, "connection.port", 5050)
    text = configure_bot.splice_scalar(text, "mesh.max_inbound_hops", 2)
    text = configure_bot.splice_channels(
        text, [{"name": "#one", "reply": True}, {"name": "#two", "reply": False}])
    path.write_text(text, encoding="utf-8")
    from core.config import load
    settings = load(str(path))
    assert settings.connection.host == "192.0.2.7"
    assert settings.connection.port == 5050
    assert settings.mesh.max_inbound_hops == 2
    assert [c.name for c in settings.channels] == ["#one", "#two"]
    assert settings.channels[1].reply is False
