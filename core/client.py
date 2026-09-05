"""Radio client - the bot's connection to the mesh.

Connects to the openHop Repeater's companion endpoint over TCP using the
official ``meshcore`` library, supervises the connection (auto reconnect
with backoff), syncs the channel table and the contact/node database, and
hands every incoming message to the router.

The bot never holds keys: the mesh identity lives in the openHop companion
that this client talks to.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from typing import Awaitable, Callable, Dict, Optional

from meshcore import EventType, MeshCore

from .feed import FeedHub
from .models import InboundMessage, MsgRecord
from .config import ConnCfg, Settings
from .service import BotService

# Event types that are synthetic (not companion frames) and therefore not
# captured as packets. CONTACTS is the aggregate end-of-sync bulk event; the
# per-contact CONTACT/NEXT_CONTACT frames are captured individually.
_CAPTURE_SKIP = {"CONTACTS", "CONNECTED", "DISCONNECTED"}

log = logging.getLogger("meshtech-bot.client")

MAX_CHANNEL_SLOTS = 8


class RadioClient:
    def __init__(self, service: BotService):
        self.service = service
        self.settings: Settings = service.settings
        self.store = service.store
        self.feed: FeedHub = service.feed
        self.mc: Optional[MeshCore] = None
        self.is_connected = False
        # The companion's own node name (read from the SELF_INFO the openHop
        # companion sends at connect) - how the bot presents itself in replies.
        self.own_name: str = ""
        self._slot_info: Dict[int, dict] = {}
        self._on_inbound: Optional[Callable[[InboundMessage], Awaitable[None]]] = None
        self._contact_sync_again_at = 0.0
        self._last_advert_sync = 0.0

    # ------------------------------------------------------------------ wiring

    def set_inbound_handler(self, handler: Callable[[InboundMessage], Awaitable[None]]) -> None:
        self._on_inbound = handler

    # ------------------------------------------------------------------ main loop

    async def run(self) -> None:
        """Supervision loop: connect, serve, reconnect with backoff."""
        delay = self.settings.connection.reconnect_min_seconds
        while not self.service.stop_requested:
            try:
                await self._connect_and_serve()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("Connection problem: %s", exc)
            finally:
                await self._teardown()
            if self.service.stop_requested:
                break
            if not self.settings.connection.reconnect:
                log.info("Reconnect disabled; giving up.")
                break
            log.info("Reconnecting in %.0fs ...", delay)
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                raise
            delay = min(delay * 2, self.settings.connection.reconnect_max_seconds)
        log.info("Radio client stopped.")

    async def _connect_and_serve(self) -> None:
        cfg: ConnCfg = self.settings.connection
        log.info("Connecting to %s:%s ...", cfg.host, cfg.port)
        mc = await MeshCore.create_tcp(cfg.host, cfg.port, auto_reconnect=False)
        self.mc = mc
        self.is_connected = True
        log.info("Connected to %s:%s", cfg.host, cfg.port)

        # openHop names its companion (node_name) - use that as our own name.
        self.own_name = str((mc.self_info() or {}).get("name") or "").strip(" \x00")
        if self.own_name:
            log.info("Companion node name: %s", self.own_name)

        await self._try("set device time",
                         lambda: mc.commands.set_time(int(time.time())))

        await self._read_channel_slots()
        await self._apply_configured_channels()
        await self._subscribe_events()
        if self.service.capture is not None and self.service.capture.enabled:
            self._wrap_reader_for_raw_capture(mc)
        await self._sync_contacts(force=True)
        await self._advertise_on_start()
        if hasattr(mc, "start_auto_message_fetching"):
            await self._try("start auto message fetching",
                             lambda: mc.start_auto_message_fetching())
        else:
            log.warning("meshcore library has no start_auto_message_fetching - "
                        "incoming messages may not be delivered to the bot.")

        self.feed.publish("connected", {
            "host": cfg.host, "port": cfg.port,
            "channels": self.channel_names(),
        })
        log.info("Startup complete: %d channel(s), advert %s",
                 len(self.channel_names()),
                 "sent" if self.settings.bot.advertise_on_start else "skipped")

        # Serve until the connection drops or a stop is requested.
        while not self.service.stop_requested:
            healthy = bool(self.mc) and bool(getattr(self.mc, "is_connected", False))
            if not healthy:
                log.info("Connection to radio lost.")
                break
            if time.time() >= self._contact_sync_again_at:
                await self._sync_contacts(force=False)
                self._contact_sync_again_at = (
                    time.time() + self.settings.storage.contact_refresh_minutes * 60
                )
            await asyncio.sleep(1.0)

    async def _teardown(self) -> None:
        was_connected = self.is_connected
        self.is_connected = False
        mc, self.mc = self.mc, None
        if mc is not None:
            try:
                await mc.stop_auto_message_fetching()
            except Exception:
                pass
            try:
                await mc.disconnect()
            except Exception:
                pass
        if was_connected:
            self.feed.publish("disconnected", {"at": time.time()})

    # ------------------------------------------------------------------ helpers

    @staticmethod
    async def _try(what: str, command_fn) -> bool:
        """Run a meshcore command, logging failures instead of crashing."""
        try:
            result = await command_fn()
            if result is not None and getattr(result, "type", None) == EventType.ERROR:
                log.warning("%s failed: %s", what, getattr(result, "payload", "error"))
                return False
            return True
        except Exception as exc:
            log.warning("%s raised: %s", what, exc)
            return False

    # ------------------------------------------------------------------ channels

    async def _read_channel_slots(self) -> None:
        """Fetch the companion's channel table (indices 0..7)."""
        info: Dict[int, dict] = {}
        for idx in range(MAX_CHANNEL_SLOTS):
            try:
                result = await self.mc.commands.get_channel(idx)
            except Exception:
                continue
            if result is None or getattr(result, "type", None) == EventType.ERROR:
                continue
            payload = result.payload or {}
            name = payload.get("name")
            if isinstance(name, bytes):
                name = name.decode("utf-8", "replace").rstrip("\x00")
            if not isinstance(name, str):
                name = ""
            secret = payload.get("secret")
            info[idx] = {
                "name": name.strip(),
                "has_secret": bool(secret) if secret is not None else False,
            }
        self._slot_info = info
        known = {i: s["name"] for i, s in info.items() if s["name"]}
        log.info("Companion channel table: %s", known or "empty")

    def channel_names(self) -> Dict[int, str]:
        return {idx: info["name"] for idx, info in self._slot_info.items() if info.get("name")}

    def name_for_idx(self, idx: Optional[int]) -> Optional[str]:
        if idx is None:
            return None
        info = self._slot_info.get(idx)
        if info and info.get("name"):
            return info["name"]
        return None

    @staticmethod
    def _name_key(name: str) -> str:
        return name.lstrip("#").strip().casefold()

    async def _apply_configured_channels(self) -> None:
        """Make sure configured channels exist in a companion slot.

        Hashtag channels (#name) use the well-known derived key
        sha256("#name")[:16]; private channels can set secret_hex in config.
        Failures are warnings only - the companion may be configured
        server-side already, in which case we simply use the existing slot.
        """
        for cfg_channel in self.settings.channels:
            key = self._name_key(cfg_channel.name)
            slot = next((idx for idx, info in self._slot_info.items()
                         if self._name_key(info.get("name", "")) == key), None)
            if slot is not None:
                log.debug("Channel %s already in slot %d", cfg_channel.name, slot)
                continue
            free = next((idx for idx in range(1, MAX_CHANNEL_SLOTS)
                         if not self._slot_info.get(idx, {}).get("name")), None)
            if free is None:
                log.warning("No free channel slot on companion for %s - "
                            "configure it on the openHop side.", cfg_channel.name)
                continue
            if cfg_channel.secret_hex:
                try:
                    secret = bytes.fromhex(cfg_channel.secret_hex)
                except ValueError:
                    log.warning("Channel %s secret_hex is not valid hex; ignored.", cfg_channel.name)
                    secret = hashlib.sha256(cfg_channel.name.encode("utf-8")).digest()[:16]
            else:
                secret = hashlib.sha256(cfg_channel.name.encode("utf-8")).digest()[:16]
            ok = await self._try(
                f"set channel {cfg_channel.name} in slot {free}",
                lambda: self.mc.commands.set_channel(free, cfg_channel.name, secret),
            )
            if ok:
                self._slot_info[free] = {"name": cfg_channel.name, "has_secret": True}
                log.info("Channel %s added to companion slot %d", cfg_channel.name, free)

    # ------------------------------------------------------------------ contacts

    async def _sync_contacts(self, force: bool = False) -> None:
        if self.mc is None:
            return
        # Debounce advert-triggered refreshes
        if not force and time.time() < self._last_advert_sync + 30:
            return
        try:
            result = await self.mc.commands.get_contacts()
        except Exception as exc:
            log.debug("get_contacts failed: %s", exc)
            return
        if result is None or getattr(result, "type", None) == EventType.ERROR:
            return
        payload = result.payload or {}
        if not isinstance(payload, dict):
            return
        count = 0
        for key, contact in payload.items():
            if not isinstance(contact, dict) or not isinstance(key, str):
                continue
            name = contact.get("adv_name") or contact.get("name") or ""
            self.store.upsert_node(
                pubkey=key,
                name=str(name) if name else None,
                snr=_num(contact.get("adv_snr")),
                lat=_num(contact.get("adv_lat")),
                lon=_num(contact.get("adv_lon")),
                source="contact",
            )
            # Record route snapshots when the contact carries path info
            path = contact.get("path")
            contact_snr = _num(contact.get("adv_snr", contact.get("snr",
                                                                  contact.get("last_snr"))))
            if path is not None:
                try:
                    hops = len(path) if isinstance(path, (list, bytes)) else None
                except TypeError:
                    hops = None
                summary = (path.hex() if isinstance(path, bytes) and path
                           else (",".join(str(p) for p in path) if isinstance(path, list) else None))
                if hops is not None or summary:
                    self.store.add_route(key[:12], hops, summary, snr=contact_snr)
            count += 1
        self._last_advert_sync = time.time()
        if count:
            log.debug("Contact sync: %d node(s) recorded", count)

    async def _advertise_on_start(self) -> None:
        if not self.settings.bot.advertise_on_start:
            return
        await asyncio.sleep(1.0)  # let the link settle first
        await self._try("send flood advert",
                         lambda: self.mc.commands.send_advert(flood=True))

    # ------------------------------------------------------------------ events

    async def _subscribe_events(self) -> None:
        mc = self.mc
        self._subs = [
            mc.subscribe(EventType.CONTACT_MSG_RECV, self._on_dm),
            mc.subscribe(EventType.CHANNEL_MSG_RECV, self._on_channel),
            mc.subscribe(EventType.CONNECTED, self._on_connected),
            mc.subscribe(EventType.DISCONNECTED, self._on_disconnected),
        ]
        # Advert discovery refreshes the node database (debounced inside _sync_contacts)
        for event_type in (EventType.ADVERTISEMENT, EventType.NEW_CONTACT):
            try:
                self._subs.append(mc.subscribe(event_type, self._on_node_event))
            except Exception:
                pass
        # Packet capture: subscribe to EVERY dispatched frame type. The
        # capture hook is a no-op fast path when capture is disabled.
        if self.service.capture is not None and self.service.capture.enabled:
            for event_type in EventType:
                if event_type.name in _CAPTURE_SKIP:
                    continue
                try:
                    self._subs.append(mc.subscribe(event_type, self._on_packet_event))
                except Exception:
                    pass

    async def _on_connected(self, event) -> None:
        self.is_connected = True
        log.info("Connection event: %s", event.payload)

    async def _on_disconnected(self, event) -> None:
        log.info("Disconnected event: %s", event.payload)
        self.is_connected = False

    async def _on_node_event(self, event) -> None:
        # Refresh contact list shortly after an advert/new contact arrives.
        if time.time() >= self._last_advert_sync + 30:
            self._contact_sync_again_at = min(self._contact_sync_again_at,
                                              time.time() + 5)

    async def _on_channel(self, event) -> None:
        await self._dispatch("channel", event)

    async def _on_dm(self, event) -> None:
        await self._dispatch("dm", event)

    # ---------------------------------------------------------- packet capture

    async def _on_packet_event(self, event) -> None:
        capture = self.service.capture
        if capture is None:
            return
        payload = event.payload if hasattr(event, "payload") else None
        payload = payload if isinstance(payload, dict) else {}
        channel_name = None
        idx = _idx_of(payload)
        if idx is not None:
            channel_name = self.name_for_idx(idx)
        capture.record_event(
            ts=time.time(),
            event_type=getattr(event, "type", None),
            payload=payload,
            attributes=getattr(event, "attributes", None) or {},
            channel_name=channel_name,
        )

    def _wrap_reader_for_raw_capture(self, mc) -> None:
        """Wrap the library's reader entry so every wire frame is captured."""
        reader = getattr(mc, "_reader", None)
        original = getattr(reader, "handle_rx", None)
        if original is None:
            log.debug("meshcore reader not found - raw capture unavailable")
            return

        async def wrapped(data):
            try:
                self.service.capture.record_raw(time.time(), data)
            except Exception:
                pass  # capture must never break the receive path
            return await original(data)

        reader.handle_rx = wrapped

    async def _dispatch(self, kind: str, event) -> None:
        payload = event.payload if hasattr(event, "payload") else {}
        if not isinstance(payload, dict):
            return
        msg = InboundMessage(
            kind=kind,
            text=_text_of(payload),
            sender_prefix=_prefix_of(payload) if kind == "dm" else None,
            channel_idx=_idx_of(payload) if kind == "channel" else None,
            sender_ts=_ts_of(payload),
            recv_ts=time.time(),
            hops=_hops_of(payload),
            snr=_snr_of(payload),
        )
        msg.channel_name = self.name_for_idx(msg.channel_idx)
        if self._on_inbound is not None:
            try:
                await self._on_inbound(msg)
            except Exception as exc:
                log.exception("Inbound dispatch error: %s", exc)

    # ------------------------------------------------------------------ sending

    async def send_channel(self, idx: int, text: str) -> bool:
        if not self.is_connected or self.mc is None:
            log.warning("Not connected - dropping channel reply.")
            return False
        ok = await self._try(f"send channel message (slot {idx})",
                             lambda: self.mc.commands.send_chan_msg(idx, text))
        if ok:
            self.store.add_message(MsgRecord(kind="channel", direction="out",
                                             channel_name=self.name_for_idx(idx),
                                             text=text, recv_ts=time.time()))
            self.feed.publish("message_out", {"kind": "channel",
                                              "channel": self.name_for_idx(idx),
                                              "text": text})
        return ok

    async def send_dm(self, sender_prefix: str, text: str) -> bool:
        """Reply to a direct message. Needs a stored contact/full key."""
        if not self.is_connected or self.mc is None:
            log.warning("Not connected - dropping DM reply.")
            return False
        contact = self._find_contact(sender_prefix)
        if contact is None:
            log.warning("Cannot reply by DM to %s: no contact/full key stored.", sender_prefix)
            return False
        ok = await self._try(f"send DM to {sender_prefix}",
                             lambda: self.mc.commands.send_msg(contact, text))
        if ok:
            self.store.add_message(MsgRecord(kind="dm", direction="out",
                                             sender_prefix=sender_prefix,
                                             text=text, recv_ts=time.time()))
            self.feed.publish("message_out", {"kind": "dm",
                                              "sender": sender_prefix,
                                              "text": text})
        return ok

    def _find_contact(self, prefix: str):
        """Return a meshcore contact dict for a 12-hex prefix, if known."""
        prefix = prefix.lower()
        try:
            contact = self.mc.get_contact_by_key_prefix(prefix)
            if contact is not None:
                return contact
        except Exception:
            pass
        # Fall back to a full public key stored in our database
        node = self.store.get_node(prefix)
        if node and node.get("pubkey"):
            pubkey = node["pubkey"]
            try:
                contact = self.mc.get_contact_by_key_prefix(pubkey)
                if contact is not None:
                    return contact
            except Exception:
                pass
            return {"public_key": pubkey}  # meshcore accepts a key dict
        return None

    async def stop(self) -> None:
        await self._teardown()


