#!/usr/bin/env python3
"""How much of the sky the 5-camera rig covers, for different side-camera
elevations, lens projections and sensor orientations.

Run from the repo root:
    uv run --project pipeline tools/coverage_report.py [--png docs/coverage.png]

Prints a table and optionally draws a dome map coloured by how many cameras
see each point (black = gap).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pipeline"))

from skydome_pipeline.camera import Camera, FisheyeLens, PinholeLens  # noqa: E402
from skydome_pipeline.geometry import altaz_to_vec, camera_rotation  # noqa: E402
from skydome_pipeline.projection import AllSkyProjection  # noqa: E402

LENSES = {"equidistant": FisheyeLens, "rectilinear": PinholeLens}


def make_rig(projection: str, side_alt: float, portrait: bool, hfov: float = 120.0) -> list[Camera]:
    lens = LENSES[projection].from_hfov(3840, 2160, hfov)
    roll = 90.0 if portrait else 0.0
    cams = [Camera("zenith", lens, camera_rotation(0, 90))]
    for name, az in [("north", 0), ("east", 90), ("south", 180), ("west", 270)]:
        cams.append(Camera(name, lens, camera_rotation(az, side_alt, roll)))
    return cams


def counts_on_grid(cams: list[Camera], az: np.ndarray, alt: np.ndarray) -> np.ndarray:
    dirs = altaz_to_vec(az, alt)
    return sum(cam.world_to_pixel(dirs)[2].astype(np.int32) for cam in cams)


def analyse(cams: list[Camera]) -> dict:
    az = np.arange(0, 360, 0.5)
    alt = np.arange(0, 90.01, 0.25)
    A, L = np.meshgrid(az, alt)
    n = counts_on_grid(cams, A, L)
    ring_full = (n > 0).all(axis=1)
    # lowest altitude from which every ring up to the zenith is fully covered
    full_from = None
    for i in range(len(alt) - 1, -1, -1):
        if not ring_full[i]:
            break
        full_from = alt[i]
    area = np.cos(np.radians(L))  # solid-angle weight per sample
    seen = (area * (n > 0)).sum() / area.sum()
    double = (area * (n > 1)).sum() / area.sum()
    # narrowest overlap between two neighbouring side cameras, and between the
    # zenith camera and a side camera along the diagonal (worst case)
    def side_overlap(at_alt):
        row = counts_on_grid(cams, az, np.full_like(az, at_alt))
        band = (row >= 2) & (np.abs(((az - 45) + 45) % 90 - 45) < 30)  # around az 45, 135, ...
        return band.sum() * 0.5 * np.cos(np.radians(at_alt)) / 4
    col = counts_on_grid(cams, np.full_like(alt, 45.0), alt)
    zen_overlap = ((col >= 2) & (alt > 30)).sum() * 0.25
    return {"full_from": full_from, "seen": seen, "double": double,
            "side10": side_overlap(10.0), "side30": side_overlap(30.0), "zen45": zen_overlap}


def draw_map(cams: list[Camera], path: Path, size: int = 900) -> None:
    from PIL import Image, ImageDraw

    dome = AllSkyProjection(size=size)
    dirs, inside = dome.grid()
    n = sum(cam.world_to_pixel(dirs)[2].astype(np.int32) for cam in cams)
    palette = np.array([[20, 20, 24], [52, 94, 160], [96, 170, 120], [230, 190, 80], [220, 90, 70], [255, 255, 255]],
                       dtype=np.uint8)
    rgb = palette[np.clip(n, 0, 5)]
    rgb[n == 0] = [0, 0, 0]
    rgb[~inside] = [255, 255, 255]
    img = Image.fromarray(rgb)
    d = ImageDraw.Draw(img)
    c = size / 2
    for alt in (0, 30, 60):
        r = (90 - alt) / 90 * c
        d.ellipse([c - r, c - r, c + r, c + r], outline=(200, 200, 200))
    for label, az in [("N", 0), ("E", 90), ("S", 180), ("W", 270)]:
        u, v = dome.world_to_pixel(altaz_to_vec(az, 3))
        d.text((float(u) - 4, float(v) - 6), label, fill=(255, 255, 255))
    img.save(path)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--hfov", type=float, default=120.0)
    ap.add_argument("--png", type=Path, help="write landscape coverage maps, one per lens model")
    ap.add_argument("--side-alt", type=float, default=35.0,
                    help="side-camera elevation for the --png maps (as built: 35, DECISIONS.md D-009)")
    args = ap.parse_args()

    print(f"5-camera rig, {args.hfov:.0f} deg HFOV, 16:9 sensor")
    print(f"{'lens':12} {'sensor':9} {'side alt':>8} {'gap-free':>12} {'sky seen':>9} {'overlap':>8} "
          f"{'side seam@10':>13} {'side seam@30':>13} {'zenith seam':>12}")
    for projection in LENSES:
        for portrait in (False, True):
            for side_alt in (25, 30, 35, 40, 45, 50):
                r = analyse(make_rig(projection, side_alt, portrait, args.hfov))
                full = f"down to {r['full_from']:4.1f}" if r["full_from"] is not None else "never"
                print(f"{projection:12} {'portrait' if portrait else 'landscape':9} {side_alt:>8} {full:>12} "
                      f"{r['seen']:>8.1%} {r['double']:>8.1%} {r['side10']:>12.1f}° {r['side30']:>12.1f}° "
                      f"{r['zen45']:>11.1f}°")
        print()
    if args.png:
        args.png.parent.mkdir(parents=True, exist_ok=True)
        for projection in LENSES:
            out = args.png.with_name(f"{args.png.stem}_{projection}{args.png.suffix}")
            draw_map(make_rig(projection, args.side_alt, False, args.hfov), out)
            print(f"wrote {out}")


if __name__ == "__main__":
    main()
