"""Synthetic sky for testing the stitcher without hardware.

Renders a scene (sky-glow gradient, optional alt/az grid, point-like stars)
into each camera through its lens model, or directly into an output canvas
as ground truth.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .camera import Camera
from .geometry import altaz_to_vec, vec_to_altaz


@dataclass(frozen=True)
class StarField:
    dirs: np.ndarray  # (N, 3) ENU unit vectors
    flux: np.ndarray  # (N,) peak brightness

    def __len__(self) -> int:
        return len(self.flux)


def random_stars(n: int, seed: int = 0, min_alt_deg: float = -10.0) -> StarField:
    """Stars spread uniformly over the sky above ``min_alt_deg``."""
    rng = np.random.default_rng(seed)
    z = rng.uniform(np.sin(np.radians(min_alt_deg)), 1.0, n)
    az = rng.uniform(0, 360, n)
    alt = np.degrees(np.arcsin(z))
    return StarField(altaz_to_vec(az, alt), rng.uniform(0.4, 1.0, n))


@dataclass(frozen=True)
class SkyScene:
    stars: StarField | None = None
    grid_step_deg: float = 0.0     # 0 = no grid
    grid_width_deg: float = 0.25
    glow: bool = True

    def background(self, dirs: np.ndarray) -> np.ndarray:
        """Smooth part of the scene evaluated per direction."""
        az, alt = vec_to_altaz(dirs)
        out = np.zeros(dirs.shape[:-1], dtype=np.float32)
        if self.glow:
            out += 0.05 + 0.15 * (1 - np.clip(np.sin(np.radians(alt)), 0, 1))
        if self.grid_step_deg:
            # Lines with a Gaussian cross-section, so resampling them is well behaved
            s, w = self.grid_step_deg, self.grid_width_deg
            d_alt = np.abs((alt + s / 2) % s - s / 2)
            d_az = np.abs(((az + s / 2) % s - s / 2) * np.cos(np.radians(alt)))
            d = np.minimum(d_alt, d_az)
            out += (0.5 * np.exp(-0.5 * (d / w) ** 2)).astype(np.float32)
        return out


def splat_points(image: np.ndarray, u: np.ndarray, v: np.ndarray, flux: np.ndarray, sigma_px: float) -> None:
    """Add Gaussian spots at sub-pixel positions, in place."""
    h, w = image.shape
    r = int(np.ceil(3 * sigma_px))
    offs = np.arange(-r, r + 1)
    for du in offs:
        for dv in offs:
            x = np.round(u).astype(np.int64) + du
            y = np.round(v).astype(np.int64) + dv
            ok = (x >= 0) & (x < w) & (y >= 0) & (y < h)
            g = flux * np.exp(-((x - u) ** 2 + (y - v) ** 2) / (2 * sigma_px**2))
            np.add.at(image, (y[ok], x[ok]), g[ok].astype(image.dtype))


def render_camera(cam: Camera, scene: SkyScene, psf_sigma_px: float = 1.2) -> np.ndarray:
    """What ``cam`` would see of ``scene``. Pixels whose ray leaves the lens model are 0."""
    u, v = np.meshgrid(np.arange(cam.width, dtype=np.float64), np.arange(cam.height, dtype=np.float64))
    dirs = cam.pixel_to_world(u, v)
    img = scene.background(dirs)
    if scene.stars is not None:
        su, sv, ok = cam.world_to_pixel(scene.stars.dirs)
        splat_points(img, su[ok], sv[ok], scene.stars.flux[ok], psf_sigma_px)
    return img


def render_canvas(canvas, scene: SkyScene, psf_sigma_px: float = 1.2) -> np.ndarray:
    """Ground truth: the scene drawn straight into an output canvas."""
    u, v = np.meshgrid(np.arange(canvas.width, dtype=np.float64), np.arange(canvas.height, dtype=np.float64))
    dirs, inside = canvas.pixel_to_world(u, v)
    img = scene.background(dirs) * inside
    if scene.stars is not None:
        su, sv = canvas.world_to_pixel(scene.stars.dirs)
        splat_points(img, su, sv, scene.stars.flux, psf_sigma_px)
    return img


def find_peaks(image: np.ndarray, threshold: float, radius: int = 2) -> np.ndarray:
    """Local maxima above ``threshold`` with intensity-weighted centroids. Returns (N, 2) of (u, v)."""
    h, w = image.shape
    pad = np.pad(image, radius, mode="constant", constant_values=-np.inf)
    is_max = image > threshold
    for dy in range(-radius, radius + 1):
        for dx in range(-radius, radius + 1):
            if dx or dy:
                is_max &= image >= pad[radius + dy:radius + dy + h, radius + dx:radius + dx + w]
    ys, xs = np.nonzero(is_max)
    out = []
    for y, x in zip(ys, xs):
        y0, y1 = max(y - radius, 0), min(y + radius + 1, h)
        x0, x1 = max(x - radius, 0), min(x + radius + 1, w)
        patch = np.clip(image[y0:y1, x0:x1] - threshold, 0, None)
        gy, gx = np.mgrid[y0:y1, x0:x1]
        s = patch.sum()
        out.append(((gx * patch).sum() / s, (gy * patch).sum() / s))
    return np.array(out).reshape(-1, 2)
