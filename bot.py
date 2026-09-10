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
from pathlib import Path

from core.config import ConfigError, Settings, load

log = logging.getLogger("meshtech-bot")


def _build_label(version: Dict) -> str:
    """"v0.0.1 (main@abc1234)" - version first, commit in parens when known."""
    vn = version.get("version")
    commit = version.get("commit") or ""
    branch = version.get("branch") or ""
    sha = (branch + "@" + commit) if (branch and commit) else commit
    if vn and sha:
        return f"v{vn} ({sha})"
    if vn:
        return f"v{vn}"
    return sha or "unknown"


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
    from core.version import version_stamp
    stamp = _build_label(version_stamp())
    print(f"config.yaml OK: {settings.config_path}  (build: {stamp})")
    for warning in settings.warnings:
        print(f"  warning: {warning}")
    conn = settings.connection
    if conn is not None:
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
    # One-time statistics backfills (each guarded by a meta key inside the
    # store, so they run exactly once per database ever).
    try:
        store.backfill_node_routes()
        store.backfill_node_traffic(radio=settings.radio)
    except Exception as exc:
        log.warning("statistics backfill failed (non-fatal): %s", exc)
    feed = FeedHub()
    service = BotService(settings, store, feed)

    from core.capture import PacketCapture
    service.capture = PacketCapture(store, lambda: service.settings)

    router = Router(service)
    service.router = router

    # The task list must exist BEFORE the radio starts: _start_mcp and
    # _start_companion append their tasks into it. (v0.0.092 bench-test
    # fix: the list was created further down, so the first start with
    # mcp enabled crashed with UnboundLocalError - and the service
    # crash-looped. Found by the hilltop bench test 2026-09-09.)
    tasks = []

    # MCP mode: the bot OWNS the SPI radio (PiMesh-1W v2) instead of
    # talking to an openHop companion. The modem feed shares every packet
    # with meshtech-modem so openHop's log stays complete.
    if settings.mcp.enabled:
        await _start_mcp(service, settings, tasks)
    else:
        await _start_companion(service, settings, tasks)

    stop = asyncio.Event()
    service.set_stop_callback(stop.set)

    # Read-only software-update checker: feeds the dashboard's version
    # chip and update popup.  It only reads - never downloads or restarts.
    from core.updatecheck import UpdateChecker
    service.update_checker = UpdateChecker(lambda: service.settings)

    # Web-console updater (opt-in via updates.clone_path): validates the
    # request and launches the ONE sudo-whitelisted trigger script.  The
    # real work runs detached, so it survives the restart it causes.
    from core.selfupdate import SelfUpdater
    service.self_updater = SelfUpdater(
        Path(settings.storage.db_path).parent,
        Path(__file__).resolve().parent / "scripts" / "update-trigger.sh",
    )

    # (tasks already holds the radio/feed tasks - append, never rebind)
    tasks.append(service.update_checker.start())
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

    from core.version import version_stamp
    stamp = _build_label(version_stamp())
    conn = settings.connection
    if settings.mcp.enabled:
        log.info("MeshTech-Bot %s starting (MCP radio mode: owns the SPI radio)",
                 stamp)
    else:
        log.info("MeshTech-Bot %s starting: %s:%s, %d channel(s)",
                 stamp, conn.host if conn else "?", conn.port if conn else 0,
                 len(settings.channels))
    if settings.web.enabled:
        log.info("Dashboard: http://%s:%d", settings.web.host, settings.web.port)
    if settings.updates.check_enabled:
        log.info("Update check: every %g h", settings.updates.check_hours)
    else:
        log.info("Update check: disabled")

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


async def _start_companion(service, settings, tasks) -> None:
    """Legacy radio path: talk to an openHop companion over TCP."""
    try:
        from core.client import RadioClient  # imports the meshcore library
    except ImportError as exc:
        raise RuntimeError(
            f"Radio library missing ({exc}). Install it with: "
            "pip install -r requirements.txt") from exc
    client = RadioClient(service)
    service.client = client
    client.set_inbound_handler(service.router.on_inbound)
    tasks.append(asyncio.create_task(client.run(), name="radio"))


async def _start_mcp(service, settings, tasks) -> None:
    """MCP radio path: the bot owns the SPI radio + feeds the modem."""
    from core.mcp import Mcp
    from core.modemfeed import ModemFeed

    push_queue: asyncio.Queue = asyncio.Queue(
        maxsize=settings.modem_feed.queue_size)
    mcp = Mcp(service, service.router.on_inbound, push_queue)
    service.mcp = mcp
    # The router replies through ONE client interface. In MCP mode that
    # client is the radio itself: the adapter below turns channel replies
    # and DMs into real encrypted packets on the SPI radio.
    service.client = mcp
    tasks.append(asyncio.create_task(mcp.start(), name="mcp-radio"))

    if settings.modem_feed.enabled:
        feed = ModemFeed(service, push_queue)
        service.modem_feed = feed
        tasks.append(asyncio.create_task(feed.run(), name="modem-feed"))
    else:
        log.info("Modem feed disabled - packets go to the bot only "
                 "(openHop will show nothing).")


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
        # uvloop (Linux/macOS): a faster event-loop implementation that
        # uvicorn and the mesh client both inherit. Optional - the bot runs
        # fine on the standard loop if it is missing.
        try:
            import uvloop
            uvloop.install()
            log.info("Event loop: uvloop")
        except ImportError:
            pass
        asyncio.run(_run(settings))
    except KeyboardInterrupt:
        pass
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
