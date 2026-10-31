#!/usr/bin/env python3
"""Measure how the cameras are really pointed, from what neighbours see in
common, and write config/calibration/extrinsics.yaml (handoff §5.3 step 4).

    # stop the live dome first (the cameras can only be opened once)
    uv run --project pipeline tools/align_from_overlap.py --frame centered \\
        --camera zenith=/dev/video2 --camera south=/dev/video0 --camera west=/dev/video6

    uv run --project pipeline tools/align_from_overlap.py           # the rig: /dev/skycam-*
    uv run --project pipeline tools/align_from_overlap.py --images zenith=a.jpg north=b.jpg ...

Grabs one full-resolution still per camera (saved in calib/overlap/),
matches SIFT features between every pair, and solves all rotations
together (pipeline/skydome_pipeline/overlap_align.py). Until a lens has
been calibrated with the board, it also fits the lens's true field of view
(saved as lens_hfov_deg; the stitch uses it). Daylight with
detail in the overlaps works best; plain blue sky has nothing to match.

--frame nominal   (default) the solved set is turned to best fit the
                  mount's nominal pointing (config/cameras.yaml): for the rig
--frame centered  the cameras' mean view direction goes to the zenith: for
                  cameras loose on a bench

Absolute north and level come from the stars later (Phase 2); this pins
down how the cameras sit relative to each other. Rerun tools/run_live.py
afterwards: it rebuilds the stitch tables because extrinsics.yaml changed.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "pipeline"))
sys.path.insert(0, str(ROOT / "tools"))

from skydome_pipeline.config import calibrated_cameras, load_cameras  # noqa: E402
from skydome_pipeline.overlap_align import (  # noqa: E402
    PixelPairs, align_pixels, fit_shared_hfov, frame_centered, frame_to_nominal)

WORK_WIDTH = 1920   # features are found on a half-size copy; plenty for 4K frames


def grab_stills(devices: dict[str, str]) -> dict[str, Path]:
    from calibrate_intrinsics import grab

    out = ROOT / "calib" / "overlap"
    out.mkdir(parents=True, exist_ok=True)
    paths = {}
    for name, dev in devices.items():
        p = out / f"{name}.jpg"
        if grab(dev, p, 3840, 2160):
            paths[name] = p
            print(f"  {name}: grabbed from {dev}")
        else:
            print(f"  {name}: could not grab from {dev} (busy? stop the live dome first)")
    return paths


def features(path: Path):
    import cv2

    img = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
    if img is None:
        sys.exit(f"{path}: not a readable image")
    s = WORK_WIDTH / img.shape[1]
    small = cv2.resize(img, None, fx=s, fy=s, interpolation=cv2.INTER_AREA)
    kp, desc = cv2.SIFT_create(nfeatures=8000).detectAndCompute(small, None)
    # Back to full-resolution pixel coordinates (pixel centres at integers)
    pts = np.array([k.pt for k in kp], np.float64).reshape(-1, 2)
    pts = (pts + 0.5) / s - 0.5
    return pts, desc, img.shape[1], img.shape[0]


def match(desc_a, desc_b, ratio: float = 0.75) -> np.ndarray:
    import cv2

    if desc_a is None or desc_b is None or len(desc_a) < 2 or len(desc_b) < 2:
        return np.zeros((0, 2), int)
    knn = cv2.BFMatcher(cv2.NORM_L2).knnMatch(desc_a, desc_b, k=2)
    good = [(m.queryIdx, m.trainIdx) for m, n in (p for p in knn if len(p) == 2) if m.distance < ratio * n.distance]
    return np.array(good, int).reshape(-1, 2)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--camera", action="append", default=[], metavar="NAME=DEVICE")
    ap.add_argument("--images", nargs="+", metavar="NAME=FILE", help="use these stills instead of grabbing")
    ap.add_argument("--frame", choices=["nominal", "centered"], default="nominal")
    ap.add_argument("--min-inliers", type=int, default=20)
    ap.add_argument("--thresh-deg", type=float, default=1.0, help="RANSAC inlier threshold")
    ap.add_argument("--no-fit-hfov", dest="fit_hfov", action="store_false",
                    help="keep the nominal lens instead of fitting its field of view to the overlaps "
                         "(never fitted once calibrate_intrinsics.py has run)")
    ap.add_argument("--out", default=str(ROOT / "config" / "calibration" / "extrinsics.yaml"))
    args = ap.parse_args()

    names = [c.name for c in load_cameras(ROOT / "config").cameras]
    if args.images:
        stills = {k: Path(v) for k, v in (s.split("=", 1) for s in args.images)}
    else:
        devices = dict(c.split("=", 1) for c in args.camera) or {
            n: f"/dev/skycam-{n}" for n in names if Path(f"/dev/skycam-{n}").exists()}
        if not devices:
            sys.exit("no cameras: pass --camera NAME=DEVICE or set up /dev/skycam-* first")
        print("Grabbing stills ...")
        stills = grab_stills(devices)
    unknown = set(stills) - set(names)
    if unknown:
        sys.exit(f"unknown camera name(s): {', '.join(sorted(unknown))}")
    order = [n for n in names if n in stills]
    if len(order) < 2:
        sys.exit("need at least two cameras")

    cfg = load_cameras(ROOT / "config")
    cams = {c.name: c for c in calibrated_cameras(ROOT / "config")}
    nominal = {c.name: c for c in cfg.nominal_cameras()}
    charuco = {n for n in order if (ROOT / "config" / "calibration" / f"{n}.yaml").exists()}
    fit = args.fit_hfov and not charuco
    print("Finding features ...")
    feats = {}
    for n in order:
        pts, desc, w, h = features(stills[n])
        lens = cams[n].lens
        if (w, h) != (lens.width, lens.height):
            pts = (pts + 0.5) * [lens.width / w, lens.height / h] - 0.5
        feats[n] = (pts, desc)
        print(f"  {n}: {len(pts)} features")

    print("Matching pairs ...")
    pixel_pairs = []
    for a in range(len(order)):
        for b in range(a + 1, len(order)):
            pa, da = feats[order[a]]
            pb, db = feats[order[b]]
            m = match(da, db)
            pixel_pairs.append(PixelPairs(a, b, pa[m[:, 0]], pb[m[:, 1]]))

    if fit:
        # No board calibration yet: find the field of view that makes the
        # overlaps agree, for the projection in cameras.yaml (F-005)
        lens_cls = type(nominal[order[0]].lens)
        sw, sh = cfg.sensor.width, cfg.sensor.height
        hfov = fit_shared_hfov(pixel_pairs, lambda h: lens_cls.from_hfov(sw, sh, h), len(order),
                               cfg.lens.hfov_deg - 25, cfg.lens.hfov_deg + 15, args.thresh_deg, args.min_inliers)
        lenses = [lens_cls.from_hfov(sw, sh, hfov)] * len(order)
        print(f"  best-fitting lens: {cfg.lens.projection}, {hfov:.1f}° wide (nominal {cfg.lens.hfov_deg:g}°)")
    else:
        hfov = None
        lenses = [cams[n].lens for n in order]
    al, pairs = align_pixels(pixel_pairs, lenses, len(order), args.thresh_deg, args.min_inliers)
    for pp, pm in zip(pixel_pairs, pairs):
        flag = "" if pm.n_inliers >= args.min_inliers else "  (too few, not used)"
        print(f"  {order[pp.i]:>6} - {order[pp.j]:<6} {len(pp.pa):5d} matches, {pm.n_inliers:5d} agree{flag}")

    if args.frame == "nominal":
        rots = frame_to_nominal(al.rotations, [nominal[n].rotation for n in order])
    else:
        rots = frame_centered(al.rotations)

    print("\nResult:")
    doc_cams = {}
    for k, n in enumerate(order):
        r = rots[k]
        if r is None:
            print(f"  {n:>6}: no overlap with the others; keeps its previous pointing")
            continue
        fwd = r[:, 2]
        az = np.degrees(np.arctan2(fwd[0], fwd[1])) % 360
        alt = np.degrees(np.arcsin(np.clip(fwd[2], -1, 1)))
        print(f"  {n:>6}: looks at az {az:5.1f}°, alt {alt:5.1f}°; "
              f"{al.matches[k]} matches, RMS {al.residual_deg[k] * 60:.1f} arcmin")
        doc_cams[n] = {"rotation": [[round(float(x), 9) for x in row] for row in r],
                       "az_deg": round(float(az), 2), "alt_deg": round(float(alt), 2),
                       "matches": int(al.matches[k]), "rms_arcmin": round(al.residual_deg[k] * 60, 2)}
    if not doc_cams:
        sys.exit("nothing solved; more overlap or more detail in the overlaps needed")

    out = Path(args.out)
    prev = yaml.safe_load(out.read_text()) if out.exists() else {}
    merged = dict((prev or {}).get("cameras") or {})
    merged.update(doc_cams)
    doc = {"method": "overlap features", "frame": args.frame,
           "date_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    if hfov is not None:
        # Used by the stitch for lenses without a board calibration
        doc["lens_hfov_deg"] = round(float(hfov), 2)
    doc["cameras"] = merged
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("# Written by tools/align_from_overlap.py: world-from-camera rotations\n"
                   "# (columns = camera x, y, z in east-north-up; DECISIONS.md D-003)\n"
                   + yaml.safe_dump(doc, sort_keys=False))
    print(f"\nWrote {out}")


if __name__ == "__main__":
    main()
