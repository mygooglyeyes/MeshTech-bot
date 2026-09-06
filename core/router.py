"""Message router.

Every inbound message flows through here:

    normalise -> dedupe -> persist + publish -> guards (channel allowlist,
    reply-enabled, hop limit, DM access) -> trigger parse (command keyword,
    verbosity modifier, arguments) -> handler -> render -> send

Guard decisions are configurable and every one is logged, so behaviour is
easy to follow in the logs / dashboard.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .config import Settings, VerbosityCfg
from .feed import FeedHub
from .format import chunk_text
from .models import HandlerResult, InboundMessage, MsgRecord, split_channel_text
from .service import BotService

log = logging.getLogger("meshtech-bot.router")


def _log_line(text: str, limit: int = 80) -> str:
    """Radio text made safe for one log line.

    Message bodies are attacker-controlled (anyone on the mesh); collapsing
    every run of whitespace to a single space stops embedded newlines/tabs
    from forging extra log lines or faking journalctl entries.
    """
    return " ".join((text or "").split())[:limit]


# --------------------------------------------------------------------------
# Pure parsing helpers (unit-testable without a radio)
# --------------------------------------------------------------------------

def tokenize(raw: str) -> Tuple[List[str], bool]:
    """Split a message into (tokens, used_exclamation_mark).

    Trailing colons are stripped from each token so that punctuation glued
    to a word ("hello:", "!help:") does not stop it matching. The leading
    "!" is only honoured at the start of the message; a mid-message
    "!help" stays glued so embedded-sender-name parsing can tell "Alice:
    !help" apart from a bare command.
    """
    text = raw.strip()
    prefixed = text.startswith("!")
    if prefixed:
        text = text[1:].lstrip()
    tokens = [t.lower().rstrip(":") for t in text.split() if t]
    return tokens, prefixed


def _keyword_matches(keyword: str, tokens: List[str], prefixed: bool) -> bool:
    if " " not in keyword:
        if prefixed:
            return bool(tokens) and tokens[0] == keyword
        return keyword in tokens
    phrase = " ".join(tokens)
    if prefixed:
        return phrase == keyword or phrase.startswith(keyword + " ")
    return keyword in phrase


def expand_glued_x(tokens: List[str], prefixed: bool, handlers: List[Any]) -> List[str]:
    """Support '!pathx' as shorthand for '!path x' (extended modifier).

    Only fires when the message starts with '!', the first token is a known
    command keyword with a trailing 'x', and the base itself is a registered
    keyword - so plain words ('box'), bare '!x' and future keywords that
    legitimately end in 'x' are left alone.
    """
    if not prefixed or not tokens:
        return tokens
    first = tokens[0]
    if len(first) <= 1 or not first.endswith("x"):
        return tokens
    base = first[:-1]
    known = {kw for h in handlers for kw in getattr(h, "keywords", [])}
    if base in known:
        return [base] + tokens[1:] + ["x"]
    return tokens


def resolve_verbosity(tokens: List[str], cfg: VerbosityCfg, default: str) -> str:
    """Last explicit modifier word wins; otherwise use *default*."""
    level = default
    for token in tokens:
        found = cfg.level_for_token(token)
        if found:
            level = found
    return level


def select_handler(tokens: List[str], prefixed: bool, handlers: List[Any],
                   kind: str, is_admin: bool) -> Optional[Tuple[Any, str]]:
    """Choose the first eligible handler with a matching keyword.

    Handlers are pre-sorted by (priority, name). Returns (handler,
    matched_keyword) or None.
    """
    for handler in handlers:
        if kind == "channel" and handler.scope not in ("both", "channel"):
            continue
        if kind == "dm" and handler.scope not in ("both", "dm"):
            continue
        if handler.access == "admin" and not is_admin:
            continue
        if not prefixed and getattr(handler, "require_prefix", True):
            continue  # commands like !status only fire on an explicit prefix
        for keyword in handler.keywords:
            if _keyword_matches(keyword, tokens, prefixed):
                return handler, keyword
    return None


def resolve_channel_text(text: str, mode: str, handlers: List[Any],
                         is_admin: bool = False):
    """Split a channel message into (sender_name, body) per the configured
    mesh.channel_sender_name policy:

      "trust" - MeshCore always embeds the sender name ("Name: body"), so
                strip it. This is the protocol-compliant default.
      "smart" - strip the name, but only when the full text (prefix still
                attached) would NOT already match a handler. A message like
                "hello: anyone around?" then keeps "hello" as part of the
                message instead of being eaten as a sender name.
      "off"   - never strip. For gateways that relay messages without the
                embedded name (commands then arrive bare: "!help").
    """
    if mode == "off":
        return None, text
    name, body = split_channel_text(text)
    if mode == "smart" and name is not None:
        tokens, prefixed = tokenize(text)
        if tokens and select_handler(tokens, prefixed, handlers, "channel",
                                     is_admin) is not None:
            return None, text  # prefix is part of the message, not a name
    return name, body


def handler_args(tokens: List[str], command_word: str, cfg: VerbosityCfg) -> List[str]:
    """Remaining words after the command keyword + any modifier words."""
    args: List[str] = []
    for token in tokens:
        if token == command_word:
            continue
        if cfg.level_for_token(token):
            continue
        args.append(token)
    return args


# --------------------------------------------------------------------------
# Router
# --------------------------------------------------------------------------

@dataclass
class RouterCtx:
    """Everything a handler may need for one incoming message."""

    service: BotService
    msg: InboundMessage
    tokens: List[str]
    command: str          # matched keyword
    args: List[str]
    verbosity: str        # "brief" | "full"
    is_admin: bool

    @property
    def settings(self) -> Settings:
        return self.service.settings

    @property
    def store(self):
        return self.service.store

    @property
    def feed(self) -> FeedHub:
        return self.service.feed

    @property
    def now(self) -> float:
        return time.time()

    def sender_display(self) -> str:
        """Friendly name of the sender (DM) or the channel name."""
        if self.msg.kind == "dm":
            name = self.store.resolve_name(self.msg.sender_prefix) if self.msg.sender_prefix else None
            prefix = self.msg.sender_prefix or "?"
            return f"{name} ({prefix})" if name else prefix
        return self.msg.channel_name or "?"


class _Dedupe:
    def __init__(self, maxlen: int = 1000, window: float = 45.0):
        self.maxlen = maxlen
        self.window = window
        self._items: deque = deque()

    def seen_recently(self, key: str, now: float) -> bool:
        self._prune(now)
        for stamp, k in self._items:
            if k == key:
                return True
        return False

    def add(self, key: str, now: float) -> None:
        self._prune(now)
        self._items.append((now, key))
        if len(self._items) > self.maxlen:
            self._items.popleft()

    def _prune(self, now: float) -> None:
        while self._items and now - self._items[0][0] > self.window:
            self._items.popleft()


class Router:
    def __init__(self, service: BotService):
        self.service = service
        self.handlers: List[Any] = []
        self._dedupe = _Dedupe()
        # Handlers run OFF the inbound dispatch chain (see on_inbound): the
        # meshcore event loop awaits each event handler to completion, so a
        # slow command (weather's HTTP lookups, DB work) would hold up every
        # later mesh message. The semaphore caps how many run at once (a
        # burst must not stampede a Pi) and the reply lock keeps the
        # rate-limit bookkeeping and multi-chunk sends atomic.
        self._handler_gate = asyncio.Semaphore(2)
        self._handler_tasks: set = set()
        self._reply_lock = asyncio.Lock()
        self._last_reply_at = 0.0
        self._last_answer: Dict[str, float] = {}
        # Last-reply time per channel, for limits.channel_interval_seconds -
        # one busy channel must not reset the global pace for everyone else.
        self._last_channel_reply: Dict[str, float] = {}
        # Last-reply time per sender IN CHANNELS, for
        # limits.per_sender_channel_seconds - one persistent node must not
        # eat every cadence slot in a busy channel. DMs use _last_answer.
        self._last_channel_answer: Dict[str, float] = {}
        self._rebuild_registry()

    # ------------------------------------------------------------------ registry

    def _rebuild_registry(self) -> None:
        from handlers import discover_handlers  # local import: no radio needed

        instances = [cls() for cls in discover_handlers()]
        for instance in instances:
            instance.attach(self.service)
        instances.sort(key=lambda h: (h.priority, h.name))
        self.handlers = instances
        self.service.registry = instances
        log.info("Registered %d handler(s): %s", len(instances),
                 ", ".join(h.name for h in instances))

    def on_config_reload(self) -> None:
        """Refresh handler instances after config.yaml is re-read."""
        self._rebuild_registry()

    # ------------------------------------------------------------------ inbound

    async def on_inbound(self, msg: InboundMessage) -> None:
        settings = self.service.settings

        # -- direct messages can be switched off entirely
        if msg.kind == "dm" and not settings.dm.enabled:
            return

        # -- channel allowlist
        channel_cfg = None
        if msg.kind == "channel":
            name = msg.channel_name or f"#ch{msg.channel_idx}"
            channel_cfg = settings.channel_by_name(name)
            if channel_cfg is None:
                log.debug("Ignoring traffic on unconfigured channel %s", name)
                return
            msg.channel_name = name

        # -- group messages embed the sender's display name in the text
        #    ("Name: body"). Split it off so commands still match, and keep
        #    the original text intact for storage / the activity feed. The
        #    mesh.channel_sender_name policy decides how strictly to trust
        #    the embedded name (trust / smart / off). Done early because the
        #    blocked-node and unknown-sender guards both use sender identity.
        if msg.kind == "channel":
            msg.sender_name, body = resolve_channel_text(
                msg.text, settings.mesh.channel_sender_name, self.handlers)
        else:
            body = msg.text

        # -- dedupe (mesh retransmits can double-deliver)
        key = "|".join([
            msg.kind,
            msg.channel_name or "",
            msg.sender_prefix or "",
            msg.text,
            str(int(msg.sender_ts or msg.recv_ts)),
        ])
        if self._dedupe.seen_recently(key, time.time()):
            return
        self._dedupe.add(key, time.time())

        # -- persist + publish (listen-only channels still log here)
        self.service.store.add_message(MsgRecord(
            kind=msg.kind, direction="in", channel_name=msg.channel_name,
            sender_prefix=msg.sender_prefix, text=msg.text,
            sender_ts=msg.sender_ts, recv_ts=msg.recv_ts,
            hops=msg.hops, snr=msg.snr, sender_name=msg.sender_name,
        ))
        self.service.feed.publish("message_in", {
            "kind": msg.kind,
            "channel": msg.channel_name,
            "sender": msg.sender_prefix,
            "text": msg.text,
            "hops": msg.hops,
            "snr": msg.snr,
        })
        log.info("IN %s%s: %s%s", msg.kind,
                 f" {msg.channel_name}" if msg.channel_name else
                 (f" from {msg.sender_prefix}" if msg.sender_prefix else ""),
                 _log_line(msg.text), f" (hops={msg.hops})" if msg.hops is not None else "")

        # -- blocked node: operator marked this sender to be ignored entirely
        blocked_by = self._blocked_identity(msg, settings)
        if blocked_by is not None:
            log.info("Ignoring message from blocked node %s: %s",
                     blocked_by, _log_line(msg.text, 60))
            self.service.feed.publish("dropped", {
                "reason": "node blocked",
                "kind": msg.kind,
                "channel": msg.channel_name,
                "sender": blocked_by,
                "text": msg.text,
            })
            return

        # -- global mute (dashboard switch)
        if self.service.store.global_mute():
            return

        # -- channel reply enabled?
        if msg.kind == "channel" and channel_cfg is not None:
            override = self.service.store.channel_reply_override(channel_cfg.name)
            if not (override if override is not None else channel_cfg.reply):
                return  # listen-only channel

        # -- hop limit: never answer distant traffic
        if not self._hop_allowed(msg, settings):
            return

        # -- unknown sender: fail closed unless the operator opted in
        if not settings.bot.answer_unknown_senders and not self._sender_known(msg, settings):
            log.info("Ignoring message from unknown sender (%s): %s",
                     msg.kind, _log_line(msg.text, 60))
            self.service.feed.publish("dropped", {
                "reason": "unknown sender",
                "kind": msg.kind,
                "channel": msg.channel_name,
                "sender": msg.sender_prefix or msg.sender_name,
                "text": msg.text,
            })
            return

        # -- DM sender access
        is_admin = settings.is_admin_prefix(msg.sender_prefix) if msg.kind == "dm" else False

        tokens, prefixed = tokenize(body)
        if not tokens:
            return

        # '!pathx' is shorthand for '!path x' (extended version)
        tokens = expand_glued_x(tokens, prefixed, self.handlers)

        picked = select_handler(tokens, prefixed, self.handlers, msg.kind, is_admin)
        if picked is None:
            return
        handler, command_word = picked

        default_level = (settings.verbosity.dm_default if msg.kind == "dm"
                         else settings.verbosity.channel_default)
        verbosity = resolve_verbosity(tokens, settings.verbosity, default_level)
        args = handler_args(tokens, command_word, settings.verbosity)

        ctx = RouterCtx(service=self.service, msg=msg, tokens=tokens,
                        command=command_word, args=args, verbosity=verbosity,
                        is_admin=is_admin)

        # -- rate limiting (cheap, synchronous) ----------------------------
        # Checked here rather than inside the spawned task so a flood of
        # commands cannot even start handlers while the paces are exhausted.
        now = time.time()
        if now - self._last_reply_at < settings.limits.min_interval_seconds:
            log.debug("Rate limited: too soon since last reply.")
            return
        # -- per-channel reply cadence: each channel is paced on its own
        #    timer so a busy channel (or one noisy node spamming it) cannot
        #    keep resetting the global pace above and crowd out replies on
        #    other channels or in DMs. 0 / unlisted = no cadence (default).
        if msg.kind == "channel":
            lane = self._lane_of(msg)
            interval = self._channel_interval(lane, settings)
            if interval > 0 and \
                    now - self._last_channel_reply.get(lane, 0.0) < interval:
                log.debug("Channel %s reply cadence: too soon since the "
                          "last reply there (%.0fs interval).", lane, interval)
                return
        if msg.kind == "dm" and msg.sender_prefix and not is_admin:
            last = self._last_answer.get(msg.sender_prefix, 0.0)
            if now - last < settings.limits.per_sender_seconds:
                log.debug("Rate limited: recent reply to %s.", msg.sender_prefix)
                return
        # -- per-sender pace in channels: the same node asking again within
        #    the window waits (admins exempt; identity resolved best-effort
        #    from the embedded name, as the block list does).
        if msg.kind == "channel" and not is_admin:
            pace = settings.limits.per_sender_channel_seconds
            if pace > 0:
                identity = self._channel_sender_identity(msg)
                if identity and \
                        now - self._last_channel_answer.get(identity, 0.0) < pace:
                    log.debug("Channel per-sender pace: %s replied to %.0fs ago.",
                              identity, now - self._last_channel_answer[identity])
                    return
        # -- airtime budget: cheap non-recording pre-filter (the authoritative
        #    check+record happens under the send lock) so a doomed command
        #    never even spawns a handler task. Covers keyword replies AND
        #    pushes; admins (DM only) are exempt.
        if not self.service.budget_check(
                "reply",
                self._lane_of(msg) if msg.kind == "channel" else "dm",
                msg.sender_prefix or msg.sender_name or "?",
                msg.text,
                exempt=(msg.kind == "dm" and is_admin),
                record=False):
            return

        # -- run the handler off the dispatch chain -------------------------
        task = asyncio.get_running_loop().create_task(
            self._run_handler(handler, ctx, verbosity))
        self._handler_tasks.add(task)
        task.add_done_callback(self._handler_tasks.discard)

    async def _run_handler(self, handler, ctx, verbosity: str) -> None:
        """Execute one command with bounded concurrency; sends serialized."""
        try:
            async with self._handler_gate:
                try:
                    result = await handler.handle(ctx)
                except Exception as exc:
                    log.exception("Handler %s failed: %s", handler.name, exc)
                    self.service.feed.publish(
                        "notice", {"text": f"Handler {handler.name} error: {exc}"})
                    return
                if result is None:
                    return
                reply_text = self._render(handler, result, verbosity)
                # kind="dm_text" forces the reply out as a DM to the sender,
                # even when the command arrived on a channel (used by !dm).
                # The authoritative pace check happens HERE, under the lock,
                # immediately before sending: handlers now run off the
                # dispatch chain, so several may finish around the same time
                # - checking only at dispatch would let two rapid messages
                # both pass before either reply lands (double replies on
                # the mesh). The dispatch-time check above stays as a cheap
                # pre-filter so doomed commands never spawn a task.
                async with self._reply_lock:
                    send_settings = self.service.settings
                    send_now = time.time()
                    if send_now - self._last_reply_at < \
                            send_settings.limits.min_interval_seconds:
                        return
                    if ctx.msg.kind == "channel":
                        lane = self._lane_of(ctx.msg)
                        interval = self._channel_interval(lane, send_settings)
                        if interval > 0 and send_now - \
                                self._last_channel_reply.get(lane, 0.0) < interval:
                            return
                    if ctx.msg.kind == "dm" and ctx.msg.sender_prefix \
                            and not ctx.is_admin:
                        if send_now - self._last_answer.get(
                                ctx.msg.sender_prefix, 0.0) < \
                                send_settings.limits.per_sender_seconds:
                            return
                    if ctx.msg.kind == "channel" and not ctx.is_admin:
                        pace = send_settings.limits.per_sender_channel_seconds
                        if pace > 0:
                            identity = self._channel_sender_identity(ctx.msg)
                            if identity and send_now - \
                                    self._last_channel_answer.get(identity, 0.0) < pace:
                                return
                    # airtime budget, authoritative: check + record one slot
                    # per reply (a multi-chunk answer is one answer)
                    if not self.service.budget_check(
                            "reply",
                            self._lane_of(ctx.msg) if ctx.msg.kind == "channel"
                            else "dm",
                            ctx.msg.sender_prefix or ctx.msg.sender_name or "?",
                            reply_text,
                            exempt=(ctx.msg.kind == "dm" and ctx.is_admin),
                            record=True):
                        return
                    await self._send_reply(ctx, reply_text,
                                           force_dm=(result.kind == "dm_text"))
        except asyncio.CancelledError:
            raise

    # ------------------------------------------------------------------ guards

    def _channel_sender_identity(self, msg: InboundMessage) -> Optional[str]:
        """Best-effort stable identity of a channel sender, for per-sender
        pacing: the embedded name resolved to a known node's prefix (the
        same resolution and trust level the block list uses). Falls back
        to the bare embedded name when the node is unknown."""
        if not msg.sender_name:
            return None
        node = self.service.store.find_node(msg.sender_name)
        return node["prefix"] if node else f"name:{msg.sender_name}"

    def _blocked_identity(self, msg: InboundMessage, settings: Settings) -> Optional[str]:
        """Return the blocked identity if this message's sender is blocked.

        DMs identify the sender by public-key prefix (reliable). Channel
        messages carry only the unverified embedded name, so the name is
        resolved to a known node's prefix via the registry - best effort,
        exactly as names are treated elsewhere in the pipeline.
        """
        store = self.service.store
        if msg.kind == "dm":
            prefix = msg.sender_prefix
            return prefix if (prefix and store.is_blocked(prefix)) else None
        if not msg.sender_name:
            return None
        node = store.find_node(msg.sender_name)
        if node and store.is_blocked(node["prefix"]):
            return node["prefix"]
        return None

    @staticmethod
    def _sender_known(msg: InboundMessage, settings: Settings) -> bool:
        """Can the bot identify who sent this message?

        DM messages carry a public-key prefix from the protocol - that is
        the sender's identity. Channel messages carry only the (unverified)
        embedded display name, which counts as identity unless the operator
        declared names are not embedded at all (channel_sender_name: off),
        in which case name-less channel traffic is normal, not unknown.
        """
        if msg.kind == "dm":
            return bool(msg.sender_prefix)
        if settings.mesh.channel_sender_name == "off":
            return True
        return bool(msg.sender_name)

    def _hop_allowed(self, msg: InboundMessage, settings: Settings) -> bool:
        limit = settings.mesh.max_inbound_hops
        if limit <= 0:
            return True
        if msg.hops is None:
            if settings.mesh.unknown_hops == "respond":
                log.debug("Hop count unknown; policy=respond, continuing.")
                return True
            log.debug("Hop count unknown; policy=ignore, dropping.")
            return False
        if msg.hops > limit:
            log.info("Ignoring message %d hop(s) away (limit %d): %s",
                     msg.hops, limit, _log_line(msg.text, 60))
            self.service.feed.publish("dropped", {
                "reason": f"hops {msg.hops} > limit {limit}",
                "kind": msg.kind,
                "channel": msg.channel_name,
                "sender": msg.sender_prefix,
                "text": msg.text,
            })
            return False
        return True

    # ------------------------------------------------------------------ lanes

    @staticmethod
    def _lane_of(msg: InboundMessage) -> str:
        """Which reply lane a message belongs to (its channel name)."""
        if msg.channel_name:
            return msg.channel_name
        if msg.channel_idx is not None:
            return f"#ch{msg.channel_idx}"
        return "#?"

    @staticmethod
    def _channel_interval(lane: str, settings: Settings) -> float:
        """Reply cadence for one channel: per-channel override or the global
        default (0 = off)."""
        overrides = settings.limits.channel_intervals or {}
        if lane in overrides:
            return max(0.0, float(overrides[lane]))
        return max(0.0, float(settings.limits.channel_interval_seconds))

    # ------------------------------------------------------------------ outbound

    @staticmethod
    def _render(handler, result: HandlerResult, verbosity: str) -> str:
        if result.kind == "text":
            data = result.data
            return "\n".join(str(data).splitlines()) if isinstance(data, str) else str(data or "")
        lines = handler.render_lines(result, verbosity)
        return "\n".join(lines)

    async def _send_reply(self, ctx: RouterCtx, text: str,
                          force_dm: bool = False) -> None:
        settings = self.service.settings
        width = settings.limits.max_reply_length
        messages = chunk_text(text, width, settings.limits.max_chunks)
        if not messages:
            return
        client = self.service.client
        if client is None:
            log.warning("No radio client attached; reply dropped.")
            return
        sent = 0
        dm_target = None
        if force_dm:
            # The sender of a channel message is identified by the embedded
            # name - resolve it to a registry node prefix to address the DM.
            dm_target = ctx.msg.sender_prefix
            if not dm_target and ctx.msg.sender_name:
                node = self.service.store.find_node(ctx.msg.sender_name)
                dm_target = node["prefix"] if node else None
            if not dm_target:
                log.warning("!dm reply dropped: cannot resolve sender of %s.",
                            ctx.msg.kind)
                return
            for message in messages:
                if await client.send_dm(dm_target, message):
                    sent += 1
                    await asyncio.sleep(0.2)
        elif ctx.msg.kind == "channel":
            idx = ctx.msg.channel_idx
            for message in messages:
                if await client.send_channel(idx, message):
                    sent += 1
                    await asyncio.sleep(0.4)  # small gap between chunks on air
        else:
            for message in messages:
                if await client.send_dm(ctx.msg.sender_prefix, message):
                    sent += 1
                    await asyncio.sleep(0.2)
        if sent:
            now = time.time()
            self._last_reply_at = now
            if ctx.msg.kind == "channel":
                self._last_channel_reply[self._lane_of(ctx.msg)] = now
                identity = self._channel_sender_identity(ctx.msg)
                if identity:
                    self._last_channel_answer[identity] = now
            if ctx.msg.kind == "dm" and ctx.msg.sender_prefix:
                self._last_answer[ctx.msg.sender_prefix] = now
            if force_dm and dm_target:
                self._last_answer[dm_target] = now
            if force_dm:
                log.info("OUT DM->%s: %s", dm_target, _log_line(text))
            else:
                log.info("OUT %s: %s", ctx.sender_display(), _log_line(text))
