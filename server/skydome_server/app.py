"""The SkyDome web server on port 80 (handoff §5.7–5.9, DECISIONS.md D-021):

    /               the web page (web/dist, built from web/)
    /api/...        status, camera settings, Detail/Smooth, snapshot, full-detail
                    still, zoom requests, ground mask
    /api/webrtc/... mediamtx's WHEP, so the page plays the dome from its own
                    address (the video itself still flows straight from mediamtx)
    /onvif/...      ONVIF for NVRs (D-020)

LAN only, no login for the page or the API (handoff §5.7: "Basic auth
optional; off by default"); ONVIF keeps its own login.
"""

from __future__ import annotations

import io
import json
import time
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timezone
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
ZOOM_PATH = "dome-zoom"   # what the page is zoomed into, at the cameras' sharpness (D-023)


@dataclass
class Live:
    """Where the server finds the live dome's mode file, stats and stream."""
    mode_file: Path
    stats_files: list[Path]
    mediamtx_api: str = "http://127.0.0.1:9997"
    webrtc_port: int = 8889
    webrtc: str = "http://127.0.0.1:8889"
    zoom_file: Path | None = None      # skydome-live renders this rectangle to /dome-zoom (D-023)
    horizon_file: Path | None = None   # ground mask (D-024): on while this file exists
    raw_dir: Path | None = None        # skydome-live saves camera frames here on request (D-023)
    extra_files: tuple[Path, ...] = ()  # put in the raw download for reference (alignment, cameras)
    edge_deg: float = 100.0            # zenith angle at the dome's edge (config/modes.yaml, D-018)
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
               onvif: OnvifEndpoint | None = None, web_dir: Path | None = None,
               full: SnapshotSource | None = None) -> FastAPI:
    app = FastAPI(title="SkyDome", docs_url="/api/docs", openapi_url="/api/openapi.json", redoc_url=None)

    # ---- status ------------------------------------------------------------------

    @app.get("/api/status")
    def status() -> dict:
        mode, until = live.read_mode(live_cfg.mode_file)
        paths = live_cfg.stream_paths()
        path = STREAM_PATHS[mode]
        hf = live_cfg.horizon_file
        return {
            "zoom_path": ZOOM_PATH,
            "edge_zenith_deg": live_cfg.edge_deg,
            "ground_mask": {"available": bool(hf and (hf.exists() or _off(hf).exists())),
                            "on": bool(hf and hf.exists())},
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

    # ---- zoom (D-023) --------------------------------------------------------------

    @app.put("/api/zoom")
    def zoom(x0: float = Body(...), y0: float = Body(...), x1: float = Body(...), y1: float = Body(...)) -> dict:
        """The rectangle of the dome (0..1 across its square) the page is zoomed into.
        Sent again every second or so while zoomed; skydome-live streams it on
        /dome-zoom until the requests stop."""
        if not live_cfg.zoom_file:
            raise HTTPException(404, "zoom isn't set up")
        if not (-1 <= x0 < x1 <= 2 and -1 <= y0 < y1 <= 2) or x1 - x0 < 1e-3:
            raise HTTPException(400, "want x0 < x1, y0 < y1 around 0..1")
        tmp = live_cfg.zoom_file.with_name(live_cfg.zoom_file.name + ".tmp")
        tmp.write_text(f"{x0:.6f} {y0:.6f} {x1:.6f} {y1:.6f}\n")
        tmp.replace(live_cfg.zoom_file)
        return {"path": ZOOM_PATH}

    # ---- ground mask (D-024) ---------------------------------------------------------

    @app.get("/api/ground-mask")
    def get_ground_mask() -> dict:
        """The mask: 360 horizon altitudes (north, then clockwise), and whether it's on."""
        hf = live_cfg.horizon_file
        src = hf if hf and hf.exists() else _off(hf) if hf and _off(hf).exists() else None
        return {"on": bool(hf and hf.exists()), "alt": _read_horizon(src) if src else None}

    @app.put("/api/ground-mask")
    def ground_mask(on: bool | None = Body(None), alt: list[float] | None = Body(None)) -> dict:
        """Save an edited mask (alt) and/or turn it on or off: var/horizon.txt is
        the mask in use, var/horizon.off.txt the same mask switched off.
        skydome-live picks up either change within a second."""
        hf = live_cfg.horizon_file
        if not hf:
            raise HTTPException(404, "no ground mask file set up")
        if alt is not None:
            if len(alt) != 360 or not all(-20 <= a <= 90 for a in alt):
                raise HTTPException(400, "want 360 altitudes between -20 and 90")
            dest = hf if (on if on is not None else hf.exists()) else _off(hf)
            tmp = dest.with_name(dest.name + ".tmp")
            tmp.write_text("# Ground mask: horizon altitude in degrees for each degree of azimuth,\n"
                           "# line 1 = north, then clockwise (east = line 91). Edited on the web page\n"
                           + "".join(f"{a:.1f}\n" for a in alt))
            tmp.replace(dest)
            (_off(hf) if dest == hf else hf).unlink(missing_ok=True)
        if not (hf.exists() or _off(hf).exists()):
            raise HTTPException(404, "no ground mask yet (tools/detect_horizon.py, or draw one)")
        if on and _off(hf).exists():
            _off(hf).replace(hf)
        elif on is False and hf.exists():
            hf.replace(_off(hf))
        return {"on": hf.exists()}

    # ---- snapshot --------------------------------------------------------------------

    def jpeg() -> Response:
        data = snapshot.get()
        if data is None:
            raise HTTPException(503, "no dome image (is skydome-live running?)")
        return Response(data, media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    app.get("/api/snapshot.jpg")(jpeg)

    @app.get("/api/raw.zip")
    def raw_zip() -> Response:
        """Every pixel there is: each camera's latest frame exactly as it sent it
        (3840 x 2160 JPEG, no stitching or re-encoding), zipped, with the
        camera layout and alignment for reference."""
        d = live_cfg.raw_dir
        stats = live.read_stats(live_cfg.stats_files)
        names = [n for n, c in ((stats or {}).get("cameras") or {}).items() if c.get("state") == "live"]
        if not d or not names:
            raise HTTPException(503, "no live cameras (is skydome-live running?)")
        asked = time.time()
        (d / "request").touch()
        got: dict[str, Path] = {}
        while time.time() - asked < 5 and len(got) < len(names):
            time.sleep(0.05)
            for n in names:
                p = d / f"{n}.jpg"
                if n not in got and p.exists() and p.stat().st_mtime >= asked - 0.01:
                    got[n] = p
        if not got:
            raise HTTPException(503, "the cameras didn't send a frame in time")
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:   # JPEGs don't compress further
            for n, p in got.items():
                z.write(p, f"{n}.jpg")
            for p in live_cfg.extra_files:
                if p.exists():
                    z.write(p, p.name)
            z.writestr("README.txt", f"SkyDome raw frames, {stamp}\n"
                       "Each camera's frame exactly as the camera sent it (MJPEG, 3840 x 2160).\n"
                       f"Cameras: {', '.join(got)}"
                       + (f"; missing: {', '.join(sorted(set(names) - set(got)))}" if len(got) < len(names) else "")
                       + "\ncameras.yaml: which camera points where; extrinsics.yaml: how each is really\n"
                       "pointed (from the overlap alignment).\n")
        return Response(buf.getvalue(), media_type="application/zip",
                        headers={"Content-Disposition": f'attachment; filename="skydome-raw-{stamp}.zip"',
                                 "Cache-Control": "no-store"})

    @app.get("/api/full.jpg")
    def full_jpeg() -> Response:
        """The dome at the cameras' own sharpness (6656², ~10 MB)."""
        data = full.get() if full else None
        if data is None:
            raise HTTPException(503, "no full-detail still (is skydome-live running?)")
        return Response(data, media_type="image/jpeg", headers={"Cache-Control": "no-store"})

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


def _off(horizon_file: Path) -> Path:
    return horizon_file.with_name(horizon_file.stem + ".off" + horizon_file.suffix)


def _read_horizon(path: Path) -> list[float] | None:
    vals = [float(x) for line in path.read_text().splitlines() for x in line.split("#")[0].split()]
    return vals if len(vals) == 360 else None

