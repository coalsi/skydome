"""The SkyDome web server on port 80 (handoff §5.7–5.9, DECISIONS.md D-021):

    /               the web page (web/dist, built from web/)
    /api/...        status, camera settings, Detail/Smooth, snapshot, full-detail
                    still, zoom requests, ground mask, the stars and calibrating
                    from them
    /api/webrtc/... mediamtx's WHEP, so the page plays the dome from its own
                    address (the video itself still flows straight from mediamtx)
    /onvif/...      ONVIF for NVRs (D-020)

LAN only, no login for the page or the API (handoff §5.7: "Basic auth
optional; off by default"); ONVIF keeps its own login.
"""

from __future__ import annotations

import io
import json
import threading
import time
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timezone
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from . import focus, live
from .asi_settings import AsiSettings
from .camera_settings import CameraSettings
from .objects import ObjectLog
from .sky import BUTTON_SUN_ALT, Sky
from .onvif.http import OnvifEndpoint, SnapshotSource
from .system import System

STREAM_PATHS = {"detail": "dome", "smooth": "dome-smooth"}   # mediamtx paths (config/mediamtx.yml)
ZOOM_PATH = "dome-zoom"   # what the page is zoomed into, at the cameras' sharpness (D-023)
PHONE_PATH = "dome-phone" # the whole picture, small enough for a phone to decode (D-032)

# One click for the sky (D-025). Night: the longest exposure the cameras do
# (1 s; they slow to 1 fps for it), full gain, no backlight lift, less colour
# (it's mostly noise at night), and the stretch that lifts faint stars off a
# background measured at ~36-38 (brightness 132). Day: the cameras' own auto.
# The cameras' own contrast/gamma/brightness can't do the stretch: on the rig
# they moved stars by 2-3 levels at most, and contrast > 65 crushed the sky to 0.
PRESETS = {
    # Brightness 154 / contrast 73 / sharpness 230: A/B-tested on the rig 2026-11-03 against
    # 132 / 65 / 128, interleaved on the same sky: +24% stars, brightest ~10-15% stronger.
    # Contrast 80 crushes the night sky to 0-1 and loses the faint ones: stay below ~75
    "night": {"camera": {"auto_exposure": 1, "exposure_dynamic_framerate": 1, "exposure_time_absolute": 10000,
                         "gain": 190, "backlight_compensation": 0, "saturation": 60,
                         "brightness": 154, "contrast": 73, "sharpness": 230},
              # Stretch black just under the sky background those give (29); k 45 shows
              # the most clear (6σ) stars with the stack, k 60 only lifts the background
              "stretch": {"black": 27, "k": 45},
              "stack": 16},                          # 16 s of light; stars drift ~2 px in that (D-026)
    "day": {"camera": {"auto_exposure": 3, "exposure_dynamic_framerate": 0, "gain": 140,
                       "backlight_compensation": 48, "saturation": 100,
                       "brightness": 128, "contrast": 65, "sharpness": 128},
            "stretch": {"black": 0, "k": 0}, "stack": 0},
}

# The same for the ZWO all-sky camera (D-029), whose own automatic exposure
# runs from daylight video to 20 s night frames, so the presets only change
# how the picture is shown. Night: linear (gamma 1), the sky held at 35%, since
# seven exposures on the real sky (2026-11-05, D-031) caught the most catalogue
# stars with the most gain: 423 at 20 s gain 200, 551 at gain 300, where the
# brightest stars clip. The stretch's black sits just under that sky, and 2
# frames are stacked (40 s of light; stars drift ~4 px in it, less than their
# size). Day: a display gamma, no stretch, no stack. sky.py switches these by
# the Sun.
ASI_PRESETS = {
    "night": {"camera": {"auto_exposure": 1, "gamma": 100, "auto_target": 35, "auto_max_gain": 300,
                         "auto_max_exposure_ms": 20000, "wb_red": 102, "wb_blue": 76},
              "stretch": {"black": 56, "k": 14}, "stack": 2},
    "day": {"camera": {"auto_exposure": 1, "gamma": 220, "auto_target": 26, "auto_max_gain": 200,
                       "auto_max_exposure_ms": 1000, "wb_red": 85, "wb_blue": 118},
            "stretch": {"black": 0, "k": 0}, "stack": 0},
}


