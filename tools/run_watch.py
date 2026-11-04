#!/usr/bin/env python3
"""The sky watcher (DECISIONS.md D-030): every moving thing the all-sky
camera sees, followed, identified and logged; anything unknown flagged.

    uv run --project pipeline tools/run_watch.py        # systemd/skydome-watch.service

Reads the frames skydome-live leaves in WATCH_DIR (run_live.py passes
--watch-dir), checks them against what should be up there (stars, planets,
the Moon, satellites from var/tle/active.tle, aircraft from the ADS-B
receiver in var/watch.yaml) with skydome_pipeline/skywatch.py, and logs
each object to var/watch/objects.db with a thumbnail and animation in
var/watch/media/. Labels given on the web page teach it: it reloads them as
they change.

var/watch.yaml (this install's own, not in git):

    adsb_url: http://<receiver>/tar1090/data/aircraft.json
    alert_url: https://ntfy.sh/<your-topic>   # optional: a POST per unknown object
    alert_kinds: [unknown]                     # which kinds alert (default unknown)
    alert_priority: high                       # ntfy priority (default high: a banner, not just a list entry)
    page_url: http://skydome.local             # for the alert's link
    keep_days: 30                              # pictures of unlabelled, identified objects
"""

from __future__ import annotations

import argparse
import signal
import sys
import threading
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "pipeline"))
sys.path.insert(0, str(ROOT / "tools"))

import yaml  # noqa: E402

from run_live import HORIZON_FILE, WATCH_DIR  # noqa: E402
from skydome_pipeline.config import calibrated_cameras, load_cameras, load_site  # noqa: E402
from skydome_pipeline.skymodel import SkyModel  # noqa: E402
from skydome_pipeline.skywatch import (Classifier, Watcher, labelled_examples, labels_version, log_track,  # noqa: E402
                                       open_log, read_tap)
from skydome_pipeline.stars import load_bsc, star_directions  # noqa: E402

WATCH_CONFIG = ROOT / "var" / "watch.yaml"
TLE_FILE = ROOT / "var" / "tle" / "active.tle"
DB = ROOT / "var" / "watch" / "objects.db"
MEDIA = ROOT / "var" / "watch" / "media"
STARS_EVERY_S = 60
LABELS_EVERY_S = 15
ALERT_GAP_S = 120


def log(*a) -> None:
    print(*a, file=sys.stderr, flush=True)


def watch_config() -> dict:
    try:
        return yaml.safe_load(WATCH_CONFIG.read_text()) or {}
    except (OSError, yaml.YAMLError):
        return {}


def read_horizon() -> list[float] | None:
    try:
        vals = [float(x) for line in HORIZON_FILE.read_text().splitlines() for x in line.split("#")[0].split()]
        return vals if len(vals) == 360 else None
    except (OSError, ValueError):
        return None


def calibration_stamp() -> float:
    files = [ROOT / "config" / "cameras.yaml", HORIZON_FILE, *sorted((ROOT / "config" / "calibration").glob("*.yaml"))]
    return max((f.stat().st_mtime for f in files if f.exists()), default=0.0)


def alert(cfg: dict, oid: int, rec: dict) -> bool:
    url = cfg.get("alert_url")
    if not url:
        return False
    when = datetime.fromtimestamp(rec["start"], timezone.utc).astimezone().strftime("%H:%M:%S")
    p = rec["path"]
    body = (f"{rec['kind'].capitalize()} object at {when}: {len(p)} frame(s), from {p[0][2]:.0f}° up at "
            f"{p[0][1]:.0f}° to {p[-1][2]:.0f}° up at {p[-1][1]:.0f}°, "
            f"{rec['features']['speed_dps']:.2f}°/s").encode()
    # High priority: ntfy at the default only lists it in the app, without a banner (tested 2026-11-04)
    headers = {"Title": f"SkyDome: {rec['kind']} object", "Tags": "flying_saucer",
               "Priority": str(cfg.get("alert_priority", "high"))}
    if cfg.get("page_url"):
        headers["Click"] = f"{cfg['page_url'].rstrip('/')}/#objects/{oid}"
    try:
        urllib.request.urlopen(urllib.request.Request(url, data=body, headers=headers, method="POST"), timeout=10)
        return True
    except OSError as e:
        log(f"alert not sent: {e}")
        return False


