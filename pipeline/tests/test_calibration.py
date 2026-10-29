"""Intrinsic calibration recovers a known lens from rendered ChArUco views."""

import numpy as np
import pytest

cv2 = pytest.importorskip("cv2")

from skydome_pipeline.calibration import (  # noqa: E402
    BoardSpec, detect, fit_fisheye, fit_pinhole, fov_deg, lens_from_dict, lens_to_dict, save_calibration, load_lens)
from skydome_pipeline.camera import FisheyeLens, PinholeLens  # noqa: E402
from skydome_pipeline.geometry import rot_x, rot_y, rot_z  # noqa: E402

# A sixth of the real 3840 x 2160 frame keeps rendering fast
W, H = 640, 360
BOARD = BoardSpec()
PX_PER_MM = 8.0
TEX = BOARD.image(PX_PER_MM).astype(np.float32)


def render(lens, R, t, ss=2):
    """Board seen by ``lens`` with board-to-camera rotation R (columns = board
    axes) and centre t (mm). Supersampled ss x ss, grey background."""
    u, v = np.meshgrid((np.arange(W * ss) + 0.5) / ss - 0.5, (np.arange(H * ss) + 0.5) / ss - 0.5)
    rays = lens.unproject(u, v)
    n = R[:, 2]
    denom = rays @ n
    s = np.where(np.abs(denom) > 1e-9, (t @ n) / np.where(np.abs(denom) > 1e-9, denom, 1), -1)
    hit = (s > 0) & (rays[..., 2] > 0)
    if isinstance(lens, FisheyeLens):
        hit &= np.degrees(np.arccos(np.clip(rays[..., 2], -1, 1))) < lens.max_theta_deg
    q = (rays * s[..., None] - t) @ R           # board frame, mm, origin at centre
    bw, bh = BOARD.squares_x * BOARD.square_mm, BOARD.squares_y * BOARD.square_mm
    x = (q[..., 0] + bw / 2) * PX_PER_MM
    y = (q[..., 1] + bh / 2) * PX_PER_MM
    inside = hit & (x >= 0) & (y >= 0) & (x < TEX.shape[1] - 1) & (y < TEX.shape[0] - 1)
    img = np.full(u.shape, 140.0, np.float32)
    xi, yi = x[inside].astype(int), y[inside].astype(int)
    img[inside] = TEX[yi, xi]
    img = img.reshape(H, ss, W, ss).mean(axis=(1, 3))
    return np.clip(img, 0, 255).astype(np.uint8)


def views(lens, n=24, seed=0):
    """Board poses spread across the whole field, 0.23-0.4 m away (squares ~20-35 px, like the real 4K frame at ~1 m), tilted up to ~35 deg."""
    rng = np.random.default_rng(seed)
    out = []
    grid = [(gx, gy) for gy in np.linspace(0.12, 0.88, 4) for gx in np.linspace(0.08, 0.92, 6)]
    for i in range(n):
        gx, gy = grid[i % len(grid)]
        d = lens.unproject(np.array(gx * W), np.array(gy * H))
        dist = rng.uniform(230, 400)
        t = d * dist
        # Face the camera (board z axis pointing away from it, so the printed
        # side shows unmirrored), then tilt
        n = d / np.linalg.norm(d)
        x = np.cross([0, 1, 0], n); x /= np.linalg.norm(x)
        y = np.cross(n, x)
        R = np.stack([x, y, n], axis=1) @ rot_x(rng.uniform(-35, 35)) @ rot_y(rng.uniform(-35, 35)) @ rot_z(rng.uniform(-20, 20))
        if R[:, 2] @ d < 0:   # keep the printed side toward the camera
            R = R @ rot_x(180)
        out.append((f"view{i:02d}", render(lens, R, t)))
    return out


@pytest.fixture(scope="module")
def fisheye_case():
    base = FisheyeLens.from_hfov(W, H, 112)      # say the "120 deg" was really diagonal
    truth = FisheyeLens(W, H, base.fx * 1.004, base.fy, base.cx + 3.2, base.cy - 2.1, (0.03, -0.012, 0.004, -0.001))
    return truth, detect(views(truth), BOARD)


@pytest.fixture(scope="module")
def pinhole_case():
    base = PinholeLens.from_hfov(W, H, 100)
    truth = PinholeLens(W, H, base.fx, base.fy * 1.002, base.cx - 1.5, base.cy + 2.0, (-0.02, 0.004, 0.0))
    return truth, detect(views(truth, seed=1), BOARD)


def test_board_detected_across_the_frame(fisheye_case):
    _, det = fisheye_case
    assert det.views >= 20
    assert det.coverage() >= 0.8


def test_fisheye_lens_recovered(fisheye_case):
    truth, det = fisheye_case
    fe, ph = fit_fisheye(det, 120), fit_pinhole(det, 120)
    assert fe.rms_px < 0.3
    assert fe.rms_px < ph.rms_px                 # the right model wins
    assert abs(fe.lens.fx - truth.fx) / truth.fx < 0.01
    assert abs(fe.lens.cx - truth.cx) < 1.5 and abs(fe.lens.cy - truth.cy) < 1.5
    got, want = fov_deg(fe.lens), fov_deg(truth)
    assert abs(got["hfov_deg"] - want["hfov_deg"]) < 0.5
    assert abs(got["dfov_deg"] - want["dfov_deg"]) < 1.0


def test_pinhole_lens_recovered(pinhole_case):
    truth, det = pinhole_case
    ph = fit_pinhole(det, 120)
    assert ph.rms_px < 0.3
    assert abs(ph.lens.fx - truth.fx) / truth.fx < 0.01
    assert abs(fov_deg(ph.lens)["hfov_deg"] - fov_deg(truth)["hfov_deg"]) < 0.5


def test_saved_calibration_round_trips(fisheye_case, tmp_path):
    _, det = fisheye_case
    fe, ph = fit_fisheye(det, 120), fit_pinhole(det, 120)
    doc = save_calibration(tmp_path / "zenith.yaml", "zenith", fe, ph, det, BOARD)
    assert doc["model"] == "fisheye" and doc["other_model"]["model"] == "pinhole"
    back = load_lens(tmp_path / "zenith.yaml")
    assert back == lens_from_dict(lens_to_dict(fe.lens))
    u, v = np.array([10.0, 320.0, 600.0]), np.array([20.0, 180.0, 340.0])
    assert np.allclose(back.unproject(u, v), fe.lens.unproject(u, v))
