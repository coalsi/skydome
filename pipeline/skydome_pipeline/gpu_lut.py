"""Stitch tables for the GPU (DECISIONS.md D-005, D-016).

The CUDA kernel in ``pipeline/native`` doesn't read full-resolution tables:
for every 8th output pixel (a grid node) it gets each camera's source
coordinates, interpolates them bilinearly per pixel (error < 0.01 px for
the 120° lenses), and computes the feather weight from the interpolated
coordinates with the same rule as ``lut.edge_weight``. A per-cell bit mask
says which cameras can reach each grid cell, so most pixels touch only one
or two cameras.

``grid_stitch`` is the NumPy twin of that kernel: same arithmetic, used to
test the kernel and to check it against the full-resolution reference in
``lut.py``.

File format (little endian), written by ``GridLUT.save``::

    8s   magic b"SKYLUT01"
    u32  canvas size (pixels), grid step, nodes per side N, camera count C
    f32  edge zenith angle (degrees; informational)
    C x [16s name, u32 width, u32 height, f32 feather_px]
    f32  u[C][N][N]   source x, NaN where the ray misses the lens model
    f32  v[C][N][N]
    u8   mask[N-1][N-1]   bit c = camera c can contribute to the cell
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

import numpy as np

from .camera import Camera
from .projection import AllSkyProjection

MAGIC = b"SKYLUT01"
MAX_CAMERAS = 8      # the cell mask is one byte


@dataclass
class GridLUT:
    names: list[str]
    canvas_size: int
    edge_zenith_deg: float
    step: int
    cam_size: np.ndarray    # (C, 2) int: width, height that u and v refer to
    feather_px: np.ndarray  # (C,) float32
    u: np.ndarray           # (C, N, N) float32
    v: np.ndarray           # (C, N, N) float32
    mask: np.ndarray        # (N-1, N-1) uint8

    @property
    def nodes(self) -> int:
        return self.u.shape[1]

    def save(self, path: str | Path) -> None:
        c, n = len(self.names), self.nodes
        with open(path, "wb") as f:
            f.write(MAGIC)
            f.write(struct.pack("<4If", self.canvas_size, self.step, n, c, self.edge_zenith_deg))
            for name, (w, h), fp in zip(self.names, self.cam_size, self.feather_px):
                f.write(struct.pack("<16s2If", name.encode()[:16], int(w), int(h), float(fp)))
            f.write(np.ascontiguousarray(self.u, "<f4").tobytes())
            f.write(np.ascontiguousarray(self.v, "<f4").tobytes())
            f.write(np.ascontiguousarray(self.mask, np.uint8).tobytes())

    @classmethod
    def load(cls, path: str | Path) -> "GridLUT":
        data = Path(path).read_bytes()
        if data[:8] != MAGIC:
            raise ValueError(f"{path}: not a SkyDome stitch table")
        size, step, n, c, edge = struct.unpack_from("<4If", data, 8)
        off = 8 + 20
        names, cam_size, feather = [], [], []
        for _ in range(c):
            name, w, h, fp = struct.unpack_from("<16s2If", data, off)
            off += 28
            names.append(name.rstrip(b"\0").decode())
            cam_size.append((w, h))
            feather.append(fp)
        plane = c * n * n * 4
        u = np.frombuffer(data, "<f4", c * n * n, off).reshape(c, n, n)
        v = np.frombuffer(data, "<f4", c * n * n, off + plane).reshape(c, n, n)
        mask = np.frombuffer(data, np.uint8, (n - 1) ** 2, off + 2 * plane).reshape(n - 1, n - 1)
        return cls(names, size, edge, step, np.array(cam_size), np.array(feather, np.float32), u, v, mask)


def build_grid_lut(canvas: AllSkyProjection, cameras: Sequence[Camera], step: int = 8,
                   feather_frac: float = 0.06) -> GridLUT:
    """Grid tables for ``canvas``. ``feather_frac`` matches ``lut.build_lut``."""
    if canvas.size % step:
        raise ValueError("canvas size must be a multiple of the grid step")
    if len(cameras) > MAX_CAMERAS:
        raise ValueError(f"at most {MAX_CAMERAS} cameras")
    n = canvas.size // step + 1
    g = np.arange(n, dtype=np.float64) * step
    gu, gv = np.meshgrid(g, g)
    # Nodes outside the dome circle still get directions (below the horizon),
    # so pixels near the rim interpolate between real values
    dirs, _ = canvas.pixel_to_world(gu, gv)
    c = len(cameras)
    u = np.full((c, n, n), np.nan, np.float32)
    v = np.full((c, n, n), np.nan, np.float32)
    mask = np.zeros((n - 1, n - 1), np.uint8)
    slack = 2.0
    for i, cam in enumerate(cameras):
        rays = np.einsum("...i,ij->...j", dirs, cam.rotation)
        cu, cv, ok = cam.lens.project(rays)
        u[i][ok] = cu[ok]
        v[i][ok] = cv[ok]
        corners = [(slice(0, -1), slice(0, -1)), (slice(0, -1), slice(1, None)),
                   (slice(1, None), slice(0, -1)), (slice(1, None), slice(1, None))]
        cu4 = np.stack([u[i][a, b] for a, b in corners])
        cv4 = np.stack([v[i][a, b] for a, b in corners])
        all_ok = np.all(np.isfinite(cu4), axis=0)
        with np.errstate(invalid="ignore"):
            hits = (all_ok & (cu4.max(0) >= -slack) & (cu4.min(0) <= cam.width - 1 + slack)
                    & (cv4.max(0) >= -slack) & (cv4.min(0) <= cam.height - 1 + slack))
        mask |= (hits.astype(np.uint8) << i)
    return GridLUT(
        names=[cam.name for cam in cameras], canvas_size=canvas.size,
        edge_zenith_deg=canvas.edge_zenith_deg, step=step,
        cam_size=np.array([(cam.width, cam.height) for cam in cameras]),
        feather_px=np.array([feather_frac * cam.width for cam in cameras], np.float32),
        u=u, v=v, mask=mask,
    )


def _bilinear_sample(img: np.ndarray, x: np.ndarray, y: np.ndarray) -> np.ndarray:
    """Sample at in-range float coords, clamping the 2x2 footprint at the border."""
    h, w = img.shape
    x0 = np.clip(np.floor(x).astype(np.int64), 0, w - 1)
    y0 = np.clip(np.floor(y).astype(np.int64), 0, h - 1)
    x1 = np.minimum(x0 + 1, w - 1)
    y1 = np.minimum(y0 + 1, h - 1)
    fx = (x - x0).astype(np.float32)
    fy = (y - y0).astype(np.float32)
    top = img[y0, x0] * (1 - fx) + img[y0, x1] * fx
    bot = img[y1, x0] * (1 - fx) + img[y1, x1] * fx
    return top * (1 - fy) + bot * fy


def source_coords(glut: GridLUT, cam: int, px: np.ndarray, py: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Interpolated source coordinates of output pixels (px, py) for camera ``cam``."""
    n, s = glut.nodes, glut.step
    gx = px.astype(np.float32) / s
    gy = py.astype(np.float32) / s
    i = np.clip(np.floor(gx).astype(np.int64), 0, n - 2)
    j = np.clip(np.floor(gy).astype(np.int64), 0, n - 2)
    fx, fy = gx - i, gy - j

    def bl(a):
        return (a[j, i] * (1 - fx) + a[j, i + 1] * fx) * (1 - fy) + (a[j + 1, i] * (1 - fx) + a[j + 1, i + 1] * fx) * fy

    return bl(glut.u[cam]), bl(glut.v[cam])


