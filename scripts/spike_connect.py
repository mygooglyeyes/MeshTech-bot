#!/usr/bin/env python3
"""M0 validation spike - prove the bot's assumptions against your radio.

Run this once against your openHop Repeater to answer the questions that
shaped the bot's design:

  1. Does the companion endpoint accept a standard TCP client?
  2. What channels exist on the companion channel table?
  3. Do incoming messages carry hop/path metadata we can use?
  4. Does the companion allow the client to add a channel?

Usage:
    python scripts/spike_connect.py --host 192.168.1.50 --port 5000
    python scripts/spike_connect.py --config config.yaml   # use bot config
    python scripts/spike_connect.py ... --seconds 30       # listen longer

Then send a channel message to the bot's channels from another radio and
watch the payloads print below.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import time


async def main() -> int:
    parser = argparse.ArgumentParser(description="openHop/MeshCore link probe")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--config", default=None, help="load host/port from bot config.yaml")
    parser.add_argument("--seconds", type=int, default=20, help="listen time (default 20)")
    parser.add_argument("--add-channel", default=None,
                        help="e.g. '#spike' - try adding a hashtag channel")
    args = parser.parse_args()

    host = args.host
    if args.config:
        try:
            from core.config import load
            settings = load(args.config)
            host = settings.connection.host
            args.port = settings.connection.port
        except Exception as exc:
            print(f"Could not read {args.config}: {exc}")

    if not host:
        print("Need --host <ip> (or --config config.yaml).")
        return 1

    try:
        from meshcore import EventType, MeshCore
    except ImportError:
        print("The meshcore library is not installed.\n"
              "Install with:  pip install -r requirements.txt")
        return 1

    print(f"== Probe {host}:{args.port} ==")
    try:
        mc = await MeshCore.create_tcp(host, args.port, auto_reconnect=False)
    except Exception as exc:
        print(f"CONNECT FAILED: {exc}")
        print("Check the host IP and that the openHop companion identity "
              "(mesh.companions) is configured with this tcp_port.")
        return 1
    print("Connected OK.")

    # --- device identity
    try:
        result = await mc.commands.send_device_query()
        if getattr(result, "type", None) != EventType.ERROR:
            info = getattr(result, "payload", {}) or {}
            print(f"Device: model={info.get('model')!r} version={info.get('ver')!r} "
                  f"max_channels={info.get('max_channels')}")
    except Exception as exc:
        print(f"(device query skipped: {exc})")

    # --- channel table
    print("\nChannel table:")
    for idx in range(8):
        try:
            result = await mc.commands.get_channel(idx)
        except Exception:
            continue
        if result is None or getattr(result, "type", None) == EventType.ERROR:
            continue
        payload = result.payload or {}
        name = payload.get("name", b"")
        if isinstance(name, bytes):
            name = name.decode("utf-8", "replace").rstrip("\x00")
        secret = payload.get("secret")
        print(f"  slot {idx}: name={name!r} has_key={bool(secret)}")

    # --- optional: try adding a hashtag channel (probe for client-side writes)
    if args.add_channel:
        import hashlib
        name = args.add_channel if args.add_channel.startswith("#") else "#" + args.add_channel
        free = None
        try:
            for idx in range(1, 8):
                result = await mc.commands.get_channel(idx)
                payload = (result or {}).payload if hasattr(result, "payload") else {}
                channel_name = payload.get("name", b"")
                if isinstance(channel_name, bytes):
                    channel_name = channel_name.decode("utf-8", "replace").rstrip("\x00")
                if not channel_name:
                    free = idx
                    break
        except Exception:
            pass
        if free is None:
            print(f"\nCannot add {name}: no free channel slot 1-7.")
        else:
            secret = hashlib.sha256(name.encode("utf-8")).digest()[:16]
            try:
                result = await mc.commands.set_channel(free, name, secret)
                ok = getattr(result, "type", None) != EventType.ERROR
                print(f"\nset_channel(slot {free}, {name!r}) -> "
                      + ("OK (client can manage channels)" if ok else
                         f"rejected ({getattr(result, 'payload', 'error')}) - "
                         "channels must be configured on the openHop side"))
            except Exception as exc:
                print(f"\nset_channel raised: {exc} (client writes not supported?)")

    # --- contacts
    try:
        result = await mc.commands.get_contacts()
        if getattr(result, "type", None) != EventType.ERROR:
            contacts = getattr(result, "payload", {}) or {}
            print(f"\nContacts in companion: {len(contacts)}")
            for key, contact in list(contacts.items())[:5]:
                print(f"  {key[:12]} name={contact.get('adv_name')!r}")
        else:
            print("\nget_contacts failed:", getattr(result, "payload", "?"))
    except Exception as exc:
        print(f"\nget_contacts raised: {exc}")

    # --- listen for incoming messages (channel + DM payloads incl. hops)
    print(f"\nListening {args.seconds}s for traffic - send a channel message "
          "from another radio now (watch the raw payload fields).")

    async def on_channel(event):
        payload = getattr(event, "payload", {})
        print("\n[channel msg payload]")
        _dump(payload)

    async def on_dm(event):
        payload = getattr(event, "payload", {})
        print("\n[dm msg payload]")
        _dump(payload)

    subs = [
        mc.subscribe(EventType.CHANNEL_MSG_RECV, on_channel),
        mc.subscribe(EventType.CONTACT_MSG_RECV, on_dm),
    ]
    if hasattr(mc, "start_auto_message_fetching"):
        await mc.start_auto_message_fetching()

    await asyncio.sleep(args.seconds)
    print("\nDone. If channel payloads above contain 'hops' or 'path_len', "
          "hop-based filtering will work. If they are missing, the bot falls "
          "back to its unknown_hops policy (configurable in config.yaml).")
    try:
        await mc.stop_auto_message_fetching()
    except Exception:
        pass
    try:
        await mc.disconnect()
    except Exception:
        pass
    return 0


def _dump(payload) -> None:
    if not isinstance(payload, dict):
        print("  (not a dict:", payload, ")")
        return
    for key in ("text", "message", "channel_idx", "channel_index",
                "pubkey_prefix", "timestamp", "snr", "hops", "path_len",
                "path_length", "txt_type", "route"):
        if key in payload:
            print(f"  {key}: {payload[key]!r}")
    print("  (all keys:", sorted(payload.keys()), ")")


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
