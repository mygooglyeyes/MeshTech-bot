"""v0.0.141 (Brett): the dashboard header shows the bot's own public key -
truncated on screen, with a copy button for the full key. The status
endpoint therefore must expose it in both radio modes (and hide the line
when it is unknown)."""
from __future__ import annotations

from types import SimpleNamespace


def _make_service(make_config):
    from core.config import load
    from core.feed import FeedHub
    from core.service import BotService
    from core.store import Store

    settings = load(make_config())
    store = Store(settings.storage.db_path)
    return BotService(settings, store, FeedHub()), store


def test_status_snapshot_contains_own_pubkey_mcp_mode(make_config):
    service, store = _make_service(make_config)
    try:
        key = "ab" * 32  # 64 hex chars, like a real radio public key
        service.mcp = SimpleNamespace(
            own_pubkey=key,
            is_running=False,
            stats=SimpleNamespace(rx_count=0, tx_count=0),
            modem_client=None,          # v0.0.173 TCP Push chip state
        )
        snap = service.status_snapshot()
        assert snap["own_pubkey"] == key
    finally:
        store.close()


def test_status_snapshot_companion_mode_reads_client(make_config):
    service, store = _make_service(make_config)
    try:
        service.client = SimpleNamespace(
            own_pubkey="CD" * 32,  # deliberately uppercase: stored lowercased
            own_name="",
            is_connected=False,
            channel_names=lambda: {},
        )
        snap = service.status_snapshot()
        assert snap["own_pubkey"] == "cd" * 32
    finally:
        store.close()


def test_status_snapshot_own_pubkey_empty_without_radio(make_config):
    # No client and no MCP (fresh service): the field still exists and is
    # empty - the dashboard hides the key line instead of rendering "-".
    service, store = _make_service(make_config)
    try:
        snap = service.status_snapshot()
        assert snap["own_pubkey"] == ""
    finally:
        store.close()


def test_mcp_own_pubkey_property_derives_from_identity():
    # The radio driver's Identity class is Linux-only, so exercise the
    # property logic directly with a stub.
    from core.mcp import Mcp

    class FakeIdentity:
        def get_public_key(self):
            return bytes(range(32))

    ok = SimpleNamespace(identity=FakeIdentity())
    assert Mcp.own_pubkey.fget(ok) == bytes(range(32)).hex()

    broken = SimpleNamespace(identity=None)
    assert Mcp.own_pubkey.fget(broken) == ""
