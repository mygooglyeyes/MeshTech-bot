"""Management dashboard server (FastAPI, in-process).

Endpoints:
    /api/login POST / GET        - password auth
    /api/status                  - bot + db summary
    /api/channels GET/POST       - effective channel states + toggles
    /api/mute POST               - global reply mute
    /api/nodes, /api/nodes/{id}  - node table + drill-down
    /api/messages, /api/config   - message browser + config view
    /api/packets/raw POST        - runtime raw-capture toggle
    /api/actions POST            - reload / shutdown
    /ws                          - live activity feed (WebSocket)
    /, /app.js, /style.css       - dashboard page
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import FileResponse, JSONResponse, Response

from .auth import Auth, LoginThrottle

log = logging.getLogger("meshtech-bot.web")

_STATIC_DIR = Path(__file__).parent / "static"

# All dashboard POST bodies are small JSON documents; refuse anything bigger
# so a LAN peer cannot exhaust memory with a giant request to /api/login.
_MAX_BODY_BYTES = 32 * 1024
_WS_AUTH_TIMEOUT = 15.0


# --------------------------------------------------------------------------
# App factory
# --------------------------------------------------------------------------

def build_app(service) -> FastAPI:
    auth = Auth(service.settings.web.password)
    store = service.store
    login_throttle = LoginThrottle()
    app = FastAPI(title="MeshTech-Bot", docs_url=None, redoc_url=None)

    # ------------------------------------------------------------- helpers

    @app.middleware("http")
    async def limit_body_size(request: Request, call_next):
        content_length = request.headers.get("content-length")
        if content_length is not None and content_length.isdigit() \
                and int(content_length) > _MAX_BODY_BYTES:
            return JSONResponse({"ok": False, "error": "Request too large"},
                                status_code=413)
        return await call_next(request)

    def require_auth(request: Request) -> None:
        if not auth.check(auth.bearer_token(request.headers.get("authorization", ""))):
            raise HTTPException(status_code=401, detail="Not authorized")

    def json_error(message: str, status: int = 400) -> JSONResponse:
        return JSONResponse({"ok": False, "error": message}, status_code=status)

    # ------------------------------------------------------------- login

    @app.get("/api/login")
    async def login_state():
        return {"auth_required": auth.required()}

    @app.post("/api/login")
    async def login(payload: Dict[str, Any], request: Request):
        # Brute-force protection: after a handful of failures from one IP,
        # refuse further attempts until the cooldown passes.
        ip = request.client.host if request.client else ""
        if login_throttle.locked_out(ip):
            retry = login_throttle.retry_after(ip)
            response = JSONResponse({"ok": False,
                                     "error": "Too many attempts - try again in a few minutes"},
                                    status_code=429)
            response.headers["Retry-After"] = str(max(1, int(retry)))
            return response
        token = auth.issue(str(payload.get("password", "")))
        if not auth.required():
            return {"token": ""}
        if not token:
            login_throttle.record_failure(ip)
            raise HTTPException(status_code=401, detail="Wrong password")
        login_throttle.record_success(ip)
        return {"token": token}

    # ------------------------------------------------------------- core

    @app.get("/api/status", dependencies=[Depends(require_auth)])
    async def status():
        return service.status_snapshot()

    @app.get("/api/channels", dependencies=[Depends(require_auth)])
    async def channels():
        return {"channels": service.effective_channel_states()}

    @app.post("/api/channels", dependencies=[Depends(require_auth)])
    async def set_channel(payload: Dict[str, Any]):
        name = str(payload.get("channel", ""))
        value = payload.get("reply")
        if name not in service.settings.configured_channel_names():
            return json_error(f"Unknown channel '{name}'")
        enabled = None if value is None else bool(value)
        return service.set_channel_reply(name, enabled)

    @app.post("/api/mute", dependencies=[Depends(require_auth)])
    async def set_mute(payload: Dict[str, Any]):
        muted = bool(payload.get("muted", False))
        store.set_global_mute(muted)
        service.feed.publish("notice", {"text": "Bot muted" if muted else "Bot unmuted"})
        return {"muted": muted}

    @app.get("/api/nodes", dependencies=[Depends(require_auth)])
    async def nodes(limit: int = 100):
        rows = store.list_nodes(limit=max(1, min(limit, 500)))
        blocked = store.blocked_prefixes()
        for row in rows:
            row["blocked"] = row["prefix"] in blocked
        return {"nodes": rows}

    @app.get("/api/nodes/{key}", dependencies=[Depends(require_auth)])
    async def node_detail(key: str):
        node = store.find_node(key)
        if node is None:
            return json_error("Node not found", 404)
        prefix = node["prefix"]
        return {
            "node": node,
            "blocked": store.is_blocked(prefix),
            "routes": store.route_history(prefix, limit=12),
            "link_history": store.link_history(prefix, limit=30),
            "stats": store.propagation_stats(prefix=prefix),
            "recent_messages": _messages_from_node(store, prefix),
        }

    @app.post("/api/nodes/{key}/block", dependencies=[Depends(require_auth)])
    async def block_node(key: str):
        node = store.find_node(key)
        if node is None:
            return json_error("Node not found", 404)
        prefix = node["prefix"]
        store.block_node(prefix)
        service.feed.publish("notice", {"text": f"Blocked node {prefix}"})
        return {"ok": True, "blocked": True, "prefix": prefix}

    @app.delete("/api/nodes/{key}/block", dependencies=[Depends(require_auth)])
    async def unblock_node(key: str):
        node = store.find_node(key)
        if node is None:
            return json_error("Node not found", 404)
        prefix = node["prefix"]
        store.unblock_node(prefix)
        service.feed.publish("notice", {"text": f"Unblocked node {prefix}"})
        return {"ok": True, "blocked": False, "prefix": prefix}

    @app.put("/api/nodes/{key}/note", dependencies=[Depends(require_auth)])
    async def set_node_note(key: str, payload: Dict[str, Any]):
        note = str(payload.get("note", "")).strip()[:200]
        if not store.set_node_note(key, note):
            return json_error("Node not found", 404)
        return {"ok": True, "prefix": key, "note": note}

    @app.get("/api/messages", dependencies=[Depends(require_auth)])
    async def messages(channel: Optional[str] = None, kind: Optional[str] = None,
                       limit: int = 100):
        rows = store.query_messages(channel=channel or None, kind=kind or None,
                                    limit=max(1, min(limit, 500)))
        return {"messages": rows}

    @app.get("/api/packets", dependencies=[Depends(require_auth)])
    async def packets(layer: Optional[str] = None, limit: int = 50):
        capture = service.capture
        if capture is None:
            return {"packets": [], "total": 0, "stats": {}, "raw_capture": False}
        layer = layer if layer in ("decoded", "raw") else None
        rows = capture.recent(layer=layer, limit=max(1, min(limit, 500)))
        return {"packets": rows, "total": capture.stats()["total"],
                "stats": capture.stats(),
                "raw_capture": bool(capture.enabled and capture.raw_enabled()),
                "raw_override": capture.raw_override()}

    @app.post("/api/packets/raw", dependencies=[Depends(require_auth)])
    async def packets_raw_toggle(payload: Dict[str, Any]):
        """Turn raw capture on/off in the running bot (not persisted)."""
        if service.capture is None:
            raise HTTPException(status_code=400,
                                detail="packet capture is disabled in config")
        service.capture.set_raw_enabled(bool(payload.get("enabled")))
        return {"raw_capture": service.capture.raw_enabled()}

    @app.get("/api/packets/export", dependencies=[Depends(require_auth)])
    async def packets_export(layer: Optional[str] = None):
        """Download captured packets as a CSV file (browser attachment)."""
        layer = layer if layer in ("decoded", "raw") else None
        try:
            from scripts.export_packets import packets_csv_text
            text, count = packets_csv_text(service.settings.storage.db_path,
                                           layer=layer, limit=100000)
        except Exception as exc:
            log.warning("packet CSV export failed: %s", exc)
            raise HTTPException(status_code=500, detail="CSV export failed")
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        filename = f"meshtech-packets-{layer or 'all'}-{stamp}.csv"
        return Response(
            content=text,
            media_type="text/csv",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "X-Rows": str(count),
            },
        )

    @app.get("/api/packets/profile", dependencies=[Depends(require_auth)])
    async def packets_profile():
        if service.capture is None:
            return {"profile": None}
        return {"profile": service.capture.profile()}

    @app.get("/api/packets/analysis", dependencies=[Depends(require_auth)])
    async def packets_analysis(hours: float = 24.0):
        hours = max(0.2, min(hours, 24 * 7))
        return {"analysis": store.packet_analysis(hours=hours)}

    @app.get("/api/config", dependencies=[Depends(require_auth)])
    async def config_view():
        return {"config": service.config_snapshot(),
                "warnings": service.settings.warnings}

    # ------------------------------------------------------------- modules

    @app.get("/api/modules", dependencies=[Depends(require_auth)])
    async def modules_list():
        """The module menu: declaration + current state per module."""
        from core.modules import module_menu
        settings_map = service.settings.modules.entries
        items = []
        for item in module_menu():
            name = item["name"]
            cfg = settings_map.get(name)
            values = dict(cfg.settings) if cfg else {}
            item["enabled"] = bool(cfg.enabled) if cfg else False
            item["values"] = values
            items.append(item)
        return {"modules": items}

    @app.post("/api/modules/{name}", dependencies=[Depends(require_auth)])
    async def module_save(name: str, payload: Dict[str, Any]):
        """Save one module's enabled flag + settings; hot-reloads."""
        from core.modules import module_menu
        from core.persist import save_module_settings
        from core.config import ConfigError
        known = [m for m in module_menu() if m["name"] == name]
        if not known:
            return json_error(f"Unknown module '{name}'", 404)
        if not known[0].get("available", True):
            return json_error(f"Module '{name}' is not available: "
                              f"{known[0].get('unavailable_reason', 'not implemented')}")
        enabled = bool(payload.get("enabled", False))
        values = payload.get("settings") or {}
        if not isinstance(values, dict):
            return json_error("settings must be an object")
        values = {str(k): v for k, v in values.items() if k != "enabled"}

        # validate against the module's declared fields before writing
        problems = []
        for field_def in known[0].get("fields", []):
            key = field_def.get("key")
            value = values.get(key)
            ftype = field_def.get("type", "text")
            if value in (None, ""):
                continue
            if ftype == "number":
                try:
                    values[key] = int(str(value))
                except (TypeError, ValueError):
                    problems.append(f"{field_def.get('label', key)} must be a whole number")
            elif ftype == "choice":
                choices = field_def.get("choices") or []
                if choices and str(value) not in choices:
                    problems.append(f"{field_def.get('label', key)} must be one of: "
                                    ", ".join(str(c) for c in choices))
        if problems:
            return json_error("; ".join(problems))

        try:
            fresh = save_module_settings(service.settings.config_path, name,
                                         enabled, values)
        except (ConfigError, OSError) as exc:
            return json_error(f"Config not saved: {exc}")
        summary = service.reload()
        service.feed.publish("notice", {"text": f"Module {name} "
                                                f"{'enabled' if enabled else 'disabled'}"})
        return {"ok": True, "message": summary, "module": {
            "name": name, "enabled": fresh.enabled, "values": fresh.settings}}

    # ------------------------------------------------------------- actions

    @app.post("/api/actions", dependencies=[Depends(require_auth)])
    async def actions(payload: Dict[str, Any]):
        action = str(payload.get("action", ""))
        if action == "reload":
            return {"ok": True, "message": service.reload()}
        if action == "shutdown":
            asyncio.get_event_loop().call_later(1.0, service.request_shutdown,
                                                "web dashboard")
            return {"ok": True, "message": "Shutting down"}
        return json_error(f"Unknown action '{action}'")

    # ------------------------------------------------------------- live feed

    @app.websocket("/ws")
    async def ws_feed(websocket: WebSocket):
        await websocket.accept()
        # The bearer token is sent as the first frame - never in the URL -
        # so it cannot leak into browser history, access logs or proxies.
        # The frame also carries the client's catch-up position (feed
        # generation + last sequence seen), so a reconnect is not answered
        # by re-pasting history the client already has on screen.
        authenticated = False
        last_seq = 0
        last_inst = None
        try:
            first = await asyncio.wait_for(websocket.receive_json(),
                                           timeout=_WS_AUTH_TIMEOUT)
            if isinstance(first, dict):
                token = str(first.get("token", ""))
                authenticated = auth.check(token)
                try:
                    last_seq = max(0, int(first.get("last_seq", 0) or 0))
                except (TypeError, ValueError):
                    last_seq = 0
                last_inst = first.get("inst")
        except Exception:
            authenticated = False
        if not authenticated:
            await websocket.close(code=4401)
            return
        feed = service.feed
        # Subscribe before reading history so an event published in between
        # is caught by the queue rather than missed; the rare replay/live
        # overlap is harmless - the page drops duplicate sequences.
        queue = await feed.subscribe()
        try:
            for event in feed.history_after(last_inst, last_seq, limit=30):
                await websocket.send_json(event)
            while True:
                event = await queue.get()
                await websocket.send_json(event)
        except Exception:
            pass
        finally:
            feed.unsubscribe(queue)

    # ------------------------------------------------------------- static page

    # The dashboard is edited in place; never let the browser serve stale
    # JS/CSS/HTML across reloads.
    _NO_CACHE = {"Cache-Control": "no-store"}

    @app.get("/")
    async def index():
        return FileResponse(_STATIC_DIR / "index.html", headers=_NO_CACHE)

    @app.get("/app.js")
    async def app_js():
        return FileResponse(_STATIC_DIR / "app.js", media_type="text/javascript",
                            headers=_NO_CACHE)

    @app.get("/style.css")
    async def style_css():
        return FileResponse(_STATIC_DIR / "style.css", media_type="text/css",
                            headers=_NO_CACHE)

    # ------------------------------------------------------------- legal files

    # The About dialog shows the project's license and third-party
    # attributions. Only these two hardcoded names are ever served (no
    # user input reaches the path), and each file is capped in size.
    _LEGAL_FILES = {
        "license": ("LICENSE", "text/plain; charset=utf-8"),
        "notices": ("THIRD_PARTY_NOTICES.md", "text/plain; charset=utf-8"),
    }
    _LEGAL_MAX_BYTES = 256 * 1024

    @app.get("/api/legal/{name}", dependencies=[Depends(require_auth)])
    async def legal(name: str):
        entry = _LEGAL_FILES.get(name)
        if entry is None:
            return json_error("Not found", 404)
        filename, media_type = entry
        path = _STATIC_DIR.parent.parent / filename
        try:
            data = path.read_bytes()
        except OSError:
            return json_error(f"{filename} not installed with the bot", 404)
        if len(data) > _LEGAL_MAX_BYTES:
            data = data[:_LEGAL_MAX_BYTES]
        return Response(content=data, media_type=media_type, headers=_NO_CACHE)

    return app


def _messages_from_node(store, prefix: str) -> list:
    """DM history for one node (sender or recipient)."""
    rows = store.query_messages(kind="dm", limit=200)
    return [r for r in rows if r.get("sender_prefix") == prefix][:15]


# --------------------------------------------------------------------------
# In-process uvicorn runner
# --------------------------------------------------------------------------

async def serve(service) -> None:
    """Run uvicorn on the same event loop until the bot stops."""
    import uvicorn

    web_cfg = service.settings.web
    app = build_app(service)
    config = uvicorn.Config(app, host=web_cfg.host, port=web_cfg.port,
                            log_level="warning")
    server = uvicorn.Server(config)
    log.info("Dashboard at http://%s:%d  (password: %s)",
             web_cfg.host, web_cfg.port, "set" if web_cfg.password else "none")
    await server.serve()
