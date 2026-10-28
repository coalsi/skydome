"""Stitch look-up tables: for every output pixel, where to sample each camera
and how much weight that camera gets.

This is the CPU reference implementation. The production path (handoff §4)
runs the same remap + weighted sum on the GPU from these tables; to keep
memory inside the 8 GB budget the tables are stored on a coarse grid and
interpolated per pixel by the kernel (DECISIONS.md D-005).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, Sequence

import numpy as np

from .camera import Camera


class Canvas(Protocol):
    width: int
    height: int

    def pixel_to_world(self, u, v): ...

    def world_to_pixel(self, dirs): ...


@dataclass
class StitchLUT:
    names: list[str]
    map_u: np.ndarray    # (C, H, W) float32 source x per camera, NaN where unused
    map_v: np.ndarray    # (C, H, W) float32 source y per camera
    weight: np.ndarray   # (C, H, W) float32, sums to 1 where covered
    covered: np.ndarray  # (H, W) bool: inside the canvas and seen by >= 1 camera
    inside: np.ndarray   # (H, W) bool: inside the canvas (e.g. above the horizon)

    @property
    def overlap_count(self) -> np.ndarray:
        return (self.weight > 0).sum(axis=0)

    def save(self, path: str | Path) -> None:
        np.savez_compressed(path, names=np.array(self.names), map_u=self.map_u, map_v=self.map_v,
                            weight=self.weight, covered=self.covered, inside=self.inside)

    @classmethod
    def load(cls, path: str | Path) -> "StitchLUT":
        d = np.load(path)
        return cls(list(d["names"]), d["map_u"], d["map_v"], d["weight"], d["covered"], d["inside"])


def _smoothstep(x: np.ndarray) -> np.ndarray:
    x = np.clip(x, 0.0, 1.0)
    return x * x * (3 - 2 * x)


def edge_weight(u: np.ndarray, v: np.ndarray, width: int, height: int, feather_px: float) -> np.ndarray:
    """1 in the middle of the image, ramping smoothly to 0 at the borders."""
    d = np.minimum(np.minimum(u, width - 1 - u), np.minimum(v, height - 1 - v))
    return _smoothstep(d / feather_px)


def build_lut(canvas: Canvas, cameras: Sequence[Camera], feather_frac: float = 0.06) -> StitchLUT:
    """Tables for ``canvas`` at its full resolution.

    ``feather_frac`` is the width of the blend ramp at each image border, as a
    fraction of that camera's image width.
    """
    u_out, v_out = np.meshgrid(np.arange(canvas.width, dtype=np.float64),
                               np.arange(canvas.height, dtype=np.float64))
    dirs, inside = canvas.pixel_to_world(u_out, v_out)
    shape = (len(cameras), canvas.height, canvas.width)
    map_u = np.full(shape, np.nan, dtype=np.float32)
    map_v = np.full(shape, np.nan, dtype=np.float32)
    weight = np.zeros(shape, dtype=np.float32)
    for i, cam in enumerate(cameras):
        u, v, valid = cam.world_to_pixel(dirs)
        valid &= inside
        w = edge_weight(u, v, cam.width, cam.height, feather_frac * cam.width) * valid
        map_u[i][valid] = u[valid]
        map_v[i][valid] = v[valid]
        weight[i] = w
    total = weight.sum(axis=0)
    # Pixels seen only at a camera's very edge still get that camera at full weight
    single = (total <= 0) & np.any(~np.isnan(map_u), axis=0)
    for i in range(len(cameras)):
        weight[i][single & ~np.isnan(map_u[i])] = 1.0
    total = weight.sum(axis=0)
    weight = np.divide(weight, total, out=np.zeros_like(weight), where=total > 0)
    covered = inside & (total > 0)
    return StitchLUT([c.name for c in cameras], map_u, map_v, weight, covered, inside)


def bilinear(image: np.ndarray, u: np.ndarray, v: np.ndarray) -> np.ndarray:
    """Sample ``image`` (H, W[, C]) at float pixel coords; out-of-range -> 0."""
    h, w = image.shape[:2]
    ok = np.isfinite(u) & np.isfinite(v)
    uu = np.where(ok, u, 0.0)
    vv = np.where(ok, v, 0.0)
    x0 = np.clip(np.floor(uu).astype(np.int64), 0, w - 2)
    y0 = np.clip(np.floor(vv).astype(np.int64), 0, h - 2)
    fx = np.clip(uu - x0, 0.0, 1.0)
    fy = np.clip(vv - y0, 0.0, 1.0)
    if image.ndim == 3:
        fx, fy, ok = fx[..., None], fy[..., None], ok[..., None]
    out = (image[y0, x0] * (1 - fx) * (1 - fy) + image[y0, x0 + 1] * fx * (1 - fy)
           + image[y0 + 1, x0] * (1 - fx) * fy + image[y0 + 1, x0 + 1] * fx * fy)
    return np.where(ok, out, 0.0)


def stitch(lut: StitchLUT, images: Sequence[np.ndarray]) -> np.ndarray:
    """Blend the camera images into the canvas. Images are in LUT camera order."""
    out = None
    for i, img in enumerate(images):
        w = lut.weight[i]
        if not np.any(w):
            continue
        sample = bilinear(img.astype(np.float32), lut.map_u[i], lut.map_v[i])
        contrib = sample * (w[..., None] if sample.ndim == 3 else w)
        out = contrib if out is None else out + contrib
    return out
