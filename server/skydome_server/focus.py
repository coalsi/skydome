"""Focusing the lens by eye (web/public/focus/): how sharp the camera's own
frame is, every pixel of it, before stitching. Three measures, since a lens is
focused on whatever's there: the size of the one star in the spot picked on the
page and of the stars across the whole frame (night only, lower is better),
and the spot's sharpness (any scene, higher is better).
"""

from __future__ import annotations

import base64
import io
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

WINDOW = 45              # px either side of a star for its size: wide enough for one well out of focus
MAX_STARS = 40           # the brightest ones are enough
MIN_STARS = 3            # fewer than this and it's not stars
NIGHT_EXPOSURE_US = 200_000   # a shorter frame has no stars in it: what it finds is edges
SATURATED = 0.95 * 65535


def read_pgm(path: Path) -> np.ndarray:
    """A 16-bit binary PGM, as skydome-live saves the camera's luminance."""
    data = Path(path).read_bytes()
    parts, pos = [], 0
    while len(parts) < 4:
        end = pos
        while not data[end:end + 1].isspace():
            end += 1
        parts.append(data[pos:end])
        pos = end + 1
    if parts[0] != b"P5" or int(parts[3]) != 65535:
        raise ValueError(f"{path} isn't a 16-bit PGM")
    w, h = int(parts[1]), int(parts[2])
    return np.frombuffer(data, ">u2", w * h, pos).reshape(h, w).astype(np.float32)


def sharpness(img: np.ndarray) -> float:
    """Edge energy for the spot's brightness, so a change of exposure hardly
    moves it. The blur keeps pixel noise out of it: at night that's most of what's there."""
    soft = cv2.GaussianBlur(img, (0, 0), 1.5)
    gx, gy = cv2.Sobel(soft, cv2.CV_32F, 1, 0), cv2.Sobel(soft, cv2.CV_32F, 0, 1)
    return float(1000 * np.mean(gx * gx + gy * gy) / max(float(soft.mean()) ** 2, 1.0))


