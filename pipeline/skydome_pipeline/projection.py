"""Output canvases: the all-sky dome and the equirectangular hemisphere.

Both map output pixels to ENU directions and back. Pixel centres are at
integer coordinates.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .geometry import altaz_to_vec, vec_to_altaz


@dataclass(frozen=True)
class AllSkyProjection:
    """Azimuthal-equidistant all-sky image (handoff §5.4).

    Zenith at the centre, the circle of radius size/2 at zenith angle
    ``edge_zenith_deg`` (90 = horizon). North is up. ``east_left`` gives the
    conventional view of the sky from below (east on the left); False gives
    a map view (east on the right).
    """

    size: int = 4096
    edge_zenith_deg: float = 90.0
    east_left: bool = True

    @property
    def width(self) -> int:
        return self.size

    @property
    def height(self) -> int:
        return self.size

    @property
    def deg_per_px(self) -> float:
        return self.edge_zenith_deg / (self.size / 2)

    def _east_sign(self) -> float:
        return -1.0 if self.east_left else 1.0

    def pixel_to_world(self, u: np.ndarray, v: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """Pixel coords -> (ENU directions, inside-the-circle mask)."""
        half = self.size / 2
        dx = (np.asarray(u, dtype=np.float64) + 0.5 - half) / half
        dy = (np.asarray(v, dtype=np.float64) + 0.5 - half) / half
        rho = np.hypot(dx, dy)
        zenith = rho * self.edge_zenith_deg
        az = np.degrees(np.arctan2(self._east_sign() * dx, -dy))
        return altaz_to_vec(az, 90.0 - zenith), rho <= 1.0

    def world_to_pixel(self, dirs: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        az, alt = vec_to_altaz(dirs)
        rho = (90.0 - alt) / self.edge_zenith_deg
        a = np.radians(az)
        half = self.size / 2
        u = self._east_sign() * np.sin(a) * rho * half + half - 0.5
        v = -np.cos(a) * rho * half + half - 0.5
        return u, v

    def grid(self, step: int = 1) -> tuple[np.ndarray, np.ndarray]:
        """Directions and mask for every ``step``-th output pixel."""
        idx = np.arange(0, self.size, step, dtype=np.float64)
        u, v = np.meshgrid(idx, idx)
        return self.pixel_to_world(u, v)


@dataclass(frozen=True)
class EquirectHemisphere:
    """Equirectangular sky for the 3D viewer: x = azimuth 0..360 (north at the
    left edge, increasing east), y = altitude 90 (top) .. alt_min (bottom)."""

    width: int = 8192
    height: int = 2048
    alt_min_deg: float = 0.0

    def pixel_to_world(self, u: np.ndarray, v: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        u = np.asarray(u, dtype=np.float64)
        v = np.asarray(v, dtype=np.float64)
        az = (u + 0.5) / self.width * 360.0
        alt = 90.0 - (v + 0.5) / self.height * (90.0 - self.alt_min_deg)
        return altaz_to_vec(az, alt), np.ones(np.broadcast(u, v).shape, dtype=bool)

    def world_to_pixel(self, dirs: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        az, alt = vec_to_altaz(dirs)
        u = az / 360.0 * self.width - 0.5
        v = (90.0 - alt) / (90.0 - self.alt_min_deg) * self.height - 0.5
        return u, v

    def grid(self, step: int = 1) -> tuple[np.ndarray, np.ndarray]:
        u, v = np.meshgrid(np.arange(0, self.width, step, dtype=np.float64),
                           np.arange(0, self.height, step, dtype=np.float64))
        return self.pixel_to_world(u, v)
