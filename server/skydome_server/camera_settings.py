"""Camera controls shared by every camera in the rig.

All five cameras get the same values (D-015: one exposure target for the
whole dome, so the seams stay even). The web page and ONVIF both change
them, through the same object. Values someone has set are kept in
var/camera_settings.yaml (V4L2 control names, the cameras' own units) and
re-applied whenever a camera appears, since a replugged UVC camera wakes up
with its firmware defaults. Controls nobody has set stay at those defaults.

The file can also be edited by hand for controls ONVIF has no field for,
e.g. `power_line_frequency: 2` (60 Hz) or `white_balance_temperature: 5500`;
restart the service to apply.
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path
from typing import Callable

import yaml

from .v4l2 import CameraNode, Control, Device, find_cameras

# What the U3CAM4K60 reports (Phase 0 findings), for when no camera is plugged in
FALLBACK_CONTROLS = {c.name: c for c in (
    Control(0x00980900, "brightness", 0, 255, 1, 128),
    Control(0x00980901, "contrast", 0, 128, 1, 65),
    Control(0x00980902, "saturation", 0, 128, 1, 100),
    Control(0x00980903, "hue", -128, 128, 1, 0),
    Control(0x0098090C, "white_balance_automatic", 0, 1, 1, 1),
    Control(0x00980910, "gamma", 0, 255, 1, 138),
    Control(0x00980913, "gain", 0, 190, 1, 140),
    Control(0x00980918, "power_line_frequency", 0, 2, 1, 1),
    Control(0x0098091A, "white_balance_temperature", 2800, 6500, 1, 4650),
    Control(0x0098091B, "sharpness", 14, 255, 1, 128),
    Control(0x0098091C, "backlight_compensation", 0, 160, 1, 48),
    Control(0x009A0901, "auto_exposure", 1, 3, 1, 3),
    Control(0x009A0902, "exposure_time_absolute", 1, 10000, 1, 156),
)}

# The picture controls offered for change (web page, ONVIF, the file). The
# cameras also report pan/tilt/zoom/privacy controls they refuse to set
# (EPIPE), which a stored value would retry every few seconds forever.
IMAGE_CONTROLS = frozenset(FALLBACK_CONTROLS)
# Held at the camera's default on every camera: the stitch's alignment
# assumes the whole sensor picture. A digital zoom (1.31x, saved from the web
# page before IMAGE_CONTROLS existed) is kept by the camera's firmware across
# power cycles and silently cropped all five pictures on 2026-11-03
GEOMETRY_DEFAULTS = ("zoom_absolute",)

AUTO_EXPOSURE_MANUAL = 1
AUTO_EXPOSURE_AUTO = 3   # "aperture priority", the cameras' only automatic mode

# Set automatic modes first: a UVC camera refuses a manual value while its
# automatic mode is on, and skips it here for the same reason
_MASTERS = ("auto_exposure", "white_balance_automatic")
_SLAVES = {"exposure_time_absolute": ("auto_exposure", AUTO_EXPOSURE_MANUAL),
           "white_balance_temperature": ("white_balance_automatic", 0)}


class CameraSettings:
    def __init__(self, path: Path, finder: Callable[[], list[CameraNode]] = find_cameras,
                 opener: Callable[[str], Device] = Device, log=print):
        self.path = Path(path)
        self._find = finder
        self._open = opener
        self._log = log
        self._lock = threading.Lock()         # values, _controls
        self._apply_lock = threading.Lock()   # one pass over the cameras at a time
        self._controls: dict[str, Control] | None = None
        self._applied: set[tuple] = set()
        self.values: dict[str, int] = {}
        self._refused: set[tuple[str, str]] = set()   # (device, control) refusals already logged
        if self.path.exists():
            loaded = yaml.safe_load(self.path.read_text()) or {}
            self.values = {str(k): int(v) for k, v in loaded.items() if str(k) in IMAGE_CONTROLS}
            for k in sorted(set(map(str, loaded)) - IMAGE_CONTROLS):
                self._log(f"camera settings: ignoring {k} in {self.path} (not a picture control)", file=sys.stderr)

    def controls(self) -> dict[str, Control]:
        """The cameras' picture controls and ranges, read from the first camera found."""
        with self._lock:
            if self._controls is None:
                for cam in self._find():
                    try:
                        with self._open(cam.device) as d:
                            self._controls = {n: c for n, c in d.controls().items() if n in IMAGE_CONTROLS}
                        break
                    except OSError:
                        continue
            return self._controls if self._controls is not None else FALLBACK_CONTROLS

    def effective(self) -> dict[str, int]:
        """What the cameras are running: set values over the firmware defaults."""
        ctrls = self.controls()
        with self._lock:
            return {name: self.values.get(name, c.default) for name, c in ctrls.items()}

    def update(self, changes: dict[str, int]) -> None:
        """Validate, store and apply to every camera. ValueError if out of range."""
        ctrls = self.controls()
        for name, v in changes.items():
            c = ctrls.get(name)
            if c is None:
                raise ValueError(f"{name} is not one of the cameras' picture controls")
            if not c.minimum <= v <= c.maximum:
                raise ValueError(f"{name} {v} is outside {c.minimum}..{c.maximum}")
        with self._lock:
            self.values.update(changes)
            self._refused = {r for r in self._refused if r[1] not in changes}   # a new value: try again
            self._save()
        with self._apply_lock:
            for cam in self._find():
                self._apply(cam)

    def sync(self) -> None:
        """Apply the stored values to cameras that are new since the last call."""
        with self._apply_lock:
            cams = self._find()
            self._applied &= {c.key for c in cams}
            for cam in cams:
                if cam.key not in self._applied:
                    self._apply(cam)

    def watch(self, stop: threading.Event, interval: float = 5.0) -> None:
        while not stop.is_set():
            try:
                self.sync()
            except Exception as e:   # keep watching whatever a scan throws
                self._log(f"camera settings: {e}", file=sys.stderr)
            stop.wait(interval)

    def _apply(self, cam: CameraNode) -> None:
        with self._lock:
            values = dict(self.values)
        applied = 0
        try:
            with self._open(cam.device) as d:
                ctrls = d.controls()
                for name in GEOMETRY_DEFAULTS:
                    if name in ctrls:
                        try:
                            d.set(ctrls[name].id, ctrls[name].default)
                        except OSError:
                            pass   # a camera without a usable zoom has nothing to undo
                for name in sorted(values, key=lambda n: (n not in _MASTERS, n)):
                    if name not in ctrls:
                        continue
                    master = _SLAVES.get(name)
                    if master and master[0] in ctrls and \
                            values.get(master[0], ctrls[master[0]].default) != master[1]:
                        continue
                    try:
                        d.set(ctrls[name].id, values[name])
                        applied += 1
                    except OSError as e:
                        # This camera refuses this value: the rest still go on, and it
                        # isn't retried until the camera is replugged or the value changes
                        if (cam.device, name) not in self._refused:
                            self._refused.add((cam.device, name))
                            self._log(f"camera settings: {cam.device} refused {name}={values[name]}: {e}",
                                      file=sys.stderr)
        except OSError as e:
            self._log(f"camera settings: {cam.device}: {e}", file=sys.stderr)
            return   # couldn't open it: retried on the next sync
        self._log(f"camera settings: applied {applied} to {cam.device}", file=sys.stderr)
        self._applied.add(cam.key)

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text("# Camera controls set from the web page or ONVIF (or by hand), applied to all cameras.\n"
                       "# V4L2 names and units; see server/skydome_server/camera_settings.py.\n"
                       + yaml.safe_dump(dict(sorted(self.values.items()))))
        tmp.replace(self.path)
