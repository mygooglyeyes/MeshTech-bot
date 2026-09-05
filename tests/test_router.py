"""Tests for core/router.py - guards, trigger parsing, dispatch.

Runs fully offline: messages are fed in as InboundMessage objects and a
FakeClient records whatever the bot would transmit.
"""
from __future__ import annotations

import asyncio
import time

from core.config import load
from core.feed import FeedHub
from core.models import InboundMessage
from core.router import (handler_args, resolve_verbosity, select_handler,
                         tokenize)
from core.service import BotService
from core.store import Store


class FakeClient:
    is_connected = True

    def __init__(self):
        self.sent = []  # (kind, target, text)

    async def send_channel(self, idx, text):
        self.sent.append(("channel", idx, text))
        return True

    async def send_dm(self, prefix, text):
        self.sent.append(("dm", prefix, text))
        return True


async def _run_router(make_config, messages, extra=None, prepare=None):
    path = make_config(extra)
    settings = load(path)
    store = Store(settings.storage.db_path)
    try:
        if prepare is not None:
            prepare(store)
        service = BotService(settings, store, FeedHub())
        router = __import__("core.router", fromlist=["Router"]).Router(service)
        service.router = router
        client = FakeClient()
        service.client = client
        for message in messages:
            await router.on_inbound(message)
        return client.sent
    finally:
        store.close()


def _msg(kind, text, hops=None, channel="#bot", sender="aabbccddeeff",
         recv_delay=0.0):
    return InboundMessage(
        kind=kind, text=text,
        channel_name=channel if kind == "channel" else None,
        channel_idx=1 if kind == "channel" else None,
        sender_prefix=sender if kind == "dm" else None,
        sender_ts=None,
        recv_ts=time.time() - recv_delay,
        hops=hops,
    )


def _channel(text, hops=None, channel="#bot", recv_delay=0.0):
    return _msg("channel", text, hops=hops, channel=channel,
                recv_delay=recv_delay)


def _dm(text, sender="aabbccddeeff", hops=0, recv_delay=0.0):
    return _msg("dm", text, hops=hops, sender=sender,
                recv_delay=recv_delay)


# ------------------------------------------------------------- pure parsing

def test_tokenize_strips_command_mark():
    assert tokenize("!nodes full") == (["nodes", "full"], True)
    assert tokenize("!nodes x") == (["nodes", "x"], True)
    assert tokenize("hello world") == (["hello", "world"], False)


def test_tokenize_strips_glued_colons():
    # Punctuation glued to a word must not stop it matching
    assert tokenize("hello: anyone?") == (["hello", "anyone?"], False)
    assert tokenize("!help: now") == (["help", "now"], True)
    # A mid-message '!' stays glued so 'Alice: !help' is name + command body
    assert tokenize("Alice: !help") == (["alice", "!help"], False)


def test_split_channel_text():
    from core.models import split_channel_text
    # MeshCore group messages are '<sender name>: <body>' per the protocol docs
    assert split_channel_text("user123: I'm on my way") == ("user123", "I'm on my way")
    assert split_channel_text("LoganHome\U0001f3e0: !help") == ("LoganHome\U0001f3e0", "!help")
    # Text without a ': ' prefix (or with an empty name) is left untouched
    assert split_channel_text("!status full") == (None, "!status full")
    assert split_channel_text(": starts with colon") == (None, ": starts with colon")
    assert split_channel_text("") == (None, "")
    # Only the first ': ' splits - the body may contain colons
    assert split_channel_text("Node 7: meet at 5:30") == ("Node 7", "meet at 5:30")


def test_split_channel_text_name_length_boundary():
    from core.models import MAX_SENDER_NAME_LEN, split_channel_text
    # Protocol advert names are chars(32); accept names up to the cap
    name = "N" * MAX_SENDER_NAME_LEN
    assert split_channel_text(f"{name}: body") == (name, "body")
    # ...but a longer prefix is treated as message text, not a name
    too_long = "N" * (MAX_SENDER_NAME_LEN + 1)
    assert split_channel_text(f"{too_long}: body") == (None, f"{too_long}: body")


def test_split_channel_text_multiline():
    from core.models import split_channel_text
    # A line break inside the candidate name means it is not a name
    assert split_channel_text("line1\nJohn: hi") == (None, "line1\nJohn: hi")
    assert split_channel_text("John\r\nDoe: hi") == (None, "John\r\nDoe: hi")
    # Line breaks in the BODY are fine
    assert split_channel_text("John: line1\nline2") == ("John", "line1\nline2")


def test_split_channel_text_colons_and_spacing():
    from core.models import split_channel_text
    # A name may itself contain a colon - first ': ' still wins
    assert split_channel_text("user:123: hi") == ("user:123", "hi")
    # Extra spaces around the separator collapse into the body
    assert split_channel_text("John:  hello") == ("John", "hello")
    assert split_channel_text(" John : hello ") == ("John", "hello")
    # Colon without the space separator (or an empty name) is not a split
    assert split_channel_text("John:hello") == (None, "John:hello")
    assert split_channel_text("John:") == (None, "John:")
    # A trailing-separator-only message has no body after outer strip -
    # treat it as plain text (nothing to match either way)
    assert split_channel_text("John: ") == (None, "John:")


