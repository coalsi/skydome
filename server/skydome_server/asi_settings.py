"""Picture settings for the ZWO all-sky camera (DECISIONS.md D-029).

skydome-live owns the camera (ZWO's SDK opens it for one program only), so
the server doesn't talk to it: it writes var/asi_settings.yaml, which
skydome-live rereads within a second (pipeline/native/src/asi_source.h), and
offers the ranges the camera reported when it opened (var/asi_caps.json).
The same face as CameraSettings, so the web page, the presets and ONVIF
work on either. Units are in the names; gains in the camera's 0.1 dB.
"""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

import yaml

from .v4l2 import Control

# Mirrors AsiSettings in asi_source.h (keep the defaults the same)
BASE = {c.name: c for c in (
    Control(0, "auto_exposure", 0, 1, 1, 1),
    Control(0, "exposure_us", 32, 60_000_000, 1, 10_000),       # manual
    Control(0, "gain", 0, 600, 1, 0),                            # manual
    Control(0, "auto_max_exposure_ms", 100, 60_000, 1, 20_000),  # automatic: the longest at night
    Control(0, "auto_max_gain", 0, 600, 1, 200),                 # automatic: the most gain
    Control(0, "auto_target", 1, 90, 1, 18),                     # automatic: the sky's median, % of full
    Control(0, "offset", 0, 255, 1, 10),                         # black level pedestal
    Control(0, "wb_red", 10, 400, 1, 180),                       # % of green
    Control(0, "wb_blue", 10, 400, 1, 160),
    Control(0, "gamma", 50, 300, 1, 220),                        # x100; 100 = linear (night stretch)
    Control(0, "bandwidth", 40, 100, 1, 80),                     # USB, %
    Control(0, "flip", 0, 3, 1, 0),
)}


class AsiSettings:
    # ONVIF's exposure in these terms: exposure_us in µs, auto_exposure 1 = automatic
    ONVIF_EXPOSURE = ("exposure_us", 1, 1, 0)

    def __init__(self, path: Path, caps_path: Path | None = None, log=print):
        self.path = Path(path)
        self.caps_path = Path(caps_path) if caps_path else None
        self._log = log
        self._lock = threading.Lock()
        self.values: dict[str, int] = {}
        if self.path.exists():
            loaded = yaml.safe_load(self.path.read_text()) or {}
            self.values = {str(k): int(v) for k, v in loaded.items() if str(k) in BASE}
            for k in sorted(set(map(str, loaded)) - set(BASE)):
                self._log(f"camera settings: ignoring {k} in {self.path} (not a ZWO camera setting)", file=sys.stderr)

    def caps(self) -> dict:
        """What the camera reported when skydome-live opened it ({} before it has)."""
        try:
            return json.loads(self.caps_path.read_text()) if self.caps_path else {}
        except (OSError, ValueError):
            return {}

    def controls(self) -> dict[str, Control]:
        """The settings and their ranges: the camera's own for exposure, gain and offset once it has opened."""
        out = dict(BASE)
        reported = self.caps().get("controls") or {}

        def limits(name: str, key: str, cap_max: int | None = None, default: bool = False) -> None:
            r = reported.get(key)
            if not r:
                return
            c = out[name]
            hi = min(int(r["max"]), cap_max) if cap_max else int(r["max"])
            lo = max(int(r["min"]), c.minimum) if name == "exposure_us" else int(r["min"])
            dflt = int(r["default"]) if default else min(max(c.default, lo), hi)
            out[name] = Control(0, name, lo, hi, 1, dflt)

        limits("exposure_us", "Exposure", cap_max=BASE["exposure_us"].maximum)
        limits("gain", "Gain")
        limits("auto_max_gain", "Gain")
        limits("offset", "Offset", default=True)
        return out

    def effective(self) -> dict[str, int]:
        ctrls = self.controls()
        with self._lock:
            return {name: self.values.get(name, c.default) for name, c in ctrls.items()}

    def update(self, changes: dict[str, int]) -> None:
        """Validate and store; skydome-live picks it up. ValueError if out of range."""
        ctrls = self.controls()
        for name, v in changes.items():
            c = ctrls.get(name)
            if c is None:
                raise ValueError(f"{name} is not one of the ZWO camera's settings")
            if not c.minimum <= v <= c.maximum:
                raise ValueError(f"{name} {v} is outside {c.minimum}..{c.maximum}")
        with self._lock:
            self.values.update(changes)
            self._save()

    def sync(self) -> None:
        """Nothing to push: skydome-live reads the file itself, also after a replug."""

    def watch(self, stop: threading.Event, interval: float = 5.0) -> None:
        stop.wait()

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text("# ZWO camera settings from the web page or ONVIF (or by hand); skydome-live rereads\n"
                       "# this within a second. See server/skydome_server/asi_settings.py for the units.\n"
                       + yaml.safe_dump(dict(sorted(self.values.items()))))
        tmp.replace(self.path)
