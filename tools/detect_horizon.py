#!/usr/bin/env python3
"""Find the horizon line (houses, trees, fences) from a daylight dome image
and write the ground mask (DECISIONS.md D-024).

    uv run --project pipeline tools/detect_horizon.py dome.jpg --preview mask.jpg
    curl -o dome.jpg http://skydome.local/api/snapshot.jpg    # a daylight dome to start from

For each degree of azimuth it walks down from 45° altitude and stops where
the picture turns from sky to ground: the first place where at least 2° in a
row is ground. Ground is much darker than the open sky above it in that
direction (houses, fences), or not blue and busy (trees: on the rig at
sunset, canopy measured blueness −0.1 to −0.7 and texture 21–27, sky +0.2
to +0.4 and 2–5, wires +0.2 and 13). Thin things (wires, poles) don't stop
it; the line is smoothed across neighbouring azimuths and given a small
margin.

Writes var/horizon.off.txt (the mask, switched off) unless --on is given,
which writes var/horizon.txt (in use). Switch it on and off from the web
page; skydome-live applies changes within a second. Edit the file by hand
to fix a direction: one altitude per line, line 1 = north, then clockwise
(east = line 91).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageFilter

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "pipeline"))

from skydome_pipeline.config import load_modes  # noqa: E402
from skydome_pipeline.geometry import altaz_to_vec  # noqa: E402
from skydome_pipeline.projection import AllSkyProjection  # noqa: E402

STEP_DEG = 0.25        # sampling along each azimuth
REF_ALT = (50.0, 70.0)  # open sky for the brightness reference
START_ALT = 45.0       # nothing above this is ground
DARK = 0.55            # darker than this share of the open sky = not sky
TREE_BLUE, TREE_TEXTURE = -0.05, 10.0   # less blue and busier than this = foliage
NOT_SKY_BLUE = -0.25   # this far from blue isn't sky either...
SHADE = 0.8            # ...but only when darker than this share of the open sky: lit clouds and
                       # a sunset glow are yellow and can be busy, never darker (canopy measured 0.5-0.75)
RUN_SHARE = 0.6        # of the 2°: bare branches leave sky between them
RUN_DEG = 2.0          # this much ground in a row ends the sky
MARGIN_DEG = 0.5


def horizon_from_image(img: np.ndarray, edge_deg: float, north_deg: float = 0.0) -> np.ndarray:
    """img: H x W x 3 RGB dome (north north_deg from the top, east to its left). Returns 360 altitudes."""
    size = img.shape[0]
    proj = AllSkyProjection(size=size, edge_zenith_deg=edge_deg, north_deg=north_deg)
    rgb = img.astype(np.float32)
    luma = rgb @ np.array([0.299, 0.587, 0.114], np.float32)
    blue = (rgb[..., 2] - rgb[..., 0]) / (luma + 8)   # >0 blue sky, <0 green/brown
    edges = Image.fromarray(img).convert("L").filter(ImageFilter.FIND_EDGES)
    edges = Image.fromarray(np.clip(np.asarray(edges, np.float32) * 4, 0, 255).astype(np.uint8))
    texture = np.asarray(edges.filter(ImageFilter.BoxBlur(6)), np.float32) / 4
    lowest = 90.0 - edge_deg
    alts = np.arange(START_ALT, lowest - 1e-6, -STEP_DEG)
    run = int(round(RUN_DEG / STEP_DEG))
    out = np.full(360, lowest)
    for az in range(360):
        a = az + 0.5
        u, v = proj.world_to_pixel(altaz_to_vec(np.full_like(alts, a), alts))
        ui = np.clip(np.round(u).astype(int), 0, size - 1)
        vi = np.clip(np.round(v).astype(int), 0, size - 1)
        y = luma[vi, ui]
        ru, rv = proj.world_to_pixel(altaz_to_vec(np.full(40, a), np.linspace(*REF_ALT, 40)))
        ref = np.median(luma[np.clip(np.round(rv).astype(int), 0, size - 1),
                             np.clip(np.round(ru).astype(int), 0, size - 1)])
        b, t = blue[vi, ui], texture[vi, ui]
        shade = y < SHADE * ref
        ground = (y < DARK * ref) | (shade & (((b < TREE_BLUE) & (t > TREE_TEXTURE)) | (b < NOT_SKY_BLUE)))
        # First index where `run` samples in a row are (mostly) ground
        counts = np.convolve(ground.astype(int), np.ones(run, int), "valid")
        hit = np.flatnonzero(counts >= RUN_SHARE * run)
        if hit.size:
            out[az] = alts[hit[0]]
    # Smooth across azimuth: a median drops one-degree spikes (a pole, a gap
    # in a tree), then the higher of each neighbour pair covers the edges
    pad = np.concatenate([out[-3:], out, out[:3]])
    med = np.array([np.median(pad[i:i + 7]) for i in range(360)])
    pad = np.concatenate([med[-1:], med, med[:1]])
    return np.maximum.reduce([pad[:-2], pad[1:-1], pad[2:]]) + MARGIN_DEG


def preview(img: np.ndarray, horizon: np.ndarray, edge_deg: float, north_deg: float = 0.0) -> Image.Image:
    """The image with the masked ground tinted red."""
    size = img.shape[0]
    proj = AllSkyProjection(size=size, edge_zenith_deg=edge_deg, north_deg=north_deg)
    idx = np.arange(size, dtype=np.float64)
    u, v = np.meshgrid(idx, idx)
    half = size / 2
    dx, dy = (u + 0.5 - half) / half, (v + 0.5 - half) / half
    alt = 90.0 - np.hypot(dx, dy) * edge_deg
    az = (np.degrees(np.arctan2(-dx, -dy)) - north_deg) % 360
    hidden = (alt < horizon[np.minimum(az.astype(int), 359)]) & (np.hypot(dx, dy) <= 1)
    out = img.astype(np.float32)
    out[hidden] = out[hidden] * 0.35 + np.array([160, 0, 0], np.float32)
    return Image.fromarray(np.clip(out, 0, 255).astype(np.uint8))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("image", help="daylight dome image (the live dome's snapshot)")
    ap.add_argument("--edge", type=float, help="zenith angle at the dome edge (default: config/modes.yaml)")
    ap.add_argument("--preview", help="also write the image with the mask tinted red")
    ap.add_argument("--on", action="store_true", help="write var/horizon.txt (in use) instead of horizon.off.txt")
    ap.add_argument("--out", help="write the mask here instead")
    args = ap.parse_args()

    edge = args.edge or load_modes(ROOT / "config").dome.edge_zenith_deg
    img = np.asarray(Image.open(args.image).convert("RGB"))
    if img.shape[0] != img.shape[1]:
        sys.exit("expected a square dome image")
    horizon = horizon_from_image(img, edge)
    out = Path(args.out) if args.out else ROOT / "var" / ("horizon.txt" if args.on else "horizon.off.txt")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("# Ground mask (tools/detect_horizon.py): horizon altitude in degrees for each\n"
                   "# degree of azimuth, line 1 = north, then clockwise (east = line 91)\n"
                   + "".join(f"{a:.1f}\n" for a in horizon))
    print(f"wrote {out}: horizon {horizon.min():.1f}° to {horizon.max():.1f}° "
          f"(N {horizon[0]:.1f}, E {horizon[90]:.1f}, S {horizon[180]:.1f}, W {horizon[270]:.1f})")
    if args.preview:
        small = 1600
        im = Image.fromarray(img).resize((small, small), Image.BILINEAR) if img.shape[0] > small else Image.fromarray(img)
        preview(np.asarray(im), horizon, edge).save(args.preview, quality=88)
        print(f"preview: {args.preview}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
