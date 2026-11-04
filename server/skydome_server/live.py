"""The live dome as the server sees it: which mode it should run, and how it's doing.

The web page picks the mode; tools/run_live.py follows it. They share
var/live_mode.yaml:

    mode: smooth
    since: '2026-11-03T23:59:00Z'

Smooth (2048² × 30) reverts to Detail (5120² × 15) after SMOOTH_MINUTES
(D-015), so the NVR's stream comes back by itself. No file means Detail.

skydome-live writes its stats as JSON once a second (--stats-file); a file
that hasn't changed for STALE_S means it isn't running.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

MODES = ("detail", "smooth")
SMOOTH_MINUTES = 30
STALE_S = 5.0


def _now() -> datetime:
    return datetime.now(timezone.utc)


def read_mode(path: Path, now: datetime | None = None) -> tuple[str, datetime | None]:
    """(mode to run, when Smooth ends or None)."""
    now = now or _now()
    try:
        data = yaml.safe_load(Path(path).read_text()) or {}
    except (OSError, yaml.YAMLError):
        return "detail", None
    if data.get("mode") != "smooth":
        return "detail", None
    try:
        since = datetime.fromisoformat(str(data["since"]).replace("Z", "+00:00"))
    except (KeyError, ValueError):
        return "detail", None
    until = since + timedelta(minutes=SMOOTH_MINUTES)
    return ("smooth", until) if now < until else ("detail", None)


def write_mode(path: Path, mode: str, now: datetime | None = None) -> None:
    if mode not in MODES:
        raise ValueError(f"mode must be one of {', '.join(MODES)}")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    since = (now or _now()).strftime("%Y-%m-%dT%H:%M:%SZ")
    tmp = path.with_suffix(".tmp")
    tmp.write_text("# Live dome mode, set from the web page (skydome_server/live.py)\n"
                   + yaml.safe_dump({"mode": mode, "since": since}))
    tmp.replace(path)


def read_stats(candidates: list[Path]) -> dict | None:
    """skydome-live's latest stats from whichever file it's writing, or None
    if it isn't running. The service writes /run/skydome/live.json; by hand
    it's /tmp/skydome-live.json."""
    best = None
    for p in candidates:
        try:
            mtime = p.stat().st_mtime
        except OSError:
            continue
        if best is None or mtime > best[0]:
            best = (mtime, p)
    if best is None or time.time() - best[0] > STALE_S:
        return None
    try:
        return json.loads(best[1].read_text())
    except (OSError, ValueError):
        return None   # caught mid-write; the next poll gets it
