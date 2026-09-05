#!/usr/bin/env python3
"""MeshTech-Bot - entry point.

Usage:
    python bot.py --check            # validate config.yaml and exit
    python bot.py                    # run the bot
    python bot.py --config other.yaml
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys

from core.config import ConfigError, Settings, load

log = logging.getLogger("meshtech-bot")


def _setup_logging(settings: Settings) -> None:
    level = getattr(logging, settings.logging.level, logging.INFO)
    kwargs = {
        "level": level,
        "format": "%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    }
    if settings.logging.file:
        kwargs["filename"] = settings.logging.file
    logging.basicConfig(**kwargs)
    # The meshcore library is chatty; keep it quiet unless debugging.
    meshcore_log = logging.getLogger("meshcore")
    meshcore_log.setLevel(logging.DEBUG if settings.logging.level == "DEBUG"
                          else logging.WARNING)


def _check(settings: Settings) -> int:
    print(f"config.yaml OK: {settings.config_path}")
    for warning in settings.warnings:
        print(f"  warning: {warning}")
    conn = settings.connection
    print(f"  connection : {conn.host}:{conn.port}"
          f"{' (auto reconnect)' if conn.reconnect else ''}")
    print(f"  channels   : "
          + ", ".join(f"{c.name}{'' if c.reply else ' (listen)'}"
                      for c in settings.channels))
    print(f"  hop limit  : "
          + (str(settings.mesh.max_inbound_hops)
             if settings.mesh.max_inbound_hops else "unlimited")
          + f" (unknown hops: {settings.mesh.unknown_hops})")
    print(f"  dm         : {'enabled' if settings.dm.enabled else 'disabled'}"
          + (f", admin: {', '.join(settings.dm.admin_pubkey_prefixes) or 'NONE'}"
             if settings.dm.enabled else ""))
    print(f"  database   : {settings.storage.db_path}")
    print(f"  web        : "
          + (f"http://{settings.web.host}:{settings.web.port}"
             if settings.web.enabled else "disabled"))
    # Verify the database opens and migrations run.
    try:
        from core.store import Store
        store = Store(settings.storage.db_path)
        store.close()
        print("  database   : writable, schema OK")
    except Exception as exc:
        print(f"  ERROR opening database: {exc}")
        return 1
    if not settings.dm.admin_pubkey_prefixes:
        print("  NOTE: no admin nodes configured yet (dm.admin_pubkey_prefixes).")
    print("Ready. Start the bot with:  python bot.py")
    return 0


async def _run(settings: Settings) -> None:
    from core.feed import FeedHub
    from core.router import Router
    from core.service import BotService
    from core.store import Store

    store = Store(settings.storage.db_path)
    feed = FeedHub()
    service = BotService(settings, store, feed)

    from core.capture import PacketCapture
    service.capture = PacketCapture(store, lambda: service.settings)

    router = Router(service)
    service.router = router

    try:
        from core.client import RadioClient  # imports the meshcore library
    except ImportError as exc:
        raise RuntimeError(
            f"Radio library missing ({exc}). Install it with: "
            "pip install -r requirements.txt") from exc
    client = RadioClient(service)
    service.client = client
    client.set_inbound_handler(router.on_inbound)

    stop = asyncio.Event()
    service.set_stop_callback(stop.set)

    tasks = [
        asyncio.create_task(client.run(), name="radio"),
    ]
    if settings.web.enabled:
        try:
            from web.server import serve as web_serve
        except ImportError as exc:
            log.error("web.enabled is set but a dashboard dependency is missing (%s); "
                      "starting WITHOUT the dashboard. Install: pip install -r requirements.txt",
                      exc)
        else:
            tasks.append(asyncio.create_task(web_serve(service), name="web"))

    def _on_signal() -> None:
        log.info("Signal received - shutting down.")
        service.request_shutdown("signal")

    loop = asyncio.get_running_loop()
    for sig_name in ("SIGINT", "SIGTERM"):
        try:
            loop.add_signal_handler(getattr(signal, sig_name), _on_signal)
        except (NotImplementedError, AttributeError):
            pass  # e.g. Windows: KeyboardInterrupt path below handles Ctrl-C

    conn = settings.connection
    log.info("MeshTech-Bot starting: %s:%s, %d channel(s)",
             conn.host, conn.port, len(settings.channels))
    if settings.web.enabled:
        log.info("Dashboard: http://%s:%d", settings.web.host, settings.web.port)

    try:
        await stop.wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        service.request_shutdown("keyboard interrupt")
    finally:
        log.info("Stopping tasks...")
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        store.close()
        log.info("Bot stopped. 73!")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="MeshTech-Bot")
    parser.add_argument("--config", default="config.yaml",
                        help="path to config.yaml (default: config.yaml)")
    parser.add_argument("--check", action="store_true",
                        help="validate configuration and exit")
    args = parser.parse_args(argv)

    try:
        settings = load(args.config)
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    _setup_logging(settings)

    if args.check:
        return _check(settings)

    try:
        asyncio.run(_run(settings))
    except KeyboardInterrupt:
        pass
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
