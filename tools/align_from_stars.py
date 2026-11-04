#!/usr/bin/env python3
"""Align every camera to the stars (handoff §5.3 "Auto-align from sky",
DECISIONS.md D-027). Run on the Jetson on a clear night, with the live dome
running (the frames come from its raw download):

    uv run --project pipeline tools/align_from_stars.py          # report only
    uv run --project pipeline tools/align_from_stars.py --save   # write the result

For each camera it finds the stars in a few averaged raw frames, matches
them to the Yale Bright Star Catalogue for the site and the time (cached in
var/bsc5.dat after the first run), and solves how the camera really points.
With enough stars across the picture it also fits that camera's lens
(scale, centre, distortion), which matters once lenses have been swapped.
A camera far from where the config says (turned, swapped) is found blind,
from the shapes of its star triangles; that takes a minute or so.

--save writes config/calibration/extrinsics.yaml (rotations; true north,
level from the stars) and config/calibration/<camera>.yaml for each lens it
could fit well; skydome-live rebuilds its stitch tables when they change
(restart it: sudo systemctl restart skydome-live). The previous files are
kept as *.before-stars.
"""

from __future__ import annotations

import argparse
import io
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
from skydome_pipeline.config import calibrated_cameras, load_site  # noqa: E402
from skydome_pipeline.geometry import vec_to_altaz  # noqa: E402
from skydome_pipeline.stars import detect_stars, load_bsc, sky_stars, solve_camera, star_directions  # noqa: E402

CALIB = ROOT / "config" / "calibration"
LENS_OK = (40, 1.5)   # a lens fit is kept with at least this many stars and under this rms (px)


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


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--save", action="store_true", help="write the solved alignment and lenses")
    ap.add_argument("--frames", type=int, default=4, help="raw frames averaged per camera (1 s each)")
    ap.add_argument("--url", default="http://localhost/api/raw.zip")
    args = ap.parse_args()

    site = load_site(ROOT / "config")
    cat = load_bsc(ROOT / "var" / "bsc5.dat")
    print(f"catalogue: {len(cat.vmag)} stars; site {site.name} ({site.latitude_deg}, {site.longitude_deg})")
    images, t = grab(args.url, args.frames)
    dirs, mags = star_directions(cat, site.latitude_deg, site.longitude_deg, t)
    print(f"{t:%Y-%m-%d %H:%M:%S} UTC: {len(dirs)} catalogue stars above 8°\n")

    solved = {}
    hint = None   # the lenses are all one model: start each camera from the best lens fitted so far
    cams = sorted(calibrated_cameras(ROOT / "config"), key=lambda c: c.name != "zenith")   # most stars first
    for cam in cams:
        if cam.name not in images:
            print(f"{cam.name:7s} no frame (not live?)")
            continue
        det = sky_stars(images[cam.name], detect_stars(images[cam.name], sigma=4.5, max_stars=3000))
        start = cam
        if hint is not None and not (CALIB / f"{cam.name}.yaml").exists():
            s = hint.fx / hint.width
            start = replace(cam, lens=replace(cam.lens, fx=s * cam.lens.width, fy=s * cam.lens.width, k=hint.k))
        sol = solve_camera(start, det, dirs, mags)
        if sol is None:
            print(f"{cam.name:7s} {len(det.u)} star-like points, couldn't match them to the catalogue (cloud? far off?)")
            continue
        az0, alt0 = (float(x) for x in vec_to_altaz(cam.axis))
        az1, alt1 = (float(x) for x in vec_to_altaz(sol.camera.axis))
        turn = float(np.degrees(np.arccos(np.clip(cam.axis @ sol.camera.axis, -1, 1))))
        lens = sol.camera.lens
        print(f"{cam.name:7s} {sol.matches:4d} stars matched, rms {sol.rms_px:4.2f} px ({sol.rms_arcmin:4.1f}')  "
              f"points az {az1:6.2f} alt {alt1:5.2f} (was {az0:6.2f} {alt0:5.2f}, moved {turn:4.2f}°)"
              + (f"  lens: scale {lens.fx / cam.lens.fx:.4f}, centre {lens.cx - cam.lens.cx:+.1f},"
                 f"{lens.cy - cam.lens.cy:+.1f} px, k1 {lens.k[0]:+.4f}" if sol.fit_lens else "  lens unchanged"))
        solved[cam.name] = sol
        if sol.fit_lens and sol.rms_px < 1.0 and (hint is None or sol.matches > 60):
            hint = sol.camera.lens

    if not args.save:
        print("\n(report only; --save writes it)")
        return 0
    if not solved:
        print("nothing solved; nothing written")
        return 1
    CALIB.mkdir(parents=True, exist_ok=True)
    ext = CALIB / "extrinsics.yaml"
    prev = yaml.safe_load(ext.read_text()) if ext.exists() else {}
    if ext.exists():
        shutil.copy(ext, ext.with_name("extrinsics.yaml.before-stars"))
    cams = dict((prev or {}).get("cameras") or {})
    for name, sol in solved.items():
        az, alt = (float(x) for x in vec_to_altaz(sol.camera.axis))
        cams[name] = {"rotation": [[round(float(x), 9) for x in row] for row in sol.camera.rotation],
                      "az_deg": round(az, 2), "alt_deg": round(alt, 2), "matches": sol.matches,
                      "rms_arcmin": round(sol.rms_arcmin, 2), "method": "stars"}
    doc = {"method": "stars", "date_utc": f"{t:%Y-%m-%dT%H:%M:%SZ}"}
    if prev and prev.get("lens_hfov_deg"):
        doc["lens_hfov_deg"] = prev["lens_hfov_deg"]   # still the lens for cameras without their own fit
    doc["cameras"] = {n: cams[n] for n in ("zenith", "north", "east", "south", "west") if n in cams}
    ext.write_text("# Written by tools/align_from_stars.py: world-from-camera rotations\n"
                   "# (columns = camera x, y, z in east-north-up; DECISIONS.md D-003, D-027)\n"
                   + yaml.safe_dump(doc, sort_keys=False))
    print(f"\nwrote {ext}")
    for name, sol in solved.items():
        if sol.fit_lens and sol.matches >= LENS_OK[0] and sol.rms_px <= LENS_OK[1]:
            f = CALIB / f"{name}.yaml"
            if f.exists():
                shutil.copy(f, f.with_name(f"{name}.yaml.before-stars"))
            f.write_text("# Written by tools/align_from_stars.py (lens fitted to the stars)\n" + yaml.safe_dump(
                {"camera": name, "date_utc": f"{t:%Y-%m-%dT%H:%M:%SZ}", **lens_to_dict(sol.camera.lens),
                 "rms_px": round(sol.rms_px, 3), "stars": sol.matches}, sort_keys=False))
            print(f"wrote {f}")
    print("restart the dome to use it: sudo systemctl restart skydome-live")
    return 0


if __name__ == "__main__":
    sys.exit(main())
