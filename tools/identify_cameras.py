#!/usr/bin/env python3
"""Map physical USB ports to camera directions, once (handoff §2.1 #3).

Run ON THE JETSON (headless is fine):

    python3 tools/identify_cameras.py            # snapshot every camera, serve a page on :8090
    python3 tools/identify_cameras.py --assign north=platform-3610000.usb-usb-0:1.2:1.0 ...

The first form grabs one still from every UVC capture node and serves a page
at http://skydome.local:8090 showing each image with its ID_PATH. Point a
camera at something recognisable (or cover it with your hand), refresh, and
note which ID_PATH belongs to which direction.

The second form writes those ID_PATHs into config/cameras.yaml. Then run
tools/gen_udev_rules.py to create the /dev/skycam-<name> links.

Dry-run on the Jetson with no cameras (2026-10-29); untested with cameras.
"""

from __future__ import annotations

import argparse
import html
import http.server
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NAMES = ("zenith", "north", "east", "south", "west")


def capture_nodes() -> list[Path]:
    """UVC devices expose a capture node and a metadata node; keep capture ones."""
    nodes = []
    for dev in sorted(Path("/dev").glob("video*"), key=lambda p: int(re.sub(r"\D", "", p.name) or 0)):
        info = subprocess.run(["v4l2-ctl", "-d", str(dev), "--info"], capture_output=True, text=True).stdout
        # The line after "Device Caps" names this node's own capability
        device_caps = info.split("Device Caps", 1)[-1].splitlines()[1:2]
        if device_caps and "Video Capture" in device_caps[0]:
            nodes.append(dev)
    return nodes


def udev_props(dev: Path) -> dict[str, str]:
    out = subprocess.run(["udevadm", "info", "-q", "property", "-n", str(dev)], capture_output=True, text=True).stdout
    return dict(line.split("=", 1) for line in out.splitlines() if "=" in line)


def snapshot(dev: Path, dest: Path) -> bool:
    """One small frame from the camera. MJPEG straight from the camera needs no
    decode; if it doesn't offer 1280x720 MJPEG, take any MJPEG size, then fall
    back to raw YUYV encoded to JPEG (CPU encode of one frame is fine)."""
    chains = [["image/jpeg,width=1280,height=720"],
              ["image/jpeg"],
              ["video/x-raw,format=YUY2,width=1280,height=720", "!", "jpegenc"],
              ["video/x-raw,format=YUY2", "!", "videoconvert", "!", "jpegenc"]]
    for chain in chains:
        cmd = ["gst-launch-1.0", "-q", "v4l2src", f"device={dev}", "num-buffers=5", "!", *chain,
               "!", "multifilesink", f"location={dest}"]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        except subprocess.TimeoutExpired:
            continue
        if r.returncode == 0 and dest.exists() and dest.stat().st_size > 0:
            return True
    return False


def serve(port: int) -> None:
    work = Path(tempfile.mkdtemp(prefix="skydome-identify-"))
    rows = []
    for dev in capture_nodes():
        props = udev_props(dev)
        img = work / f"{dev.name}.jpg"
        ok = snapshot(dev, img)
        link = next((p.name for p in Path("/dev").glob("skycam-*") if p.resolve() == dev), "")
        rows.append(
            f"<figure><figcaption><b>{dev}</b> {html.escape(link)}<br><code>{html.escape(props.get('ID_PATH', '?'))}"
            f"</code><br>{html.escape(props.get('ID_MODEL', ''))}</figcaption>"
            + (f'<img src="{img.name}">' if ok else "<p>capture failed</p>") + "</figure>")
    (work / "index.html").write_text(
        "<!doctype html><meta charset=utf-8><title>SkyDome cameras</title>"
        "<style>body{font-family:system-ui;background:#111;color:#eee;margin:16px}"
        "figure{display:inline-block;margin:8px;width:min(640px,100%)}img{width:100%}"
        "code{color:#9cf;word-break:break-all}</style>"
        f"<h1>{len(rows)} camera(s)</h1>{''.join(rows)}"
        "<p>Assign with: <code>python3 tools/identify_cameras.py --assign north=&lt;ID_PATH&gt; ...</code></p>")
    handler = lambda *a, **k: http.server.SimpleHTTPRequestHandler(*a, directory=str(work), **k)  # noqa: E731
    print(f"Serving {len(rows)} snapshot(s) on http://0.0.0.0:{port} (Ctrl-C to stop; rerun to refresh)")
    try:
        http.server.ThreadingHTTPServer(("", port), handler).serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        shutil.rmtree(work, ignore_errors=True)


def assign(pairs: list[str], config: Path) -> None:
    import yaml

    mapping = {}
    for pair in pairs:
        name, _, id_path = pair.partition("=")
        if name not in NAMES or not id_path:
            sys.exit(f"bad assignment {pair!r}; use <{'|'.join(NAMES)}>=<ID_PATH>")
        mapping[name] = id_path
    text = config.read_text()
    data = yaml.safe_load(text)
    for cam in data["cameras"]:
        if cam["name"] in mapping:
            # Rewrite just the id_path line so comments in the file survive
            pattern = rf"(- name: {cam['name']}\n\s+id_path: )[^\n]*"
            text, n = re.subn(pattern, lambda m: m.group(1) + mapping[cam["name"]], text)
            if n != 1:
                sys.exit(f"could not find the id_path line for {cam['name']} in {config}")
    config.write_text(text)
    print(f"Updated {config}: " + ", ".join(f"{k} -> {v}" for k, v in mapping.items()))
    print("Next: sudo python3 tools/gen_udev_rules.py --install")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8090)
    ap.add_argument("--assign", nargs="+", metavar="NAME=ID_PATH")
    ap.add_argument("--config", type=Path, default=ROOT / "config" / "cameras.yaml")
    args = ap.parse_args()
    if args.assign:
        assign(args.assign, args.config)
    else:
        serve(args.port)


if __name__ == "__main__":
    main()