def prune(db, keep_days: float) -> None:
    """Pictures of unlabelled objects that were identified go after keep_days; unknown and labelled ones stay."""
    cutoff = time.time() - keep_days * 86400
    rows = db.execute("SELECT id, thumb, clip FROM objects WHERE start < ? AND label IS NULL AND unknown = 0 "
                      "AND (thumb IS NOT NULL OR clip IS NOT NULL)", (cutoff,)).fetchall()
    for oid, thumb, clip in rows:
        for f in (thumb, clip):
            if f:
                (MEDIA / f).unlink(missing_ok=True)
        db.execute("UPDATE objects SET thumb = NULL, clip = NULL WHERE id = ?", (oid,))
    db.commit()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tap", default=WATCH_DIR, help="where skydome-live leaves the frames")
    args = ap.parse_args()
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda s, f: stop.set())
    site = load_site(ROOT / "config")
    cat = load_bsc(ROOT / "var" / "bsc5.dat")
    db = open_log(DB)
    cfg = watch_config()
    model = SkyModel(site.latitude_deg, site.longitude_deg, site.elevation_m, TLE_FILE, cfg.get("adsb_url"))
    if not cfg.get("adsb_url"):
        log(f"no adsb_url in {WATCH_CONFIG}: aircraft won't be identified")
    name = load_cameras(ROOT / "config").cameras[0].name
    watcher, stamp, size = None, None, None
    labels_ver, next_labels, next_stars, next_prune, last_alert = None, 0.0, 0.0, 0.0, 0.0
    last_seq, logged = None, 0
    log(f"sky watcher: camera {name}, frames from {args.tap}, log {DB}")
    while not stop.is_set():
        f = read_tap(Path(args.tap), name, last_seq)
        if f is None:
            stop.wait(0.1)
            continue
        last_seq = f.seq
        now = time.time()
        # The camera model at the tap's size, rebuilt when the calibration or the ground mask changes
        if watcher is None or calibration_stamp() != stamp or f.image.shape != size:
            stamp, size = calibration_stamp(), f.image.shape
            cam = calibrated_cameras(ROOT / "config")[0]
            cam = cam.scaled(f.image.shape[1] / cam.width)
            watcher = Watcher(cam, model, horizon=read_horizon(),
                              classifier=watcher.classifier if watcher else None)
            next_stars = 0.0
            log(f"camera model: {cam.width}x{cam.height}, {watcher.px_deg * 60:.1f}' per pixel")
        if now >= next_stars:
            next_stars = now + STARS_EVERY_S
            watcher.star_dirs, watcher.star_mags = star_directions(
                cat, site.latitude_deg, site.longitude_deg, datetime.fromtimestamp(f.t, timezone.utc),
                min_alt=0.0, max_mag=watcher.cfg.star_mag)
        if now >= next_labels:
            next_labels = now + LABELS_EVERY_S
            v = labels_version(db)
            if v != labels_ver:
                labels_ver = v
                ex = labelled_examples(db)
                watcher.classifier = Classifier(ex)
                log(f"learned from {len(ex)} labelled object(s)")
            cfg = watch_config()
            model.adsb_url = cfg.get("adsb_url")
        snap = model.snapshot(datetime.fromtimestamp(f.t, timezone.utc), min_alt=-1)
        for tr in watcher.process(f, snap):
            rec = watcher.identify(tr, snap["sun_alt"])
            oid = log_track(db, rec, tr, MEDIA)
            logged += 1
            what = rec["match"] or rec["kind"]
            log(f"#{oid} {rec['kind']} ({rec['source']} {rec['confidence']:.2f}){' ' + what if rec['match'] else ''}"
                f"{' ' + rec['shower'] if rec['shower'] else ''}, {rec['frames']} frame(s)")
            if rec["kind"] in (cfg.get("alert_kinds") or ["unknown"]) and now - last_alert > ALERT_GAP_S:
                if alert(cfg, oid, rec):
                    last_alert = now
                    db.execute("UPDATE objects SET alerted = 1 WHERE id = ?", (oid,))
                    db.commit()
        if now >= next_prune:
            next_prune = now + 3600
            prune(db, float(cfg.get("keep_days", 30)))
    if watcher:
        for tr in watcher.flush():
            log_track(db, watcher.identify(tr, model.snapshot(datetime.now(timezone.utc))["sun_alt"]), tr, MEDIA)
    db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
