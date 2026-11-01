#!/usr/bin/env python3
"""Start the live dome (Phase 1): cameras -> GPU stitch -> H.264 -> mediamtx.

    uv run --project pipeline tools/run_live.py                 # Detail, 4096² × 15
    uv run --project pipeline tools/run_live.py --mode smooth   # 2048² × 30
    uv run --project pipeline tools/run_live.py --camera north=/dev/video2 ...

Watch it at http://skydome.local:8889/dome (or /dome-smooth); RTSP at
rtsp://skydome.local:8554/dome. Needs mediamtx running
(systemd/mediamtx.service) and the native build:

    cmake -S pipeline/native -B pipeline/native/build && cmake --build pipeline/native/build -j

Cameras are found as /dev/skycam-<name> (udev rules from
tools/gen_udev_rules.py), else by the id_path in config/cameras.yaml, else
from --camera NAME=DEVICE. Missing cameras are left out; skydome-live picks
them up when they appear. Rebuilds var/stitch_<size>.lut when the camera
config or a lens calibration is newer. Stills for ONVIF's GetSnapshotUri
go to --snapshot when asked for (tools/run_onvif.py). Runs jetson_clocks
while it's up (D-014) if sudo allows it without a password, and restores
the clocks after.
"""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "pipeline"))
sys.path.insert(0, str(ROOT / "tools"))

from build_stitch_lut import build, is_stale, lut_path  # noqa: E402
from skydome_pipeline.config import load_cameras  # noqa: E402

BINARY = ROOT / "pipeline" / "native" / "build" / "skydome-live"
CLOCKS_STORE = Path("/var/tmp/skydome-clocks.conf")
SNAPSHOT = "/tmp/skydome-snapshot.jpg"   # tools/run_onvif.py serves it

# D-014: Detail is the default; Smooth trades resolution for frame rate
MODES = {
    "detail": {"size": 4096, "fps": 15, "capture": "3840x2160@15", "path": "dome", "bitrate": 20000},
    "smooth": {"size": 2048, "fps": 30, "capture": "3840x2160@30", "path": "dome-smooth", "bitrate": 12000},
}


def find_devices(overrides: dict[str, str]) -> dict[str, str]:
    cfg = load_cameras(ROOT / "config")
    found = {}
    for cam in cfg.cameras:
        if cam.name in overrides:
            found[cam.name] = overrides[cam.name]
            continue
        link = Path(f"/dev/skycam-{cam.name}")
        by_path = Path(f"/dev/v4l/by-path/{cam.id_path}-video-index0") if cam.id_path else None
        if link.exists():
            found[cam.name] = str(link)
        elif by_path and by_path.exists():
            found[cam.name] = str(by_path)
        else:
            print(f"  {cam.name}: no device (no /dev/skycam-{cam.name}, no id_path match); left out")
    return found


def sudo(*cmd: str) -> bool:
    return subprocess.run(["sudo", "-n", *cmd], capture_output=True).returncode == 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mode", choices=MODES, default="detail")
    ap.add_argument("--camera", action="append", default=[], metavar="NAME=DEVICE",
                    help="use this device for a camera (repeatable)")
    ap.add_argument("--bitrate", type=int, help="H.264 kbps (default by mode)")
    ap.add_argument("--no-clocks", action="store_true", help="don't run jetson_clocks")
    ap.add_argument("--stats-file", default="/tmp/skydome-live.json")
    ap.add_argument("--snapshot", default=SNAPSHOT, help="JPEG still, written when PATH.request appears")
    args, extra = ap.parse_known_args()

    if not BINARY.exists():
        sys.exit(f"{BINARY} not built; see the build line in this script's help")
    mode = MODES[args.mode]
    overrides = dict(c.split("=", 1) for c in args.camera)
    devices = find_devices(overrides)
    if not devices:
        sys.exit("no cameras found; run identify_cameras.py + gen_udev_rules.py, or pass --camera NAME=DEVICE")

    lut = lut_path(mode["size"])
    if is_stale(mode["size"]):
        build(mode["size"])

    cmd = [str(BINARY), "--lut", str(lut), "--fps", str(mode["fps"]), "--capture", mode["capture"],
           "--bitrate", str(args.bitrate or mode["bitrate"]), "--stats-file", args.stats_file,
           "--snapshot", args.snapshot,
           "--sink", f"rtspclientsink location=rtsp://127.0.0.1:8554/{mode['path']} protocols=tcp latency=0"]
    for name, dev in devices.items():
        cmd += ["--camera", f"{name}={dev}"]
    cmd += extra

    clocks = not args.no_clocks and sudo("jetson_clocks", "--store", str(CLOCKS_STORE)) and sudo("jetson_clocks")
    if not args.no_clocks and not clocks:
        print("  jetson_clocks not available without a password; running at normal clocks")
    print(f"{args.mode}: {mode['size']}² × {mode['fps']} fps -> rtsp://127.0.0.1:8554/{mode['path']}, "
          f"cameras: {', '.join(f'{k}={v}' for k, v in devices.items())}", flush=True)
    proc = subprocess.Popen(cmd)
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda s, f: proc.send_signal(signal.SIGTERM))
    try:
        return proc.wait()
    finally:
        if clocks:
            sudo("jetson_clocks", "--restore", str(CLOCKS_STORE))


if __name__ == "__main__":
    sys.exit(main())