def star_light(patch: np.ndarray) -> np.ndarray:
    """A star's own light: the background, measured round the patch's edge,
    taken off, and what's left of the noise out in the patch set to nothing."""
    ys, xs = np.indices(patch.shape)
    r = np.hypot(xs - patch.shape[1] // 2, ys - patch.shape[0] // 2)
    edge = patch[r > 0.8 * patch.shape[0] / 2]
    bg = float(np.median(edge))
    noise = 1.4826 * float(np.median(np.abs(edge - bg)))
    light = patch - bg
    light[light < 3 * noise] = 0
    return light


def half_flux_diameter(light: np.ndarray) -> float:
    """The diameter holding half of a star's light (px): a blurred star's
    grows, however bright it is."""
    total = float(light.sum())
    if total <= 0:
        return 0.0
    ys, xs = np.indices(light.shape)
    cx, cy = float((xs * light).sum() / total), float((ys * light).sum() / total)
    r = np.hypot(xs - cx, ys - cy).ravel()
    order = np.argsort(r)
    inside = np.cumsum(light.ravel()[order])
    return 2 * float(r[order][np.searchsorted(inside, total / 2)])


def round_enough(light: np.ndarray) -> bool:
    """A star's light spreads evenly every way; an edge's or a streak's doesn't."""
    total = float(light.sum())
    if total <= 0:
        return False
    ys, xs = np.indices(light.shape)
    dx, dy = xs - (xs * light).sum() / total, ys - (ys * light).sum() / total
    xx, yy, xy = (dx * dx * light).sum(), (dy * dy * light).sum(), (dx * dy * light).sum()
    low, high = np.linalg.eigvalsh([[xx, xy], [xy, yy]])
    return bool(low > 0.55 * high)


def hot_pixel(img: np.ndarray, x: int, y: int) -> bool:
    """A hot pixel stands alone: nothing round it comes near it."""
    near = np.sort(img[y - 2:y + 3, x - 2:x + 3], axis=None)
    return near[-1] - near[12] > 3 * max(near[-2] - near[12], 1.0)


def find_stars(img: np.ndarray, saturated: bool = False) -> list[tuple[int, int, float]]:
    """The brightest clean stars in the frame, brightest first: (x, y, half-flux
    diameter). An out-of-focus star is a wide faint disc, so they're looked for
    in a blurred copy, and whatever isn't round (a roofline, cloud) is left out."""
    soft = cv2.GaussianBlur(img, (0, 0), 2.5)
    fine = cv2.GaussianBlur(img, (0, 0), 1.0)
    diff = soft - cv2.blur(soft, (121, 121))
    noise = 1.4826 * float(np.median(np.abs(diff[::8, ::8] - np.median(diff[::8, ::8]))))
    peaks = (diff > max(8 * noise, 1.0)) & (soft >= cv2.dilate(soft, np.ones((21, 21), np.uint8)))
    if not saturated:
        peaks &= img < SATURATED
    peaks[:WINDOW] = peaks[-WINDOW:] = False
    peaks[:, :WINDOW] = peaks[:, -WINDOW:] = False
    ys, xs = np.nonzero(peaks)
    order = np.argsort(-diff[ys, xs])[:20 * MAX_STARS]
    found, seen = [], []
    for y, x in zip(ys[order], xs[order]):
        if any(abs(x - sx) < WINDOW and abs(y - sy) < WINDOW for sx, sy in seen):
            continue    # the same star: a wide flat one has more than one peak
        seen.append((x, y))
        light = star_light(fine[y - WINDOW:y + WINDOW + 1, x - WINDOW:x + WINDOW + 1])
        if round_enough(light) and not hot_pixel(img, x, y):
            size = half_flux_diameter(light)
            if size > 1.0:
                found.append((int(x), int(y), size))
    return found[:MAX_STARS]


def star_sizes(img: np.ndarray) -> list[float]:
    return [s for _, _, s in find_stars(img)]


def spot_star(img: np.ndarray, x0: int, y0: int, size: int) -> dict | None:
    """The brightest star in the spot, up close: focusing on one star is
    steadier than the frame's average, and shows the star itself shrink."""
    h, w = img.shape
    left, top = max(x0 - WINDOW, 0), max(y0 - WINDOW, 0)
    near = img[top:min(y0 + size + WINDOW, h), left:min(x0 + size + WINDOW, w)]
    for x, y, star in find_stars(near, saturated=True):
        fx, fy = x + left, y + top
        if x0 <= fx < x0 + size and y0 <= fy < y0 + size:
            close = img[max(fy - 40, 0):fy + 41, max(fx - 40, 0):fx + 41]
            low, high = np.percentile(close, 5), float(close.max())
            return {"x": fx, "y": fy, "size": round(star, 2), "saturated": bool(high >= SATURATED),
                    "crop": _jpeg(close, float(low), high, 90)}
    return None


def _jpeg(img: np.ndarray, low: float, high: float, quality: int = 80) -> str:
    """As a data URL, gamma-lifted for the eye."""
    v = np.clip((img - low) / max(high - low, 1.0), 0, 1) ** (1 / 2.2)
    buf = io.BytesIO()
    Image.fromarray((v * 255).astype(np.uint8)).save(buf, "JPEG", quality=quality)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def measure(img: np.ndarray, x: float, y: float, size: int, exposure_us: float | None) -> dict:
    """The page's picture and numbers. x and y (0..1) are the spot's centre in the frame."""
    h, w = img.shape
    size = min(size, h, w)
    x0 = int(np.clip(x * w - size / 2, 0, w - size))
    y0 = int(np.clip(y * h - size / 2, 0, h - size))
    spot = img[y0:y0 + size, x0:x0 + size]
    sizes = star_sizes(img) if (exposure_us or 0) >= NIGHT_EXPOSURE_US else []
    enough = len(sizes) >= MIN_STARS
    scale = max(w // 960, 1)
    whole = cv2.resize(img, (w // scale, h // scale), interpolation=cv2.INTER_AREA)
    low, high = np.percentile(whole, [1, 99.8])
    spot_low, spot_high = np.percentile(spot, [1, 99.9])
    return {"width": w, "height": h, "exposure_us": exposure_us,
            "spot": {"x": x0, "y": y0, "size": size},
            "star": spot_star(img, x0, y0, size) if (exposure_us or 0) >= NIGHT_EXPOSURE_US else None,
            "sharpness": round(sharpness(spot), 2),
            "stars": len(sizes), "star_size": round(float(np.median(sizes)), 2) if enough else None,
            "whole": _jpeg(whole, float(low), float(high), 70), "crop": _jpeg(spot, float(spot_low), float(spot_high))}
