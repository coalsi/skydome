#!/usr/bin/env python3
"""Align the rig to the stars (handoff §5.3 "Auto-align from sky",
DECISIONS.md D-027, D-028). Run on the Jetson at night, with the live dome
running (the frames come from its raw download):

    uv run --project pipeline tools/align_from_stars.py          # report only
    uv run --project pipeline tools/align_from_stars.py --save   # write the result

The web server runs it by itself once a night after dark, and from the
"Calibrate from the stars" button (skydome_server/sky.py), so a rig set up
somewhere new, or turned, finds north and every star on its own.

It finds the stars in a few averaged raw frames, matches them to the Yale
Bright Star Catalogue for the site and the time (var/bsc5.dat), and solves
the rig: the camera with the most stars says how the whole rig sits (found
blind, from the shapes of its star triangles, if it has moved), then each
camera is refined on its own stars, pointing and, with enough stars, lens
(scale, centre, distortion: lenses get swapped and refocused). A camera
with too few stars turns with the rig.

--save writes config/calibration/extrinsics.yaml and <camera>.yaml for each
lens it could fit well, but only when some camera's view of the sky moved
--min-move degrees (0.2: night-to-night the same rig solves ~0.1° apart;
0 always writes): skydome-live rebuilds its stitch tables and restarts when
they change, so an unchanged rig keeps streaming. The previous files are kept as
*.before-stars. --status FILE writes progress and the result as JSON for
the web page.
"""

from __future__ import annotations

import argparse
import io
import json
import shutil
import sys
import time
import urllib.request
import zipfile
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import yaml
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "pipeline"))

from skydome_pipeline.calibration import lens_to_dict  # noqa: E402
from skydome_pipeline.camera import Camera  # noqa: E402
from skydome_pipeline.config import calibrated_cameras, load_site  # noqa: E402
from skydome_pipeline.geometry import vec_to_altaz  # noqa: E402
from skydome_pipeline.stars import detect_stars, load_bsc, sky_stars, solve_rig, star_directions  # noqa: E402

CALIB = ROOT / "config" / "calibration"
LENS_OK = (40, 1.5)    # a lens fit is kept with at least this many stars and under this rms (px)
# Night to night the same rig solves ~0.1° differently (other stars, seeing);
# a rig that has been moved or bumped is out by degrees
MIN_MOVE_DEG = 0.2


def grab(url: str, frames: int) -> tuple[dict[str, np.ndarray], datetime]:
    """Each live camera's raw frames, averaged, and the middle time."""
    acc: dict[str, list[np.ndarray]] = {}
    times = []
    for _ in range(frames):
        t0 = time.time()
        z = zipfile.ZipFile(io.BytesIO(urllib.request.urlopen(url, timeout=30).read()))
        times.append((t0 + time.time()) / 2)
        for n in z.namelist():
            if n.endswith(".jpg"):
                acc.setdefault(n[:-4], []).append(np.asarray(Image.open(io.BytesIO(z.read(n))).convert("L"), np.float32))
    t = datetime.fromtimestamp(float(np.median(times)), timezone.utc)
    return {n: np.mean(v, axis=0) for n, v in acc.items()}, t


def sky_shift_deg(old: Camera, new: Camera) -> float:
    """How far the same camera pixel now looks, across the part of the
    picture that sees the sky (90th percentile, degrees): pointing and lens
    together, where it matters."""
    u, v = np.meshgrid(np.linspace(0, old.width - 1, 33), np.linspace(0, old.height - 1, 19))
    a = old.pixel_to_world(u.ravel(), v.ravel())
    b = new.pixel_to_world(u.ravel(), v.ravel())
    sky = a[:, 2] > np.sin(np.radians(3))
    if not sky.any():
        return 0.0
    return float(np.percentile(np.degrees(np.arccos(np.clip(np.sum(a[sky] * b[sky], 1), -1, 1))), 90))


