"""Intrinsic lens calibration from ChArUco board images (handoff §5.2).

Fits both lens models in ``camera.py`` to the same detections:

* ``FisheyeLens`` with ``cv2.fisheye`` (Kannala-Brandt, k1..k4)
* ``PinholeLens`` with ``cv2.calibrateCamera`` (radial k1..k3, no tangential)

and reports which fits better, the field of view each implies, and how much
of the frame the detections covered. OpenCV is only needed for detecting and
fitting; loading a saved calibration needs nothing but YAML.

JPEG decoding here happens on the CPU, which is fine for a few dozen
calibration stills; the live pipeline never does (handoff §9).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import yaml

from .camera import FisheyeLens, Lens, PinholeLens


@dataclass(frozen=True)
class BoardSpec:
    """The printed target. Defaults fit US Letter landscape at 100% scale."""

    squares_x: int = 10
    squares_y: int = 7
    square_mm: float = 25.0
    marker_frac: float = 0.75
    dictionary: str = "DICT_5X5_100"

    @property
    def marker_mm(self) -> float:
        return self.square_mm * self.marker_frac

    def cv_board(self):
        import cv2

        d = cv2.aruco.getPredefinedDictionary(getattr(cv2.aruco, self.dictionary))
        return cv2.aruco.CharucoBoard((self.squares_x, self.squares_y), self.square_mm, self.marker_mm, d)

    def image(self, px_per_mm: float) -> np.ndarray:
        """The board alone, at the given resolution, no margin (uint8, white = 255)."""
        w = round(self.squares_x * self.square_mm * px_per_mm)
        h = round(self.squares_y * self.square_mm * px_per_mm)
        return self.cv_board().generateImage((w, h), marginSize=0, borderBits=1)

    def corners_mm(self) -> np.ndarray:
        """Board-frame (x right, y down, z = 0) coordinates of every ChArUco corner, by id."""
        return np.asarray(self.cv_board().getChessboardCorners(), dtype=np.float64)


@dataclass
class Detections:
    width: int
    height: int
    object_points: list[np.ndarray] = field(default_factory=list)  # (n, 3) mm, per view
    image_points: list[np.ndarray] = field(default_factory=list)   # (n, 2) px, per view
    names: list[str] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)

    @property
    def views(self) -> int:
        return len(self.image_points)

    def coverage(self, cols: int = 8, rows: int = 5) -> float:
        """Fraction of a cols x rows grid over the frame that has any detected corner."""
        hit = np.zeros((rows, cols), bool)
        for pts in self.image_points:
            c = np.clip((pts[:, 0] / self.width * cols).astype(int), 0, cols - 1)
            r = np.clip((pts[:, 1] / self.height * rows).astype(int), 0, rows - 1)
            hit[r, c] = True
        return float(hit.mean())


def detect(images: list[tuple[str, np.ndarray]], board: BoardSpec, min_corners: int = 12) -> Detections:
    """Find ChArUco corners in grayscale images (name, array). All images must be the same size."""
    import cv2

    detector = cv2.aruco.CharucoDetector(board.cv_board())
    all_obj = board.corners_mm()
    h, w = images[0][1].shape[:2]
    det = Detections(w, h)
    for name, img in images:
        if img.shape[:2] != (h, w):
            raise ValueError(f"{name}: size {img.shape[1]}x{img.shape[0]}, expected {w}x{h}")
        corners, ids, _, _ = detector.detectBoard(img)
        if ids is None or len(ids) < min_corners:
            det.rejected.append(name)
            continue
        det.object_points.append(all_obj[ids.ravel()])
        det.image_points.append(corners.reshape(-1, 2).astype(np.float64))
        det.names.append(name)
    return det


def load_images(paths: list[Path]) -> list[tuple[str, np.ndarray]]:
    import cv2

    out = []
    for p in sorted(paths):
        img = cv2.imread(str(p), cv2.IMREAD_GRAYSCALE)
        if img is not None:
            out.append((p.name, img))
    return out


@dataclass
class Fit:
    lens: Lens
    rms_px: float
    views_used: int
    dropped: list[str] = field(default_factory=list)

    @property
    def model(self) -> str:
        return "fisheye" if isinstance(self.lens, FisheyeLens) else "pinhole"


def _criteria():
    import cv2

    return (cv2.TERM_CRITERIA_COUNT + cv2.TERM_CRITERIA_EPS, 200, 1e-10)


def _fisheye_flags(*names: str) -> int:
    """OpenCV 4 keeps the fisheye flags in cv2.fisheye; OpenCV 5 moved them to cv2."""
    import cv2

    flags = 0
    for n in names:
        flags |= getattr(cv2.fisheye, n, None) if hasattr(cv2.fisheye, n) else getattr(cv2, n)
    return flags


def fit_fisheye(det: Detections, guess_hfov_deg: float) -> Fit:
    """cv2.fisheye.calibrate, dropping views it calls ill-conditioned until it converges."""
    import cv2

    g = FisheyeLens.from_hfov(det.width, det.height, guess_hfov_deg)
    flags = _fisheye_flags("CALIB_RECOMPUTE_EXTRINSIC", "CALIB_FIX_SKEW", "CALIB_USE_INTRINSIC_GUESS",
                           "CALIB_CHECK_COND")
    keep = list(range(det.views))
    dropped: list[str] = []
    while len(keep) >= 5:
        # (1, n, 3) / (1, n, 2): the only layout both OpenCV 4 and 5 accept here
        obj = [det.object_points[i].reshape(1, -1, 3) for i in keep]
        img = [det.image_points[i].reshape(1, -1, 2) for i in keep]
        K = np.array([[g.fx, 0, g.cx], [0, g.fy, g.cy], [0, 0, 1]], dtype=np.float64)
        D = np.zeros((4, 1))
        try:
            rms, K, D, _, _ = cv2.fisheye.calibrate(obj, img, (det.width, det.height), K, D, flags=flags,
                                                     criteria=_criteria())
        except cv2.error as e:
            # "... Ill-conditioned matrix for input array 12": drop that view and retry
            msg = str(e)
            idx = [int(t) for t in msg.replace(":", " ").split() if t.isdigit()]
            bad = idx[-1] if idx and idx[-1] < len(keep) else len(keep) - 1
            dropped.append(det.names[keep.pop(bad)])
            continue
        lens = FisheyeLens(det.width, det.height, K[0, 0], K[1, 1], K[0, 2], K[1, 2], tuple(float(x) for x in D.ravel()))
        return Fit(lens, float(rms), len(keep), dropped)
    raise RuntimeError("fisheye fit failed: too few usable views")


def fit_pinhole(det: Detections, guess_hfov_deg: float) -> Fit:
    """cv2.calibrateCamera with radial k1..k3 only, matching PinholeLens."""
    import cv2

    # A rectilinear lens can't reach 180 deg; cap the starting guess
    g = PinholeLens.from_hfov(det.width, det.height, min(guess_hfov_deg, 150.0))
    K = np.array([[g.fx, 0, g.cx], [0, g.fy, g.cy], [0, 0, 1]], dtype=np.float64)
    flags = cv2.CALIB_USE_INTRINSIC_GUESS | cv2.CALIB_ZERO_TANGENT_DIST
    obj = [p.astype(np.float32) for p in det.object_points]
    img = [p.astype(np.float32) for p in det.image_points]
    rms, K, dist, _, _ = cv2.calibrateCamera(obj, img, (det.width, det.height), K, None, flags=flags,
                                             criteria=_criteria())
    d = dist.ravel()
    lens = PinholeLens(det.width, det.height, K[0, 0], K[1, 1], K[0, 2], K[1, 2], (float(d[0]), float(d[1]), float(d[4])))
    return Fit(lens, float(rms), det.views)


def fov_deg(lens: Lens) -> dict[str, float]:
    """Horizontal, vertical and diagonal field of view across the image's outer edges."""
    w, h = lens.width, lens.height
    l, r, t, b = -0.5, w - 0.5, -0.5, h - 0.5

    def ang(p, q):
        a, b2 = lens.unproject(np.array([p[0], q[0]]), np.array([p[1], q[1]]))
        return float(np.degrees(np.arccos(np.clip(np.dot(a, b2), -1, 1))))

    fov = {"hfov_deg": ang((l, lens.cy), (r, lens.cy)), "vfov_deg": ang((lens.cx, t), (lens.cx, b)),
           "dfov_deg": ang((l, t), (r, b))}
    # A rectilinear fit to a very wide lens can fold over before the corners;
    # a diagonal smaller than the sides means that happened
    if fov["dfov_deg"] < max(fov["hfov_deg"], fov["vfov_deg"]):
        fov["dfov_deg"] = float("nan")
    return fov


