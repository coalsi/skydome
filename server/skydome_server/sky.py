"""The sky for the web page (DECISIONS.md D-028): where the named stars are
right now, and calibrating the rig from them.

Stars: the Yale Bright Star Catalogue (var/bsc5.dat, the same file the
alignment uses) for the site and the time, with names (star_names), for
the page to label and to name on hover. The dome is drawn in true
east-north-up once the rig is aligned, so the page places them from their
azimuth and altitude alone.

Calibration: tools/align_from_stars.py --save in a subprocess (minutes of
CPU), its progress in var/calibration.json. Automatic: once it's dark (the
Sun 12° down) after every start of the server, then once a night; if the
stars can't be matched (cloud) it tries again every 20 minutes until dawn.
Off with var/calibration.auto containing "off".

Night and Day (the ZWO camera, D-029): with apply_preset set and a
preset_file that doesn't say "off", Night goes on once the Sun is 8° down
and Day once it's back above 4° down (the gap keeps it from flapping), and
whatever fits goes on at start-up. Picked by hand, a preset stays until the
next dusk or dawn.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import numpy as np

from skydome_pipeline.star_names import describe
from skydome_pipeline.stars import Catalogue, apparent_altaz, load_bsc, sun_altaz

DARK_SUN_ALT = -12.0      # automatic calibration: nautical twilight over
BUTTON_SUN_ALT = -6.0     # by hand: the bright stars are out
RETRY_S = 20 * 60         # cloudy: try again
NIGHTLY_S = 12 * 3600     # once a night
CHECK_S = 60
NIGHT_PRESET_SUN_ALT = -8.0   # Night below this
DAY_PRESET_SUN_ALT = -4.0     # Day above this


def _utc(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class Sky:
    catalogue: Path
    lat: float
    lon: float
    status_file: Path
    auto_file: Path
    command: list[str]                       # the alignment, ending in --save --status <status_file>
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(timezone.utc))
    preset_file: Path | None = None                     # Night/Day by the Sun unless this says "off"
    apply_preset: Callable[[str], object] | None = field(default=None, repr=False)
    _preset: str | None = field(default=None, repr=False)   # the last one switched to
    _cat: Catalogue | None = field(default=None, repr=False)
    _labels: list | None = field(default=None, repr=False)
    _cache: tuple = field(default=(0.0, None, None), repr=False)
    _proc: subprocess.Popen | None = field(default=None, repr=False)
    _started: float = field(default_factory=time.time, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    # ---- stars ---------------------------------------------------------------------

    def _load(self) -> Catalogue | None:
        if self._cat is None and self.catalogue.exists():
            self._cat = load_bsc(self.catalogue)
            self._labels = [describe(f, int(h)) for f, h in zip(self._cat.names, self._cat.hr)]
        return self._cat

    def stars(self, max_mag: float = 5.5) -> dict:
        """Stars above the horizon brighter than max_mag, recomputed every 10 s:
        {"utc", "sun_alt", "stars": [[name, designation, constellation, mag, az, alt], ...]}."""
        now = time.time()
        with self._lock:
            t0, key, doc = self._cache
            if doc is not None and key == max_mag and now - t0 < 10:
                return doc
        t = self.clock()
        _, sun = sun_altaz(self.lat, self.lon, t)
        cat = self._load()
        rows = []
        if cat is not None:
            az, alt = apparent_altaz(cat, self.lat, self.lon, t)
            for i in np.nonzero((alt > -1) & (cat.vmag <= max_mag))[0]:
                name, desig, con = self._labels[i]
                rows.append([name, desig, con, round(float(cat.vmag[i]), 2), round(float(az[i]), 3),
                             round(float(alt[i]), 3)])
        doc = {"utc": _utc(t), "sun_alt": round(sun, 2), "catalogue": cat is not None, "stars": rows}
        with self._lock:
            self._cache = (now, max_mag, doc)
        return doc

    def sun_alt(self) -> float:
        return sun_altaz(self.lat, self.lon, self.clock())[1]

    # ---- calibration ---------------------------------------------------------------

    @property
    def auto(self) -> bool:
        try:
            return self.auto_file.read_text().strip().lower() != "off"
        except OSError:
            return True

    def set_auto(self, on: bool) -> None:
        self.auto_file.parent.mkdir(parents=True, exist_ok=True)
        self.auto_file.write_text("on\n" if on else "off\n")

    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def status(self) -> dict:
        try:
            doc = json.loads(self.status_file.read_text())
        except (OSError, ValueError):
            doc = {"state": "never"}
        if doc.get("state") == "running" and not self.running():
            doc["state"] = "failed"          # the run died without saying (server restarted, killed)
            doc.setdefault("message", "stopped before it finished")
        doc["auto"] = self.auto
        doc["sun_alt"] = round(self.sun_alt(), 1)
        doc["dark_enough"] = doc["sun_alt"] <= BUTTON_SUN_ALT
        return doc

    def start(self, why: str, min_move: float | None = None) -> bool:
        """Run the alignment unless it's already running. min_move 0: apply
        whatever it finds (asked for by hand); None: only if the rig moved."""
        with self._lock:
            if self.running():
                return False
            self.status_file.parent.mkdir(parents=True, exist_ok=True)
            self.status_file.write_text(json.dumps({"state": "running", "started_utc": _utc(self.clock()),
                                                    "why": why, "message": "starting"}))
            cmd = self.command + ([] if min_move is None else ["--min-move", str(min_move)])
            self._proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True

    def due(self) -> str | None:
        """Why an automatic run should start now, or None."""
        if not self.auto or self.running() or self.sun_alt() > DARK_SUN_ALT:
            return None
        doc = self.status()
        last = doc.get("started_utc")
        try:
            last_t = datetime.strptime(last, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
        except (TypeError, ValueError):
            return "first night"
        now = self.clock().timestamp()
        if last_t < self._started:
            return "dark after start-up"
        if doc.get("state") == "failed" and now - last_t > RETRY_S:
            return "trying again"
        if doc.get("state") == "done" and now - last_t > NIGHTLY_S:
            return "nightly"
        return None

    # ---- Night and Day ------------------------------------------------------------

    @property
    def auto_presets(self) -> bool:
        if self.preset_file is None or self.apply_preset is None:
            return False
        try:
            return self.preset_file.read_text().strip().lower() != "off"
        except OSError:
            return True

    def preset_due(self) -> str | None:
        """The preset to switch to now, or None."""
        if not self.auto_presets:
            return None
        sun = self.sun_alt()
        if sun < NIGHT_PRESET_SUN_ALT and self._preset != "night":
            return "night"
        if sun > DAY_PRESET_SUN_ALT and self._preset != "day":
            return "day"
        if self._preset is None:   # start-up in the gap: whichever side of it we're nearer
            return "night" if sun < (NIGHT_PRESET_SUN_ALT + DAY_PRESET_SUN_ALT) / 2 else "day"
        return None

    def switch_preset(self) -> None:
        name = self.preset_due()
        if name:
            print(f"sky: {name} (Sun at {self.sun_alt():.1f}°)", file=sys.stderr, flush=True)
            self.apply_preset(name)
            self._preset = name

    def run_forever(self, stop: threading.Event) -> None:
        try:
            self.switch_preset()
        except Exception as e:
            print(f"preset switch failed: {e}", file=sys.stderr, flush=True)
        while not stop.wait(CHECK_S):
            try:
                self.switch_preset()
            except Exception as e:
                print(f"preset switch failed: {e}", file=sys.stderr, flush=True)
            try:
                why = self.due()
                if why:
                    print(f"calibration from the stars: {why}", file=sys.stderr, flush=True)
                    self.start(why)
            except Exception as e:   # keep the loop: the next minute may do better
                print(f"calibration check failed: {e}", file=sys.stderr, flush=True)