class Status:
    """Progress and the result for the web page (--status)."""

    def __init__(self, path: str | None):
        self.path = Path(path) if path else None
        self.doc = {"state": "running", "started_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}

    def __call__(self, message: str, **fields) -> None:
        print(message, flush=True)
        self.doc.update(message=message, **fields)
        if self.path:
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(json.dumps(self.doc, indent=1))
            tmp.replace(self.path)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--save", action="store_true", help="write the solved alignment and lenses (if anything moved)")
    ap.add_argument("--frames", type=int, default=6, help="raw frames averaged per camera (1 s each)")
    ap.add_argument("--url", default="http://localhost/api/raw.zip")
    ap.add_argument("--status", help="write progress and the result here (JSON)")
    ap.add_argument("--min-move", type=float, default=MIN_MOVE_DEG,
                    help="with --save, write only if a camera's sky moved this many degrees (0: always)")
    args = ap.parse_args()
    say = Status(args.status)
    try:
        return run(args, say)
    except Exception as e:   # the page shows why, then the next try comes round
        say(f"failed: {e}", state="failed")
        raise


def run(args, say: Status) -> int:
    site = load_site(ROOT / "config")
    cat = load_bsc(ROOT / "var" / "bsc5.dat")
    cams = calibrated_cameras(ROOT / "config")
    # The lenses are all one model: a camera without its own lens fit starts from one that has
    own_lens = {c.name for c in cams if (CALIB / f"{c.name}.yaml").exists()}
    if own_lens:
        ref = next(c.lens for c in cams if c.name in own_lens)
        cams = [c if c.name in own_lens else replace(c, lens=replace(c.lens, fx=ref.fx, fy=ref.fy, k=ref.k))
                for c in cams]
    say(f"catalogue: {len(cat.vmag)} stars; site {site.name} ({site.latitude_deg}, {site.longitude_deg}); "
        f"averaging {args.frames} frames")
    images, t = grab(args.url, args.frames)
    dirs, mags = star_directions(cat, site.latitude_deg, site.longitude_deg, t)
    dets = {c.name: sky_stars(images[c.name], detect_stars(images[c.name], sigma=4.5, max_stars=3000))
            for c in cams if c.name in images}
    for c in cams:
        if c.name not in images:
            print(f"{c.name:7s} no frame (not live?)")
    say(f"{t:%Y-%m-%d %H:%M:%S} UTC: {len(dirs)} catalogue stars up; star-like points "
        + ", ".join(f"{n} {len(d.u)}" for n, d in dets.items()) + "; solving (a minute or a few)")

    rig = solve_rig(cams, dets, dirs, mags)
    if rig is None:
        say("couldn't match the stars: cloudy, too light, or the cameras aren't on Night?", state="failed",
            utc=f"{t:%Y-%m-%dT%H:%M:%SZ}")
        return 1

    z = rig.turn @ np.array([0.0, 0.0, 1.0])
    x = rig.turn @ np.array([1.0, 0.0, 0.0])
    heading = float((np.degrees(np.arctan2(-x[1], x[0])) + 180) % 360 - 180)   # + = the rig turned clockwise
    tilt = float(np.degrees(np.arccos(np.clip(z[2], -1, 1))))
    print(f"\nrig found from {rig.anchor}: turned {heading:+.2f}° (clockwise +), tilted {tilt:.2f}° "
          "since the last alignment\n")
    report, moved = {}, []
    by_name = {c.name: c for c in cams}
    for name, cam in rig.cameras.items():
        old = by_name[name]
        sol = rig.own.get(name)
        az, alt = (float(v) for v in vec_to_altaz(cam.axis))
        lens_ok = bool(sol and sol.fit_lens and sol.matches >= LENS_OK[0] and sol.rms_px <= LENS_OK[1])
        if not lens_ok:
            cam = replace(cam, lens=old.lens)   # keep the lens it has; only the pointing is new
            rig.cameras[name] = cam
        shift = sky_shift_deg(old, cam)
        if lens_ok and name not in own_lens:
            shift = max(shift, args.min_move)   # the dome still uses the default lens for it
        if shift >= args.min_move:
            moved.append(name)
        report[name] = {"stars": sol.matches if sol else 0, "rms_px": round(sol.rms_px, 2) if sol else None,
                        "az_deg": round(az, 2), "alt_deg": round(alt, 2), "moved_deg": round(shift, 3),
                        "own": sol is not None, "lens": lens_ok}
        print(f"{name:7s} " + (f"{sol.matches:4d} stars, rms {sol.rms_px:4.2f} px ({sol.rms_arcmin:4.1f}')"
                               if sol else "  too few stars: turned with the rig      ")
              + f"  points az {az:6.2f} alt {alt:5.2f}; its sky moved {shift:5.2f}°" + (" (lens refitted)" if lens_ok else ""))
    result = dict(utc=f"{t:%Y-%m-%dT%H:%M:%SZ}", anchor=rig.anchor, heading_deg=round(heading, 2),
                  tilt_deg=round(tilt, 2), cameras=report, stars=sum(r["stars"] for r in report.values()))

    if not args.save:
        say("report only (--save writes it)", state="done", applied=False, **result)
        return 0
    if not moved or (args.min_move > 0 and not any(report[n]["own"] for n in moved)):
        say(f"already aligned: no camera's sky moved {args.min_move:g}° or more", state="done", applied=False, **result)
        return 0
    moved = list(rig.cameras)   # write the whole rig together: the cameras' places on it were solved as one
    CALIB.mkdir(parents=True, exist_ok=True)
    ext = CALIB / "extrinsics.yaml"
    prev = yaml.safe_load(ext.read_text()) if ext.exists() else {}
    if ext.exists():
        shutil.copy(ext, ext.with_name("extrinsics.yaml.before-stars"))
    entries = dict((prev or {}).get("cameras") or {})
    for name, cam in rig.cameras.items():
        az, alt = (float(v) for v in vec_to_altaz(cam.axis))
        r = report[name]
        entries[name] = {"rotation": [[round(float(x), 9) for x in row] for row in cam.rotation],
                         "az_deg": round(az, 2), "alt_deg": round(alt, 2), "matches": r["stars"],
                         "rms_px": r["rms_px"], "method": "stars" if r["own"] else f"stars (turned with {rig.anchor})"}
    doc = {"method": "stars", "date_utc": result["utc"]}
    if prev and prev.get("lens_hfov_deg"):
        doc["lens_hfov_deg"] = prev["lens_hfov_deg"]   # still the lens for cameras without their own fit
    doc["cameras"] = {n: entries[n] for n in ("zenith", "north", "east", "south", "west") if n in entries}
    doc["cameras"].update({n: e for n, e in entries.items() if n not in doc["cameras"]})
    ext.write_text("# Written by tools/align_from_stars.py: world-from-camera rotations\n"
                   "# (columns = camera x, y, z in east-north-up; DECISIONS.md D-003, D-027)\n"
                   + yaml.safe_dump(doc, sort_keys=False))
    print(f"\nwrote {ext}")
    for name in moved:
        sol = rig.own.get(name)
        if not report[name]["lens"]:
            continue
        f = CALIB / f"{name}.yaml"
        if f.exists():
            shutil.copy(f, f.with_name(f"{name}.yaml.before-stars"))
        f.write_text("# Written by tools/align_from_stars.py (lens fitted to the stars)\n" + yaml.safe_dump(
            {"camera": name, "date_utc": result["utc"], **lens_to_dict(sol.camera.lens),
             "rms_px": round(sol.rms_px, 3), "stars": sol.matches}, sort_keys=False))
        print(f"wrote {f}")
    say("aligned; the dome restarts with it in a few seconds", state="done", applied=True, **result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
