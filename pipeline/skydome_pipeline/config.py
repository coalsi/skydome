"""Typed loaders for config/*.yaml."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import numpy as np
import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

from .camera import Camera, FisheyeLens, PinholeLens
from .geometry import camera_rotation

CAMERA_NAMES = ("zenith", "north", "east", "south", "west")


class Site(BaseModel):
    name: str
    latitude_deg: float = Field(ge=-90, le=90)
    longitude_deg: float = Field(ge=-180, le=180)
    elevation_m: float
    timezone: str


class Pointing(BaseModel):
    az_deg: float
    alt_deg: float = Field(ge=-90, le=90)
    roll_deg: float = 0.0


class CameraEntry(BaseModel):
    name: Literal["zenith", "north", "east", "south", "west"]
    id_path: str | None = None
    pointing: Pointing


class SensorSpec(BaseModel):
    width: int
    height: int


class LensSpec(BaseModel):
    hfov_deg: float = Field(gt=0, lt=200)
    projection: Literal["equidistant", "rectilinear"]


class CamerasConfig(BaseModel):
    sensor: SensorSpec
    lens: LensSpec
    cameras: list[CameraEntry]

    @field_validator("cameras")
    @classmethod
    def _five_unique(cls, v: list[CameraEntry]) -> list[CameraEntry]:
        names = [c.name for c in v]
        if sorted(names) != sorted(CAMERA_NAMES):
            raise ValueError(f"need exactly one each of {CAMERA_NAMES}, got {names}")
        return v

    def nominal_cameras(self) -> list[Camera]:
        """Cameras from the nominal lens model and mount pointing."""
        cls = FisheyeLens if self.lens.projection == "equidistant" else PinholeLens
        lens = cls.from_hfov(self.sensor.width, self.sensor.height, self.lens.hfov_deg)
        return [Camera(c.name, lens, camera_rotation(c.pointing.az_deg, c.pointing.alt_deg, c.pointing.roll_deg))
                for c in self.cameras]


class Capture(BaseModel):
    format: Literal["MJPG", "YUYV"]
    width: int
    height: int
    fps: float

    @model_validator(mode="after")
    def _no_4k_yuyv(self) -> "Capture":
        # Handoff §2.1 / §9: never capture YUY2 above 1080p
        if self.format == "YUYV" and self.width * self.height > 1920 * 1080:
            raise ValueError("YUYV is only allowed up to 1920x1080; use MJPG for 4K")
        return self


class Mode(BaseModel):
    model_config = {"extra": "allow"}
    capture: Capture


class ModesConfig(BaseModel):
    model_config = {"extra": "allow"}
    modes: dict[str, Mode]


def _load(path: Path) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def load_site(config_dir: str | Path) -> Site:
    return Site.model_validate(_load(Path(config_dir) / "site.yaml"))


def load_cameras(config_dir: str | Path) -> CamerasConfig:
    return CamerasConfig.model_validate(_load(Path(config_dir) / "cameras.yaml"))


def load_modes(config_dir: str | Path) -> ModesConfig:
    return ModesConfig.model_validate(_load(Path(config_dir) / "modes.yaml"))


def load_extrinsics(config_dir: str | Path) -> dict[str, np.ndarray]:
    """Measured world-from-camera rotations from config/calibration/extrinsics.yaml
    (align_from_overlap.py now, plate solving later), by camera name."""
    f = Path(config_dir) / "calibration" / "extrinsics.yaml"
    if not f.exists():
        return {}
    doc = _load(f) or {}
    out = {}
    for name, entry in (doc.get("cameras") or {}).items():
        r = np.asarray(entry["rotation"], dtype=np.float64)
        if r.shape != (3, 3) or not np.allclose(r @ r.T, np.eye(3), atol=1e-6):
            raise ValueError(f"{f}: {name} rotation is not a rotation matrix")
        out[name] = r
    return out


def fitted_hfov(config_dir: str | Path) -> float | None:
    """Field of view align_from_overlap.py fitted to the overlaps, if any."""
    f = Path(config_dir) / "calibration" / "extrinsics.yaml"
    doc = (_load(f) or {}) if f.exists() else {}
    return doc.get("lens_hfov_deg")


def calibrated_cameras(config_dir: str | Path) -> list[Camera]:
    """Nominal cameras, refined by what's been measured. Lens: the board
    calibration config/calibration/<name>.yaml if there is one, else the
    field of view fitted to the overlaps, else the nominal lens. Pointing:
    config/calibration/extrinsics.yaml where it has an entry."""
    from .calibration import load_lens

    cfg = load_cameras(config_dir)
    rotations = load_extrinsics(config_dir)
    hfov = fitted_hfov(config_dir)
    cams = []
    for cam in cfg.nominal_cameras():
        f = Path(config_dir) / "calibration" / f"{cam.name}.yaml"
        if f.exists():
            lens = load_lens(f)
        elif hfov:
            lens = type(cam.lens).from_hfov(cfg.sensor.width, cfg.sensor.height, hfov)
        else:
            lens = cam.lens
        cams.append(Camera(cam.name, lens, rotations.get(cam.name, cam.rotation)))
    return cams
