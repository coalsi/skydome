"""Lens models and posed cameras.

Two intrinsic models, matching what calibrate_intrinsics.py will fit
(handoff §5.2):

* ``FisheyeLens`` - OpenCV fisheye / Kannala-Brandt:
  theta_d = theta * (1 + k1 theta^2 + k2 theta^4 + k3 theta^6 + k4 theta^8)
* ``PinholeLens`` - OpenCV pinhole with radial distortion k1, k2, k3.

Both map camera-frame rays to pixel coordinates and back. Pixel centres
are at integer coordinates (OpenCV convention).
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

import numpy as np

_EPS = 1e-12


@dataclass(frozen=True)
class FisheyeLens:
    width: int
    height: int
    fx: float
    fy: float
    cx: float
    cy: float
    k: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
    # Rays further than this from the optical axis are rejected, which keeps
    # the distortion polynomial inside its fitted, monotonic range.
    max_theta_deg: float = 100.0

    @classmethod
    def from_hfov(cls, width: int, height: int, hfov_deg: float) -> "FisheyeLens":
        """Ideal equidistant fisheye whose image width spans hfov_deg."""
        f = (width / 2) / np.radians(hfov_deg / 2)
        return cls(width, height, f, f, (width - 1) / 2, (height - 1) / 2)

    def _distort(self, theta: np.ndarray) -> np.ndarray:
        k1, k2, k3, k4 = self.k
        t2 = theta * theta
        return theta * (1 + t2 * (k1 + t2 * (k2 + t2 * (k3 + t2 * k4))))

    def _distort_deriv(self, theta: np.ndarray) -> np.ndarray:
        k1, k2, k3, k4 = self.k
        t2 = theta * theta
        return 1 + t2 * (3 * k1 + t2 * (5 * k2 + t2 * (7 * k3 + t2 * 9 * k4)))

    def project(self, rays: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Camera-frame rays (..., 3) -> (u, v, valid)."""
        x, y, z = rays[..., 0], rays[..., 1], rays[..., 2]
        r = np.hypot(x, y)
        theta = np.arctan2(r, z)
        theta_d = self._distort(theta)
        scale = np.where(r > _EPS, theta_d / np.maximum(r, _EPS), 1.0 / np.maximum(z, _EPS))
        u = self.fx * x * scale + self.cx
        v = self.fy * y * scale + self.cy
        valid = theta <= np.radians(self.max_theta_deg)
        return u, v, valid

    def unproject(self, u: np.ndarray, v: np.ndarray) -> np.ndarray:
        """Pixel coordinates -> unit camera-frame rays (..., 3)."""
        mx = (np.asarray(u, dtype=np.float64) - self.cx) / self.fx
        my = (np.asarray(v, dtype=np.float64) - self.cy) / self.fy
        theta_d = np.hypot(mx, my)
        theta = theta_d.copy()
        if any(self.k):
            for _ in range(20):
                theta -= (self._distort(theta) - theta_d) / self._distort_deriv(theta)
        sin_t = np.sin(theta)
        scale = np.where(theta_d > _EPS, sin_t / np.maximum(theta_d, _EPS), 1.0)
        return np.stack([mx * scale, my * scale, np.cos(theta)], axis=-1)


@dataclass(frozen=True)
class PinholeLens:
    width: int
    height: int
    fx: float
    fy: float
    cx: float
    cy: float
    k: tuple[float, float, float] = (0.0, 0.0, 0.0)

    @classmethod
    def from_hfov(cls, width: int, height: int, hfov_deg: float) -> "PinholeLens":
        """Ideal rectilinear lens whose image width spans hfov_deg."""
        f = (width / 2) / np.tan(np.radians(hfov_deg / 2))
        return cls(width, height, f, f, (width - 1) / 2, (height - 1) / 2)

    def _distort(self, r2: np.ndarray) -> np.ndarray:
        k1, k2, k3 = self.k
        return 1 + r2 * (k1 + r2 * (k2 + r2 * k3))

    def project(self, rays: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        x, y, z = rays[..., 0], rays[..., 1], rays[..., 2]
        valid = z > 1e-6
        zs = np.where(valid, z, 1.0)
        a, b = x / zs, y / zs
        d = self._distort(a * a + b * b)
        return self.fx * a * d + self.cx, self.fy * b * d + self.cy, valid

    def unproject(self, u: np.ndarray, v: np.ndarray) -> np.ndarray:
        ad = (np.asarray(u, dtype=np.float64) - self.cx) / self.fx
        bd = (np.asarray(v, dtype=np.float64) - self.cy) / self.fy
        rd = np.hypot(ad, bd)
        r = rd.copy()
        if any(self.k):
            k1, k2, k3 = self.k
            # Newton on f(r) = r * d(r^2) - rd
            for _ in range(30):
                r2 = r * r
                f = r * self._distort(r2) - rd
                df = self._distort(r2) + 2 * r2 * (k1 + r2 * (2 * k2 + 3 * k3 * r2))
                r = r - f / df
        scale = np.where(rd > _EPS, r / np.maximum(rd, _EPS), 1.0)
        a, b = ad * scale, bd * scale
        rays = np.stack([a, b, np.ones_like(a)], axis=-1)
        return rays / np.linalg.norm(rays, axis=-1, keepdims=True)


Lens = FisheyeLens | PinholeLens


@dataclass(frozen=True)
class Camera:
    """A lens plus its world-from-camera rotation."""

    name: str
    lens: Lens
    rotation: np.ndarray = field(repr=False)

    @property
    def width(self) -> int:
        return self.lens.width

    @property
    def height(self) -> int:
        return self.lens.height

    @property
    def axis(self) -> np.ndarray:
        """Optical axis in ENU."""
        return self.rotation[:, 2]

    def world_to_pixel(self, dirs: np.ndarray, margin: float = 0.0):
        """ENU directions (..., 3) -> (u, v, valid). valid also requires the pixel
        to land inside the image, shrunk by ``margin`` pixels on every side."""
        # R^T applied to row vectors (einsum avoids spurious BLAS warnings on macOS)
        rays = np.einsum("...i,ij->...j", dirs, self.rotation)
        u, v, valid = self.lens.project(rays)
        inside = (u >= margin) & (u <= self.width - 1 - margin) & (v >= margin) & (v <= self.height - 1 - margin)
        return u, v, valid & inside

    def pixel_to_world(self, u: np.ndarray, v: np.ndarray) -> np.ndarray:
        """Pixel coordinates -> unit ENU directions (..., 3)."""
        return np.einsum("...j,ij->...i", self.lens.unproject(u, v), self.rotation)

    def scaled(self, factor: float) -> "Camera":
        """Same camera at a different image resolution (for previews and tests)."""
        lens = self.lens
        w, h = round(lens.width * factor), round(lens.height * factor)
        sx, sy = w / lens.width, h / lens.height
        new = replace(
            lens, width=w, height=h, fx=lens.fx * sx, fy=lens.fy * sy,
            cx=(lens.cx + 0.5) * sx - 0.5, cy=(lens.cy + 0.5) * sy - 0.5,
        )
        return Camera(self.name, new, self.rotation)