def max_theta_seen_deg(lens: Lens, det: Detections) -> float:
    """Largest angle from the optical axis among the detected corners: the fit is
    only trustworthy out to about here."""
    pts = np.concatenate(det.image_points)
    rays = lens.unproject(pts[:, 0], pts[:, 1])
    return float(np.degrees(np.arccos(np.clip(rays[:, 2], -1, 1))).max())


def lens_to_dict(lens: Lens) -> dict:
    model = "fisheye" if isinstance(lens, FisheyeLens) else "pinhole"
    return {"model": model, "width": lens.width, "height": lens.height, "fx": float(lens.fx), "fy": float(lens.fy),
            "cx": float(lens.cx), "cy": float(lens.cy), "k": [float(x) for x in lens.k]}


def lens_from_dict(d: dict) -> Lens:
    common = dict(width=int(d["width"]), height=int(d["height"]), fx=float(d["fx"]), fy=float(d["fy"]),
                  cx=float(d["cx"]), cy=float(d["cy"]))
    if d["model"] == "fisheye":
        return FisheyeLens(**common, k=tuple(float(x) for x in d["k"]))
    if d["model"] == "pinhole":
        return PinholeLens(**common, k=tuple(float(x) for x in d["k"]))
    raise ValueError(f"unknown lens model {d['model']!r}")


def save_calibration(path: Path, camera: str, best: Fit, other: Fit, det: Detections, board: BoardSpec) -> dict:
    doc = {
        "camera": camera,
        "date_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        **lens_to_dict(best.lens),
        "rms_px": round(best.rms_px, 4),
        **{k: round(v, 2) for k, v in fov_deg(best.lens).items()},
        "max_theta_seen_deg": round(max_theta_seen_deg(best.lens, det), 1),
        "views": best.views_used,
        "coverage": round(det.coverage(), 2),
        "board": {"squares": [board.squares_x, board.squares_y], "square_mm": board.square_mm,
                  "dictionary": board.dictionary},
        "other_model": {"model": other.model, "rms_px": round(other.rms_px, 4),
                        **{k: round(v, 2) for k, v in fov_deg(other.lens).items()}},
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# Written by tools/calibrate_intrinsics.py\n" + yaml.safe_dump(doc, sort_keys=False))
    return doc


def load_lens(path: Path) -> Lens:
    return lens_from_dict(yaml.safe_load(Path(path).read_text()))