@dataclass
class Live:
    """Where the server finds the live dome's mode file, stats and stream."""
    mode_file: Path
    stats_files: list[Path]
    mediamtx_api: str = "http://127.0.0.1:9997"
    webrtc_port: int = 8889
    webrtc: str = "http://127.0.0.1:8889"
    zoom_file: Path | None = None      # skydome-live renders this rectangle to /dome-zoom (D-023)
    phone_file: Path | None = None     # touched while a phone watches: skydome-live runs /dome-phone (D-032)
    horizon_file: Path | None = None   # ground mask (D-024): on while this file exists
    raw_dir: Path | None = None        # skydome-live saves camera frames here on request (D-023)
    stretch_file: Path | None = None   # night stretch "black k" (D-025)
    stack_file: Path | None = None     # stacking depth in frames (D-026)
    extra_files: tuple[Path, ...] = ()  # put in the raw download for reference (alignment, cameras)
    edge_deg: float = 100.0            # zenith angle at the dome's edge (config/modes.yaml, D-018)
    north_deg: float = 0.0             # where north sits, counterclockwise from the top
    aspect: float = 1.0                # the stream's width / height
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


def create_app(settings: CameraSettings | AsiSettings, live_cfg: Live, system: System, snapshot: SnapshotSource,
               onvif: OnvifEndpoint | None = None, web_dir: Path | None = None,
               full: SnapshotSource | None = None, sky: Sky | None = None,
               objects: ObjectLog | None = None) -> FastAPI:
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
            "phone_path": PHONE_PATH if live_cfg.phone_file else None,
            "edge_zenith_deg": live_cfg.edge_deg,
            "north_deg": live_cfg.north_deg,
            "stream_aspect": live_cfg.aspect,
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

    # ---- night stretch and presets (D-025) --------------------------------------------

    def read_stretch() -> dict:
        try:
            black, k = (float(x) for x in live_cfg.stretch_file.read_text().split()[:2])
        except (AttributeError, OSError, ValueError):
            black, k = 0.0, 0.0
        return {"black": black, "k": k}

    def write_stretch(black: float, k: float) -> dict:
        if not live_cfg.stretch_file:
            raise HTTPException(404, "night stretch isn't set up")
        if not (0 <= black <= 200 and 0 <= k <= 100):
            raise HTTPException(400, "want black 0..200 and k 0..100")
        tmp = live_cfg.stretch_file.with_name(live_cfg.stretch_file.name + ".tmp")
        tmp.write_text(f"{black:g} {k:g}\n")
        tmp.replace(live_cfg.stretch_file)
        return read_stretch()

    def read_stack() -> int:
        try:
            return int(live_cfg.stack_file.read_text().split()[0])
        except (AttributeError, OSError, ValueError, IndexError):
            return 0

    def write_stack(frames: int) -> int:
        """Rewritten even when unchanged: skydome-live starts the stack again
        on every write, so frames from other settings don't mix in."""
        if not live_cfg.stack_file:
            raise HTTPException(404, "stacking isn't set up")
        if not 0 <= frames <= 600:
            raise HTTPException(400, "want 0..600 frames")
        tmp = live_cfg.stack_file.with_name(live_cfg.stack_file.name + ".tmp")
        tmp.write_text(f"{frames}\n")
        tmp.replace(live_cfg.stack_file)
        return read_stack()

    @app.get("/api/stack")
    def get_stack() -> dict:
        return {"frames": read_stack()}

    @app.put("/api/stack")
    def put_stack(frames: int = Body(..., embed=True)) -> dict:
        """Stack each camera's last `frames` frames (0 or 1 = off). With 1 s
        exposures that's seconds of light; noise falls with its square root."""
        return {"frames": write_stack(frames)}

    @app.get("/api/stretch")
    def get_stretch() -> dict:
        return read_stretch()

    @app.put("/api/stretch")
    def put_stretch(black: float = Body(...), k: float = Body(...)) -> dict:
        """Night stretch of the dome: black point, and k for 255·asinh(k·x)/asinh(k) (0 = off)."""
        return write_stretch(black, k)

    presets = ASI_PRESETS if isinstance(settings, AsiSettings) else PRESETS

    def apply_preset(name: str) -> dict:
        """Night or Day in one go: the camera settings, the stretch and the stack.
        KeyError for an unknown name, ValueError for a setting out of range."""
        p = presets[name]
        ctrls = settings.controls()
        settings.update({k: v for k, v in p["camera"].items() if k in ctrls})
        out = {"preset": name, "stretch": write_stretch(**p["stretch"])}
        if live_cfg.stack_file:
            out["stack"] = write_stack(p["stack"])   # also restarts the stack on the new settings
        return out

    app.state.apply_preset = apply_preset   # sky.py switches them at dusk and dawn

    @app.post("/api/preset/{name}")
    def preset(name: str) -> dict:
        """Night or Day in one go."""
        if name not in presets:
            raise HTTPException(404, f"presets: {', '.join(presets)}")
        try:
            return apply_preset(name)
        except ValueError as e:
            raise HTTPException(400, str(e))

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
        # Two pages can ask at the same moment: each writes its own, the last one wins
        tmp = live_cfg.zoom_file.with_name(f"{live_cfg.zoom_file.name}.{threading.get_ident()}.tmp")
        tmp.write_text(f"{x0:.6f} {y0:.6f} {x1:.6f} {y1:.6f}\n")
        tmp.replace(live_cfg.zoom_file)
        return {"path": ZOOM_PATH}

    @app.put("/api/phone")
    def phone() -> dict:
        """A phone is watching. Sent every couple of seconds; skydome-live streams
        /dome-phone until the requests stop."""
        if not live_cfg.phone_file:
            raise HTTPException(404, "the phone stream isn't set up")
        live_cfg.phone_file.touch()
        return {"path": PHONE_PATH}

    # ---- the stars: names, and calibrating the rig from them (D-028) -----------------

    def need_sky() -> Sky:
        if sky is None:
            raise HTTPException(404, "no star catalogue set up")
        return sky

    @app.get("/api/sky")
    def get_sky(max_mag: float = 5.5) -> dict:
        """Stars up now with their names: rows of [name, designation, constellation, mag, az, alt]."""
        return need_sky().stars(min(max(max_mag, 0.0), 6.5))

    @app.get("/api/sky/moving")
    def get_moving() -> dict:
        """What moves, now (D-030): bodies [{name, kind, az, alt, ...}], satellites
        [[name, norad, az, alt, range_km, sunlit]], planes [[callsign, hex, az, alt,
        range_km, alt_ft, speed_kt, track]] (None without an ADS-B receiver)."""
        return need_sky().moving()

    @app.get("/api/calibration")
    def get_calibration() -> dict:
        return need_sky().status()

    @app.post("/api/calibration")
    def calibrate() -> dict:
        s = need_sky()
        sun = s.sun_alt()
        if sun > BUTTON_SUN_ALT:
            raise HTTPException(409, f"needs a dark sky: the Sun is at {sun:.0f}° (wait until it's {BUTTON_SUN_ALT:.0f}°)")
        if not s.start("asked from the page", min_move=0.0):
            raise HTTPException(409, "already calibrating")
        return s.status()

    @app.put("/api/calibration")
    def set_calibration(auto: bool = Body(..., embed=True)) -> dict:
        s = need_sky()
        s.set_auto(auto)
        return s.status()

    # ---- the sky watcher's log (D-030) ------------------------------------------------

    def need_objects() -> ObjectLog:
        if objects is None:
            raise HTTPException(404, "the sky watcher isn't set up")
        return objects

    @app.get("/api/objects")
    def list_objects(limit: int = 100, before: int | None = None, kind: str | None = None,
                     unknown: bool = False) -> dict:
        """What the sky watcher saw, newest first: each with its path ([t, az, alt]),
        what it is (kind), what the watcher made of it, your label, pictures."""
        log = need_objects()
        return {"objects": log.list(limit, before, kind, unknown), "labels": log.labels()}

    @app.get("/api/objects/summary")
    def objects_summary(hours: float = 24) -> dict:
        return need_objects().summary(hours)

    @app.get("/api/objects/media/{rel:path}", include_in_schema=False)
    def object_media(rel: str) -> FileResponse:
        p = need_objects().media_file(rel)
        if p is None:
            raise HTTPException(404, "no such picture")
        return FileResponse(p, headers={"Cache-Control": "max-age=86400"})

    @app.get("/api/objects/{oid}")
    def get_object(oid: int) -> dict:
        o = need_objects().get(oid)
        if o is None:
            raise HTTPException(404, "no such object")
        return o

    @app.put("/api/objects/{oid}")
    def label_object(oid: int, label: str | None = Body(None, embed=True)) -> dict:
        """Say what it was (or null to take your label back); the watcher learns from it."""
        try:
            o = need_objects().set_label(oid, label)
        except ValueError as e:
            raise HTTPException(400, str(e))
        if o is None:
            raise HTTPException(404, "no such object")
        return o

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

    def raw_frames() -> tuple[dict[str, Path], list[str], float]:
        """Each live camera's next frame: {name: file}, the names asked for, and the longest exposure (µs)."""
        d = live_cfg.raw_dir
        stats = live.read_stats(live_cfg.stats_files)
        names = [n for n, c in ((stats or {}).get("cameras") or {}).items() if c.get("state") == "live"]
        if not d or not names:
            raise HTTPException(503, "no live cameras (is skydome-live running?)")
        asked = time.time()
        d.mkdir(exist_ok=True)   # /tmp may have been cleaned since skydome-live made it
        (d / "request").touch()
        got: dict[str, Path] = {}
        # A ZWO night frame can take two exposures to arrive: the one under way, then its own
        exposure = max(((stats or {}).get("cameras") or {}).get(n, {}).get("exposure_us") or 0 for n in names)
        while time.time() - asked < 5 + 2.2 * exposure / 1e6 and len(got) < len(names):
            time.sleep(0.05)
            for n in names:
                for ext in (".jpg", ".pgm"):
                    p = d / f"{n}{ext}"
                    if n not in got and p.exists() and p.stat().st_mtime >= asked - 0.01:
                        got[n] = p
        if not got:
            raise HTTPException(503, "the cameras didn't send a frame in time")
        return got, names, exposure

    @app.get("/api/focus")
    def focus_frame(x: float = 0.5, y: float = 0.5, size: int = 512) -> dict:
        """The camera's next frame for the focus page: the whole picture, the
        spot around x, y (0..1) at every pixel, and how sharp it all is."""
        got, _, exposure = raw_frames()
        pgm = next((p for p in got.values() if p.suffix == ".pgm"), None)
        if pgm is None:
            raise HTTPException(409, "focusing needs the ZWO camera")
        return focus.measure(focus.read_pgm(pgm), x, y, size, exposure)

    @app.get("/api/raw.zip")
    def raw_zip() -> Response:
        """Every pixel there is: each camera's latest frame, zipped, with the
        camera layout and alignment for reference. A UVC camera's exactly as
        it sent it (JPEG); the ZWO camera's luminance at 16 bits (PGM, linear
        at night), before stitching or encoding (D-029)."""
        got, names, _ = raw_frames()
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:   # JPEGs don't compress further
            for n, p in got.items():
                z.write(p, f"{n}{p.suffix}")
            for p in live_cfg.extra_files:
                if p.exists():
                    z.write(p, p.name)
            z.writestr("README.txt", f"SkyDome raw frames, {stamp}\n"
                       "Each camera's frame: .jpg exactly as a UVC camera sent it; .pgm the ZWO camera's\n"
                       "luminance at 16 bits (linear when its gamma is 100, as at night).\n"
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

