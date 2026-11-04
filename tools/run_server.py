#!/usr/bin/env python3
"""The SkyDome server on port 80 (DECISIONS.md D-020, D-021): the web page,
its API, and ONVIF for NVRs, in one process.

    uv run --project pipeline tools/run_server.py              # port 80 needs the systemd unit
    uv run --project pipeline tools/run_server.py --port 8080  # by hand
    uv run --project pipeline tools/run_server.py --set-password

http://skydome.local shows the live dome with its camera settings, the
Detail/Smooth switch and the rig's health (web/, built into web/dist). API
docs at http://skydome.local/api/docs.

NVRs find it by WS-Discovery (or add http://skydome.local/onvif/device_service
by hand) and log in with the user in var/onvif_credentials.yaml, created with
a random password on the first run. ONVIF offers:

  * the Detail stream (rtsp://<jetson>:8554/dome, 5120² H.264 at 15 fps)
  * a full-resolution JPEG snapshot (skydome-live's hardware encoder)
  * imaging settings for all five cameras together: exposure (auto/manual,
    time, gain), brightness, contrast, saturation, sharpness, backlight
    compensation and white balance (auto/manual)

Camera settings from the page and from ONVIF are the same settings, kept in
var/camera_settings.yaml and re-applied when a camera is replugged.
"""

from __future__ import annotations

import argparse
import getpass
import socket
import subprocess
import sys
import threading
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "pipeline"))
sys.path.insert(0, str(ROOT / "server"))
sys.path.insert(0, str(ROOT / "tools"))

import uvicorn  # noqa: E402
from run_live import (FULL_SNAPSHOT, HORIZON_FILE, MODE_FILE, MODES, RAW_DIR, SNAPSHOT, STATS_FILES,  # noqa: E402
                      STACK_FILE, STRETCH_FILE, ZOOM_FILE)
from skydome_pipeline.config import load_modes, load_site  # noqa: E402
from skydome_server.app import Live, create_app  # noqa: E402
from skydome_server.camera_settings import CameraSettings  # noqa: E402
from skydome_server.net import interfaces  # noqa: E402
from skydome_server.onvif.discovery import Discovery  # noqa: E402
from skydome_server.onvif.http import OnvifEndpoint, SnapshotSource, load_credentials, save_credentials  # noqa: E402
from skydome_server.onvif.services import Identity, Services, Stream  # noqa: E402
from skydome_server.onvif.soap import UsernameTokenAuth  # noqa: E402
from skydome_server.system import System  # noqa: E402

CREDENTIALS = ROOT / "var" / "onvif_credentials.yaml"
CAMERA_SETTINGS = ROOT / "var" / "camera_settings.yaml"
WEB_DIST = ROOT / "web" / "dist"


def log(*args, **kw) -> None:
    kw.setdefault("file", sys.stderr)
    print(*args, **kw, flush=True)


def read(path: str) -> str:
    try:
        return Path(path).read_text().strip("\0\n ")
    except OSError:
        return ""


def identity() -> Identity:
    machine = read("/etc/machine-id") or socket.gethostname()
    try:
        firmware = subprocess.run(["git", "-C", str(ROOT), "describe", "--always", "--dirty"],
                                  capture_output=True, text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        firmware = ""
    site = load_site(ROOT / "config")
    return Identity(uuid=f"urn:uuid:{uuid.uuid5(uuid.NAMESPACE_URL, 'skydome:' + machine)}",
                    hostname=socket.gethostname(), firmware=firmware or "dev",
                    serial=read("/proc/device-tree/serial-number") or machine[:12],
                    location=site.name, timezone=site.timezone)


def set_password() -> int:
    user = input("ONVIF user name [admin]: ").strip() or "admin"
    pw = getpass.getpass("New password: ")
    if not pw or pw != getpass.getpass("Again: "):
        log("passwords empty or different; nothing changed")
        return 1
    save_credentials(CREDENTIALS, user, pw)
    log(f"saved to {CREDENTIALS}; restart the service: sudo systemctl restart skydome-server")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=80)
    ap.add_argument("--snapshot", default=SNAPSHOT, help="skydome-live's --snapshot path")
    ap.add_argument("--no-discovery", action="store_true")
    ap.add_argument("--set-password", action="store_true", help="change the ONVIF login and exit")
    args = ap.parse_args()
    if args.set_password:
        return set_password()

    ident = identity()
    detail = MODES["detail"]
    stream = Stream(detail["size"], detail["fps"], detail["bitrate"], detail["path"])
    users = load_credentials(CREDENTIALS)
    settings = CameraSettings(CAMERA_SETTINGS, log=log)
    services = Services(ident, stream, settings, users, interfaces=interfaces, log=log)
    snapshot = SnapshotSource(Path(args.snapshot))
    live_cfg = Live(MODE_FILE, STATS_FILES, zoom_file=Path(ZOOM_FILE), horizon_file=HORIZON_FILE,
                    edge_deg=load_modes(ROOT / "config").dome.edge_zenith_deg, raw_dir=RAW_DIR,
                    stretch_file=STRETCH_FILE, stack_file=STACK_FILE,
                    extra_files=(ROOT / "config" / "cameras.yaml", ROOT / "config" / "calibration" / "extrinsics.yaml"))
    app = create_app(settings, live_cfg,
                     System(ROOT / "var"), snapshot,
                     onvif=OnvifEndpoint(services, UsernameTokenAuth(users), log=log), web_dir=WEB_DIST,
                     full=SnapshotSource(Path(FULL_SNAPSHOT), timeout=10, fresh=2))
    if not (WEB_DIST / "index.html").exists():
        log(f"{WEB_DIST} not built: the page says so; the API and ONVIF work")

    stop = threading.Event()
    threads = [threading.Thread(target=settings.watch, args=(stop,), daemon=True)]
    if not args.no_discovery:
        port = args.port
        disc = Discovery(ident.uuid, ident.scopes(), lambda ip: f"http://{ip}:{port}/onvif/device_service", log=log)
        threads.append(threading.Thread(target=disc.serve, args=(stop,), daemon=True))
    for t in threads:
        t.start()
    log(f"skydome server: {ident.name} on port {args.port} ({', '.join(i.address for i in interfaces()) or 'no network'}), "
        f"stream rtsp://*:{stream.rtsp_port}/{stream.path}, firmware {ident.firmware}")
    server = uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=args.port, log_level="warning",
                                           access_log=False))   # NVRs poll constantly; errors only
    try:
        server.run()   # until SIGINT / SIGTERM
    except SystemExit:
        pass           # uvicorn exits this way when it can't bind
    finally:
        stop.set()
        for t in threads[1:]:
            t.join(timeout=3)   # discovery says Bye
    if not server.started:
        log(f"can't listen on port {args.port}: below 1024 needs systemd/skydome-server.service "
            "(CAP_NET_BIND_SERVICE); by hand use --port 8080")
        return 1
    return 0

if __name__ == "__main__":
    sys.exit(main())