# --------------------------------------------------------------------------
# Defensive payload readers (the meshcore library is still evolving; missing
# keys must never crash the client, they simply become "unknown").
# --------------------------------------------------------------------------

def _text_of(payload: dict) -> str:
    for key in ("text", "message"):
        value = payload.get(key)
        if isinstance(value, str):
            return value
    return ""


def _idx_of(payload: dict) -> Optional[int]:
    value = payload.get("channel_idx", payload.get("channel_index"))
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _prefix_of(payload: dict) -> Optional[str]:
    value = payload.get("pubkey_prefix", payload.get("sender_prefix"))
    if isinstance(value, str) and value:
        return value.lower()
    value = payload.get("public_key")
    if isinstance(value, str) and len(value) >= 12:
        return value[:12].lower()
    return None


def _ts_of(payload: dict) -> Optional[float]:
    value = payload.get("timestamp", payload.get("sender_ts"))
    try:
        number = float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
    if number is None or number <= 0:
        return None
    return number


def _snr_of(payload: dict) -> Optional[float]:
    try:
        return float(payload["snr"]) if payload.get("snr") is not None else None
    except (TypeError, ValueError):
        return None


def _hops_of(payload: dict) -> Optional[int]:
    for key in ("hops", "path_len", "path_length", "num_hops", "hop_count"):
        value = payload.get(key)
        try:
            number = int(value) if value is not None else None
        except (TypeError, ValueError):
            number = None
        if number is not None:
            break
    if number is None:
        return None
    if number in (0xFF, 255):
        return 0  # direct reception - no repeaters involved
    if 0 <= number <= 64:
        return number
    return None


def _num(value) -> Optional[float]:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
