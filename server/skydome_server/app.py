"""The SkyDome web server on port 80 (handoff §5.7–5.9, DECISIONS.md D-021):

    /               the web page (web/dist, built from web/)
    /api/...        status, camera settings, Detail/Smooth, snapshot
    /api/webrtc/... mediamtx's WHEP, so the page plays the dome from its own
                    address (the video itself still flows straight from mediamtx)
    /onvif/...      ONVIF for NVRs (D-020)

LAN only, no login for the page or the API (handoff §5.7: "Basic auth
optional; off by default"); ONVIF keeps its own login.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from . import live
from .camera_settings import CameraSettings
from .onvif.http import OnvifEndpoint, SnapshotSource
from .system import System

STREAM_PATHS = {"detail": "dome", "smooth": "dome-smooth"}   # mediamtx paths (config/mediamtx.yml)


@dataclass
class Live:
    """Where the server finds the live dome's mode file, stats and stream."""
    mode_file: Path
    stats_files: list[Path]
    mediamtx_api: str = "http://127.0.0.1:9997"
    webrtc_port: int = 8889
    webrtc: str = "http://127.0.0.1:8889"
    paths: Callable[[], dict | None] | None = field(default=None, repr=False)   # tests replace mediamtx

    def stream_paths(self) -> dict | None:
        """mediamtx's view: {path: {"ready": bool, "readers": n}}, or None if it isn't answering."""
        if self.paths:
            return self.paths()
        try:
            with urllib.request.urlopen(f"{self.mediamtx_api}/v3/paths/list", timeout=1) as r:
                items = json.load(r).get("items") or []
        except (OSError, ValueError):
            return None
        return {i["name"]: {"ready": bool(i.get("ready")), "readers": len(i.get("readers") or [])} for i in items}


def create_app(settings: CameraSettings, live_cfg: Live, system: System, snapshot: SnapshotSource,
               onvif: OnvifEndpoint | None = None, web_dir: Path | None = None) -> FastAPI:
    app = FastAPI(title="SkyDome", docs_url="/api/docs", openapi_url="/api/openapi.json", redoc_url=None)

    # ---- status ------------------------------------------------------------------

    @app.get("/api/status")
    def status() -> dict:
        mode, until = live.read_mode(live_cfg.mode_file)
        paths = live_cfg.stream_paths()
        path = STREAM_PATHS[mode]
        return {
            "mode": mode,
            "smooth_until": until.strftime("%Y-%m-%dT%H:%M:%SZ") if until else None,
            "live": live.read_stats(live_cfg.stats_files),
            "stream": {"path": path, "webrtc_port": live_cfg.webrtc_port,
                       "ready": None if paths is None else paths.get(path, {}).get("ready", False),
                       "viewers": None if paths is None else paths.get(path, {}).get("readers", 0)},
            "system": system.snapshot(),
        }

    # ---- Detail / Smooth -----------------------------------------------------------

    @app.put("/api/mode")
    def set_mode(mode: str = Body(..., embed=True)) -> dict:
        try:
            live.write_mode(live_cfg.mode_file, mode)
        except ValueError as e:
            raise HTTPException(400, str(e))
        mode, until = live.read_mode(live_cfg.mode_file)
        return {"mode": mode, "smooth_until": until.strftime("%Y-%m-%dT%H:%M:%SZ") if until else None,
                "stream_path": STREAM_PATHS[mode]}

    # ---- camera settings (shared with ONVIF imaging) ------------------------------

    @app.get("/api/settings")
    def get_settings() -> dict:
        values = settings.effective()
        return {"controls": [{"name": c.name, "min": c.minimum, "max": c.maximum, "step": c.step,
                              "default": c.default, "value": values[c.name], "set": c.name in settings.values}
                             for c in settings.controls().values()]}

    @app.patch("/api/settings")
    def patch_settings(changes: dict[str, int] = Body(...)) -> dict:
        try:
            settings.update(changes)
        except ValueError as e:
            raise HTTPException(400, str(e))
        return get_settings()

    # ---- WebRTC signalling (WHEP), passed through to mediamtx ---------------------

    def whep(method: str, rest: str, body: bytes, ctype: str | None) -> Response:
        req = urllib.request.Request(f"{live_cfg.webrtc}/{rest}", data=body or None, method=method,
                                     headers={"Content-Type": ctype} if ctype else {})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                status_code, data, headers = r.status, r.read(), r.headers
        except urllib.error.HTTPError as e:
            status_code, data, headers = e.code, e.read(), e.headers
        except OSError:
            raise HTTPException(502, "mediamtx isn't answering")
        out = {}
        if headers.get("Location"):   # the session to DELETE / PATCH later, under our prefix
            out["Location"] = "/api/webrtc/" + headers["Location"].lstrip("/")
        return Response(data, status_code=status_code, media_type=headers.get("Content-Type"), headers=out)

    @app.api_route("/api/webrtc/{rest:path}", methods=["POST", "PATCH", "DELETE"], include_in_schema=False)
    async def webrtc(rest: str, request: Request) -> Response:
        body = await request.body()
        return await run_in_threadpool(whep, request.method, rest, body, request.headers.get("content-type"))

    # ---- snapshot --------------------------------------------------------------------

    def jpeg() -> Response:
        data = snapshot.get()
        if data is None:
            raise HTTPException(503, "no dome image (is skydome-live running?)")
        return Response(data, media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    app.get("/api/snapshot.jpg")(jpeg)

    # ---- ONVIF -------------------------------------------------------------------------

    if onvif:
        app.get("/onvif/snapshot.jpg", include_in_schema=False)(jpeg)

        @app.post("/onvif/{service:path}", include_in_schema=False)
        async def soap(request: Request) -> Response:
            data = await request.body()
            host, port = (request.scope.get("server") or ("127.0.0.1", 80))[:2]
            # SOAP handlers block (snapshot waits, camera ioctls): keep them off the event loop
            status_code, body = await run_in_threadpool(onvif.post, data, host, port)
            return Response(body, status_code=status_code, media_type="application/soap+xml; charset=utf-8")

    # ---- the page -------------------------------------------------------------------

    if web_dir and (web_dir / "index.html").exists():
        app.mount("/", StaticFiles(directory=web_dir, html=True), name="web")
    else:
        @app.get("/", include_in_schema=False)
        def no_page() -> HTMLResponse:
            return HTMLResponse("<p>SkyDome: the web page isn't built (web/dist). The API is at "
                                "<a href=/api/docs>/api/docs</a>.</p>")

    return app