def test_verbosity_resolution():
    from core.config import VerbosityCfg
    cfg = VerbosityCfg()
    assert resolve_verbosity(["nodes"], cfg, "brief") == "brief"
    assert resolve_verbosity(["nodes", "x"], cfg, "brief") == "full"
    assert resolve_verbosity(["status", "brief", "x"], cfg, "brief") == "full"
    assert resolve_verbosity(["nodes", "full"], cfg, "brief") == "full"  # canonical word still works


def test_handler_args_remove_command_and_modifiers():
    from core.config import VerbosityCfg
    cfg = VerbosityCfg()
    assert handler_args(["nodes", "full", "K7ABC"], "nodes", cfg) == ["K7ABC"]
    assert handler_args(["nodes", "x", "K7ABC"], "nodes", cfg) == ["K7ABC"]
    assert handler_args(["path", "K7ABC"], "path", cfg) == ["K7ABC"]


def test_meshinfo_keyword_scopes():
    from handlers.meshinfo import MeshInfoHandler
    assert MeshInfoHandler.keyword_scope == {"nodes": "dm", "path": "both", "stats": "dm"}
    assert MeshInfoHandler.keyword_access == {"path": "public", "stats": "admin"}


def test_path_public_but_nodes_and_stats_restricted(make_config):
    sent = asyncio.run(_run_router(make_config, [
        _channel("Alice: !path K7ABC", hops=0),        # public path on a channel
        _channel("Alice: !nodes", hops=0),             # nodes DM-only -> silent
        _dm("!path K7ABC", sender="000011112222"),    # path DM from a stranger
        _dm("!stats K7ABC", sender="000011112222"),   # stats admin-only -> silent
    ]))
    kinds = [entry[0] for entry in sent]
    assert kinds.count("channel") == 1   # only the channel !path
    assert kinds.count("dm") == 1        # only the DM !path


def test_select_handler_respects_prefix_and_access():
    class H:
        def __init__(self, name, scope="both", access="public",
                     require_prefix=True, keywords=None, priority=100):
            self.name, self.keywords = name, keywords or [name]
            self.scope, self.access = scope, access
            self.require_prefix, self.priority = require_prefix, priority

    handlers = [H("status"), H("hello", require_prefix=False, priority=500)]
    # Without !, only the non-prefixed canned-style handler matches
    assert select_handler(["hello", "there"], False, handlers, "channel", False)[0].name == "hello"
    assert select_handler(["status"], False, handlers, "channel", False) is None
    picked = select_handler(["status"], True, handlers, "channel", False)
    assert picked[0].name == "status"

    admin = H("shutdown", scope="dm", access="admin")
    assert select_handler(["shutdown"], True, [admin], "dm", False) is None
    assert select_handler(["shutdown"], True, [admin], "dm", True) is not None


# ------------------------------------------------------------- router behaviour

def test_hop_filter_and_listen_only(make_config):
    sent = asyncio.run(_run_router(make_config, [
        _channel("Alice: !status", hops=1),      # within limit -> reply
        _channel("Alice: !status", hops=9),      # beyond limit -> no reply
        _channel("Alice: !status", hops=0, channel="#diagnostics"),  # listen-only
        _channel("Alice: !status", hops=0, channel="#other"),        # unconfigured
    ]))
    assert len(sent) == 1
    assert sent[0][0] == "channel"


def test_unknown_hops_policy(make_config):
    # unknown hops + policy=ignore -> silent (even for a valid command)
    dropped = asyncio.run(_run_router(make_config, [
        _channel("Alice: !status (ignore-unknown)", hops=None),
    ]))
    assert dropped == []

    # unknown hops + policy=respond -> reply is allowed
    responded = asyncio.run(_run_router(
        make_config, [_channel("Alice: !status (respond-unknown)", hops=None)],
        extra={"mesh": {"unknown_hops": "respond"}}))
    assert len(responded) == 1


def test_dm_access_gating(make_config):
    sent = asyncio.run(_run_router(make_config, [
        _dm("!shutdown", sender="aabbccddeeff"),   # admin -> allowed
        _dm("!shutdown", sender="000011112222"),   # stranger -> denied
        _dm("!nodes", sender="000011112222"),      # public command still works
    ]))
    kinds = [entry[1] for entry in sent]
    assert kinds.count("aabbccddeeff") == 1
    assert "shutting down" in sent[0][2].lower()
    assert len(sent) == 2


def test_canned_reply_without_prefix(make_config):
    sent = asyncio.run(_run_router(make_config, [
        _channel("Alice: hello mesh!", hops=0),
    ]))
    assert len(sent) == 1
    assert sent[0][2] in ("Hello there!", "Hi!")


