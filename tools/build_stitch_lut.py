#!/usr/bin/env python3
"""Write the GPU stitch tables for the live dome (DECISIONS.md D-016).

    uv run --project pipeline tools/build_stitch_lut.py              # 5120, 2048 and 6656
    uv run --project pipeline tools/build_stitch_lut.py --size 5120

Uses config/cameras.yaml for pointing, config/calibration/ for measured
lenses and pointing, and the dome edge from config/modes.yaml.
Writes var/stitch_<size>.lut. Rerun after calibrating a lens or changing
the camera pointing; tools/run_live.py does it automatically when the
config is newer than the table.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "pipeline"))

from skydome_pipeline.config import calibrated_cameras, load_modes  # noqa: E402
from skydome_pipeline.gpu_lut import build_grid_lut  # noqa: E402
from skydome_pipeline.projection import AllSkyProjection  # noqa: E402


def lut_path(size: int) -> Path:
    return ROOT / "var" / f"stitch_{size}.lut"


def config_inputs() -> list[Path]:
    """Files the tables depend on, for staleness checks."""
    cfg = ROOT / "config"
    return [cfg / "cameras.yaml", cfg / "modes.yaml", *sorted((cfg / "calibration").glob("*.yaml"))]


def is_stale(size: int) -> bool:
    out = lut_path(size)
    if not out.exists():
        return True
    built = out.stat().st_mtime
    return any(p.stat().st_mtime > built for p in config_inputs() if p.exists())


def build(size: int, step: int = 8) -> Path:
    cams = calibrated_cameras(ROOT / "config")
    t0 = time.monotonic()
    edge = load_modes(ROOT / "config").dome.edge_zenith_deg
    glut = build_grid_lut(AllSkyProjection(size=size, edge_zenith_deg=edge), cams, step=step)
    out = lut_path(size)
    out.parent.mkdir(exist_ok=True)
    glut.save(out)
    print(f"Wrote {out.relative_to(ROOT)} (edge {edge:g}° from zenith, {out.stat().st_size / 1e6:.1f} MB, "
          f"{glut.nodes}x{glut.nodes} nodes, {time.monotonic() - t0:.1f} s)")
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--size", type=int, action="append", help="dome size in pixels (repeatable; default 5120, 2048 and 6656)")
    ap.add_argument("--step", type=int, default=8, help="grid step in output pixels (D-005)")
    args = ap.parse_args()
    for size in args.size or [5120, 2048, 6656]:
        build(size, args.step)


if __name__ == "__main__":
    main()