def grid_stitch(glut: GridLUT, images: Sequence[np.ndarray | None], fill: float = 0.0,
                out_div: int = 1) -> np.ndarray:
    """Blend single-channel camera images into the canvas exactly as the CUDA
    kernel does. ``images`` are in table camera order; None = camera missing.
    An image may be smaller than the table's camera size (binned capture, or a
    chroma plane): coordinates are scaled to it. ``out_div`` = 2 renders a
    half-size output (the I420 chroma planes)."""
    size = glut.canvas_size
    osz = size // out_div
    oy, ox = np.mgrid[0:osz, 0:osz].astype(np.float32)
    # Output pixel centres in canvas pixel coordinates
    px = (ox + 0.5) * out_div - 0.5
    py = (oy + 0.5) * out_div - 0.5
    half = size / 2
    rho = np.hypot((px + 0.5 - half) / half, (py + 0.5 - half) / half)
    n = glut.nodes
    cell = glut.mask[np.clip(np.floor(py / glut.step).astype(np.int64), 0, n - 2),
                     np.clip(np.floor(px / glut.step).astype(np.int64), 0, n - 2)]
    acc = np.zeros((osz, osz), np.float32)
    wsum = np.zeros_like(acc)
    fb_acc = np.zeros_like(acc)
    fb_n = np.zeros_like(acc)
    for c, img in enumerate(images):
        if img is None:
            continue
        sel = ((cell >> c) & 1).astype(bool) & (rho <= 1)
        if not sel.any():
            continue
        h, w = img.shape
        sx = w / glut.cam_size[c][0]
        sy = h / glut.cam_size[c][1]
        u, v = source_coords(glut, c, px[sel], py[sel])
        u = (u + 0.5) * sx - 0.5
        v = (v + 0.5) * sy - 0.5
        ok = np.isfinite(u) & np.isfinite(v) & (u >= 0) & (u <= w - 1) & (v >= 0) & (v <= h - 1)
        u, v = u[ok], v[ok]
        d = np.minimum(np.minimum(u, w - 1 - u), np.minimum(v, h - 1 - v))
        t = np.clip(d / (glut.feather_px[c] * sx), 0, 1)
        wt = t * t * (3 - 2 * t)
        s = _bilinear_sample(img.astype(np.float32), u, v)
        idx = tuple(a[ok] for a in np.nonzero(sel))
        np.add.at(acc, idx, wt * s)
        np.add.at(wsum, idx, wt)
        np.add.at(fb_acc, idx, np.where(wt > 0, 0, s))
        np.add.at(fb_n, idx, (wt <= 0).astype(np.float32))
    out = np.full((osz, osz), fill, np.float32)
    has_w = wsum > 0
    out[has_w] = acc[has_w] / wsum[has_w]
    edge_only = ~has_w & (fb_n > 0)
    out[edge_only] = fb_acc[edge_only] / fb_n[edge_only]
    return out
