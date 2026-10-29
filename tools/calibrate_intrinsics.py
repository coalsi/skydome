#!/usr/bin/env python3
"""Lens (intrinsic) calibration for one camera, with a printed ChArUco board.

Run from the repo root (the board can be made on the Mac; capture and solve
run on the Jetson):

    uv run --project pipeline tools/calibrate_intrinsics.py board --out board.pdf
    uv run --project pipeline tools/calibrate_intrinsics.py capture --camera zenith --count 40
    uv run --project pipeline tools/calibrate_intrinsics.py solve --camera zenith --square-mm 25.0

board    writes a US Letter page (PDF and PNG). Print it at 100% ("actual
         size", not "fit to page"), glue it flat to card or foam board, and
         measure one square with a ruler: that is --square-mm.
capture  saves full-resolution 3840x2160 MJPEG stills from /dev/skycam-<name>
         (or --device), one every --interval seconds, into
         calib/<name>/. Hold the board about 1 m from the lens (the lens is
         focused at infinity) and move it between shots so that, over the
         whole set, it visits the centre, every edge and every corner of the
         frame, tilted different ways.
solve    detects the board, fits both lens models (fisheye and rectilinear),
         prints which fits better and the true field of view, and writes
         config/calibration/<name>.yaml. Settles DECISIONS.md F-005.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "pipeline"))

from skydome_pipeline.calibration import (  # noqa: E402
    BoardSpec, detect, fit_fisheye, fit_pinhole, fov_deg, load_images, max_theta_seen_deg, save_calibration)
from skydome_pipeline.config import load_cameras  # noqa: E402

LETTER_IN = (11.0, 8.5)   # landscape


def cmd_board(args) -> None:
    import numpy as np
    from PIL import Image, ImageDraw, ImageFont

    board = BoardSpec(square_mm=args.square_mm)
    dpi = 300
    px_per_mm = dpi / 25.4
    page = Image.new("L", (round(LETTER_IN[0] * dpi), round(LETTER_IN[1] * dpi)), 255)
    img = Image.fromarray(board.image(px_per_mm))
    page.paste(img, ((page.width - img.width) // 2, (page.height - img.height) // 2))
    try:
        font = ImageFont.load_default(size=34)
    except TypeError:          # Pillow < 10.1
        font = ImageFont.load_default()
    ImageDraw.Draw(page).text(
        (60, page.height - 90), font=font, text=
        f"SkyDome ChArUco {board.squares_x}x{board.squares_y}, {board.dictionary}, squares {board.square_mm} mm. "
        "Print at 100% (actual size). Measure a square and pass it as --square-mm.", fill=0)
    out = Path(args.out)
    page.save(out.with_suffix(".pdf"), resolution=dpi)
    page.save(out.with_suffix(".png"), dpi=(dpi, dpi))
    print(f"Wrote {out.with_suffix('.pdf')} and {out.with_suffix('.png')} "
          f"(board {board.squares_x * board.square_mm:.0f} x {board.squares_y * board.square_mm:.0f} mm)")


def grab(device: str, dest: Path, width: int, height: int) -> bool:
    """One full-res MJPEG frame, saved as-is (no decode). The first frames after
    opening can be mid auto-exposure, so take a few and keep the last."""
    with tempfile.TemporaryDirectory() as tmp:
        cmd = ["gst-launch-1.0", "-q", "v4l2src", f"device={device}", "num-buffers=8", "!",
               f"image/jpeg,width={width},height={height}", "!", "multifilesink", f"location={tmp}/f%02d.jpg"]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        frames = sorted(Path(tmp).glob("f*.jpg"))
        if r.returncode != 0 or not frames:
            sys.stderr.write(r.stderr[-500:])
            return False
        shutil.copy(frames[-1], dest)
        return True


def cmd_capture(args) -> None:
    device = args.device or f"/dev/skycam-{args.camera}"
    if not Path(device).exists():
        sys.exit(f"{device} not found. Run identify_cameras.py + gen_udev_rules.py first, or pass --device /dev/videoN")
    out = ROOT / "calib" / args.camera
    out.mkdir(parents=True, exist_ok=True)
    start = len(list(out.glob("*.jpg")))
    print(f"Capturing {args.count} stills from {device} into {out} every {args.interval:g} s. "
          "Move the board between beeps; cover centre, edges and corners.")
    for i in range(args.count):
        for s in range(int(args.interval), 0, -1):
            print(f"\r  shot {i + 1}/{args.count} in {s} s ", end="", flush=True)
            time.sleep(1)
        dest = out / f"{args.camera}_{start + i:03d}.jpg"
        ok = grab(device, dest, args.width, args.height)
        print(f"\r  shot {i + 1}/{args.count}: {'saved ' + dest.name if ok else 'FAILED'}\a")
    print(f"Done: {len(list(out.glob('*.jpg')))} images in {out}. Next: solve --camera {args.camera}")


def cmd_solve(args) -> None:
    folder = Path(args.images) if args.images else ROOT / "calib" / args.camera
    paths = list(folder.glob("*.jpg")) + list(folder.glob("*.png"))
    if not paths:
        sys.exit(f"no images in {folder}")
    board = BoardSpec(square_mm=args.square_mm)
    print(f"Detecting the board in {len(paths)} image(s) from {folder} ...")
    det = detect(load_images(paths), board)
    print(f"  usable: {det.views}, rejected (board not found or too few corners): {len(det.rejected)}")
    if det.views < 10:
        sys.exit("need at least 10 usable images; capture more, with the board larger in the frame")
    cov = det.coverage()
    print(f"  frame coverage: {cov:.0%} of an 8x5 grid" + ("" if cov >= 0.7 else
          "  <- low: take more shots near the edges and corners"))

    guess = args.guess_hfov or load_cameras(ROOT / "config").lens.hfov_deg
    fe, ph = fit_fisheye(det, guess), fit_pinhole(det, guess)
    print()
    print(f"  {'model':<10}{'RMS px':>8}{'HFOV':>9}{'VFOV':>9}{'DFOV':>9}")
    for f in (fe, ph):
        v = fov_deg(f.lens)
        print(f"  {f.model:<10}{f.rms_px:>8.3f}{v['hfov_deg']:>8.1f}°{v['vfov_deg']:>8.1f}°{v['dfov_deg']:>8.1f}°")
    best, other = (fe, ph) if fe.rms_px <= ph.rms_px else (ph, fe)
    kind = "fisheye-like (equidistant family)" if best is fe else "close to rectilinear"
    print(f"\n  Best fit: {best.model} - the lens is {kind}.")
    print(f"  Data reaches {max_theta_seen_deg(best.lens, det):.0f}° off-axis; "
          f"the image edge is at {fov_deg(best.lens)['hfov_deg'] / 2:.0f}° horizontally.")
    if fe.dropped:
        print(f"  (fisheye fit dropped ill-conditioned views: {', '.join(fe.dropped)})")
    if best.rms_px > 1.0:
        print("  RMS above 1 px: check focus, glare on the board, or that --square-mm is right.")
    out = Path(args.out) if args.out else ROOT / "config" / "calibration" / f"{args.camera}.yaml"
    save_calibration(out, args.camera, best, other, det, board)
    print(f"\nWrote {out}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("board", help="make the printable target")
    b.add_argument("--out", default="charuco_board", help="output path without extension")
    b.add_argument("--square-mm", type=float, default=25.0)
    c = sub.add_parser("capture", help="save full-res stills from one camera")
    c.add_argument("--camera", required=True, choices=["zenith", "north", "east", "south", "west"])
    c.add_argument("--device", help="override /dev/skycam-<camera>, e.g. /dev/video0")
    c.add_argument("--count", type=int, default=40)
    c.add_argument("--interval", type=float, default=4)
    c.add_argument("--width", type=int, default=3840)
    c.add_argument("--height", type=int, default=2160)
    s = sub.add_parser("solve", help="fit the lens and write config/calibration/<camera>.yaml")
    s.add_argument("--camera", required=True, choices=["zenith", "north", "east", "south", "west"])
    s.add_argument("--images", help="folder of stills (default calib/<camera>/)")
    s.add_argument("--square-mm", type=float, required=True, help="measured size of one printed square")
    s.add_argument("--guess-hfov", type=float, help="starting HFOV guess (default: config/cameras.yaml)")
    s.add_argument("--out", help="output YAML (default config/calibration/<camera>.yaml)")
    args = ap.parse_args()
    {"board": cmd_board, "capture": cmd_capture, "solve": cmd_solve}[args.cmd](args)


if __name__ == "__main__":
    main()