def test_channel_sender_name_prefix_does_not_block_commands(make_config):
    # The radio embeds '<sender name>: ' in every group message; a command
    # after that prefix must still trigger (regression: !help never matched).
    sent = asyncio.run(_run_router(make_config, [
        _channel("LoganHome\U0001f3e0: !status", hops=0),
    ]))
    assert len(sent) == 1
    assert sent[0][0] == "channel"


def test_channel_sender_name_trust_always_strips_name(make_config):
    # "trust": the prefix is always treated as a sender name.
    # "Alice: hello" -> body "hello" still triggers canned...
    sent = asyncio.run(_run_router(make_config, [
        _channel("Alice: hello", hops=0),
    ]))
    assert len(sent) == 1
    assert sent[0][2] in ("Hello there!", "Hi!")
    # ...while "hello: world" does NOT - "hello" was eaten as a name.
    sent = asyncio.run(_run_router(make_config, [
        _channel("hello: world", hops=0),
    ]))
    assert sent == []


def test_channel_sender_name_smart_keeps_likely_message_text(make_config):
    # "smart": if the full text already matches a handler, the prefix is
    # treated as part of the message, not a sender name - so a bare
    # "hello: ..." message still answers as a greeting.
    sent = asyncio.run(_run_router(make_config, [
        _channel("hello: anyone around?", hops=0),
    ], extra={"mesh": {"channel_sender_name": "smart"},
             "bot": {"answer_unknown_senders": True}}))
    assert len(sent) == 1
    assert sent[0][2] in ("Hello there!", "Hi!")


def test_channel_sender_name_smart_still_splits_real_names(make_config):
    # "smart": a real "Name: !status" must still split so the command works.
    sent = asyncio.run(_run_router(make_config, [
        _channel("LoganHome\U0001f3e0: !status", hops=0),
    ], extra={"mesh": {"channel_sender_name": "smart"}}))
    assert len(sent) == 1
    assert sent[0][0] == "channel"


def test_channel_sender_name_off_never_splits(make_config):
    # "off": no embedded names on this mesh - commands arrive bare.
    sent = asyncio.run(_run_router(make_config, [
        _channel("LoganHome: !status", hops=0),
        _channel("!status", hops=0),
    ], extra={"mesh": {"channel_sender_name": "off"}}))
    assert len(sent) == 1  # only the bare command matches
    assert sent[0][0] == "channel"


def test_global_mute_blocks_replies(make_config):
    sent = asyncio.run(_run_router(
        make_config, [_channel("Alice: !status", hops=0)],
        prepare=lambda store: store.set_global_mute(True)))
    assert sent == []


def test_dedupe_same_message_answered_once(make_config):
    msg = _channel("Alice: hello again", hops=0, recv_delay=0.2)
    sent = asyncio.run(_run_router(make_config, [msg, msg]))
    assert len(sent) == 1


def test_verbosity_defaults_by_channel_and_dm(make_config):
    # The canned 'hello' response is constant; instead verify that a channel
    # reply for a verbose handler (status) fits one brief message.
    sent = asyncio.run(_run_router(make_config, [
        _channel("Alice: !status", hops=0),
    ]))
    assert len(sent) == 1
    assert "\n" not in sent[0][2] or len(sent[0][2]) <= 133


def test_blocked_dm_ignored_even_for_admin(make_config):
    # A blocked node is ignored entirely - admin commands do not bypass it.
    sent = asyncio.run(_run_router(make_config, [
        _dm("!shutdown", sender="aabbccddeeff"),
    ], prepare=lambda store: store.block_node("aabbccddeeff")))
    assert sent == []


def test_blocked_channel_sender_ignored(make_config):
    # The embedded channel name resolves to a known node whose prefix is
    # blocked -> the message is dropped without a reply.
    def prepare(store):
        store.upsert_node("aabbccddeeff" + "0" * 52, name="Alice")
        store.block_node("aabbccddeeff")

    sent = asyncio.run(_run_router(make_config, [
        _channel("Alice: !status", hops=0),
    ], prepare=prepare))
    assert sent == []


def test_blocked_then_unblocked_replies_again(make_config):
    def prepare(store):
        store.block_node("aabbccddeeff")
        store.unblock_node("aabbccddeeff")

    sent = asyncio.run(_run_router(make_config, [
        _dm("!help", sender="aabbccddeeff"),
    ], prepare=prepare))
    assert any(entry[0] == "dm" for entry in sent)


def test_unknown_sender_ignored_by_default(make_config):
    # Fail closed: no sender identity -> no answer (default).
    sent = asyncio.run(_run_router(make_config, [
        _channel("!status", hops=0),                 # no embedded name
        _dm("!help", sender=None),                  # DM without a prefix
    ]))
    assert sent == []


def test_unknown_sender_answered_when_opted_in(make_config):
    sent = asyncio.run(_run_router(
        make_config, [
            _channel("!status", hops=0),
            _dm("!help", sender=None),
        ],
        extra={"bot": {"answer_unknown_senders": True}}))
    # both messages were answered (the DM help reply chunks into several msgs)
    assert any(entry[0] == "channel" for entry in sent)
    assert any(entry[0] == "dm" for entry in sent)
