"""Typed loaders for config/*.yaml."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

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


def calibrated_cameras(config_dir: str | Path) -> list[Camera]:
    """Nominal cameras, with the lens replaced by config/calibration/<name>.yaml
    wherever calibrate_intrinsics.py has written one."""
    from .calibration import load_lens

    cams = []
    for cam in load_cameras(config_dir).nominal_cameras():
        f = Path(config_dir) / "calibration" / f"{cam.name}.yaml"
        cams.append(Camera(cam.name, load_lens(f), cam.rotation) if f.exists() else cam)
    return cams
