"""Management dashboard server (FastAPI, in-process).

Endpoints:
    /api/login POST / GET        - password auth
    /api/status                  - bot + db summary
    /api/channels GET/POST       - effective channel states + toggles
    /api/mute POST               - global reply mute
    /api/nodes, /api/nodes/{id}  - node table + drill-down
    /api/messages, /api/config   - message browser + config view
    /api/actions POST            - reload / shutdown
    /ws                          - live activity feed (WebSocket)
    /, /app.js, /style.css       - dashboard page
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import FileResponse, JSONResponse

from .auth import Auth

log = logging.getLogger("meshtech-bot.web")

_STATIC_DIR = Path(__file__).parent / "static"


# --------------------------------------------------------------------------
# App factory
# --------------------------------------------------------------------------

def build_app(service) -> FastAPI:
    auth = Auth(service.settings.web.password)
    store = service.store
    app = FastAPI(title="MeshTech-Bot", docs_url=None, redoc_url=None)

    # ------------------------------------------------------------- helpers

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
    async def login(payload: Dict[str, Any]):
        token = auth.issue(str(payload.get("password", "")))
        if not auth.required():
            return {"token": ""}
        if not token:
            raise HTTPException(status_code=401, detail="Wrong password")
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
            return {"packets": [], "total": 0, "stats": {}}
        layer = layer if layer in ("decoded", "raw") else None
        rows = capture.recent(layer=layer, limit=max(1, min(limit, 500)))
        return {"packets": rows, "total": capture.stats()["total"],
                "stats": capture.stats()}

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
        token = websocket.query_params.get("token", "")
        if not auth.check(token):
            await websocket.close(code=4401)
            return
        await websocket.accept()
        queue = await service.feed.subscribe()
        try:
            for event in service.feed.history(limit=30):
                await websocket.send_json(event)
            while True:
                event = await queue.get()
                await websocket.send_json(event)
        except Exception:
            pass
        finally:
            service.feed.unsubscribe(queue)

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
