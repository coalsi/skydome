"""Camera orientations recovered from overlap matches (overlap_align.py)."""

import numpy as np
import pytest

from skydome_pipeline.camera import Camera, FisheyeLens
from skydome_pipeline.config import calibrated_cameras, load_extrinsics
from skydome_pipeline.geometry import angle_between_deg, camera_rotation, rot_x, rot_y, rot_z
from skydome_pipeline.overlap_align import (
    PairMatches, frame_centered, frame_to_nominal, ransac_rotation, solve)

LENS = FisheyeLens.from_hfov(3840, 2160, 120)


def nominal_rig():
    cams = [Camera("zenith", LENS, camera_rotation(0, 90))]
    for name, az in [("north", 0), ("east", 90), ("south", 180), ("west", 270)]:
        cams.append(Camera(name, LENS, camera_rotation(az, 30)))
    return cams


def matches(cams, n_dirs=4000, noise_deg=0.03, outlier_frac=0.3, seed=0):
    """Matched rays for every pair that sees the same random world points."""
    rng = np.random.default_rng(seed)
    d = rng.normal(size=(n_dirs, 3))
    d /= np.linalg.norm(d, axis=1, keepdims=True)
    pairs = []
    for i in range(len(cams)):
        for j in range(i + 1, len(cams)):
            _, _, ok_i = cams[i].world_to_pixel(d)
            _, _, ok_j = cams[j].world_to_pixel(d)
            both = d[ok_i & ok_j]
            if len(both) < 10:
                continue

            def ray(cam, w):
                r = w @ cam.rotation          # camera frame
                r = r + rng.normal(scale=np.radians(noise_deg), size=r.shape)
                return r / np.linalg.norm(r, axis=1, keepdims=True)

            a, b = ray(cams[i], both), ray(cams[j], both)
            bad = rng.random(len(b)) < outlier_frac
            b[bad] = rng.permutation(b[bad])  # wrong matches
            pairs.append(PairMatches(i, j, a, b))
    return pairs


def perturbed(seed=1, deg=4.0):
    rng = np.random.default_rng(seed)
    out = []
    for c in nominal_rig():
        d = rng.uniform(-deg, deg, 3)
        out.append(Camera(c.name, LENS, c.rotation @ rot_x(d[0]) @ rot_y(d[1]) @ rot_z(d[2])))
    return out


def test_recovers_relative_rotations():
    truth = perturbed()
    pairs = [ransac_rotation(p) for p in matches(truth)]
    al = solve(pairs, len(truth), ref=0)
    assert all(r is not None for r in al.rotations)
    # Fix the gauge: compare with the reference camera's true rotation applied
    g = truth[0].rotation
    for r, cam in zip(al.rotations, truth):
        err = angle_between_deg((g @ r)[:, 2], cam.rotation[:, 2])
        assert err < 0.02
        assert np.abs(g @ r - cam.rotation).max() < 1e-3
    assert max(al.residual_deg) < 0.1


def test_frame_to_nominal_lands_near_the_mount_pointing():
    truth = perturbed(deg=2.0)
    pairs = [ransac_rotation(p) for p in matches(truth)]
    al = solve(pairs, len(truth))
    placed = frame_to_nominal(al.rotations, [c.rotation for c in nominal_rig()])
    # Each camera within the 2° perturbation of where the mount says, and
    # the set as a whole exactly where the truth is up to that fit
    for r, nom, t in zip(placed, nominal_rig(), truth):
        assert angle_between_deg(r[:, 2], nom.rotation[:, 2]) < 4.0
    rel_truth = truth[1].rotation.T @ truth[2].rotation
    rel = placed[1].T @ placed[2]
    assert np.abs(rel - rel_truth).max() < 1e-3


def test_frame_centered_points_the_mean_axis_up():
    truth = perturbed()
    rots = frame_centered([c.rotation @ rot_x(30) for c in truth])
    m = np.mean([r[:, 2] for r in rots], axis=0)
    assert angle_between_deg(m / np.linalg.norm(m), np.array([0, 0, 1.0])) < 1e-6


def test_unconnected_camera_is_left_out():
    truth = perturbed()
    pairs = [ransac_rotation(p) for p in matches(truth) if 4 not in (p.i, p.j)]
    al = solve(pairs, len(truth))
    assert al.rotations[4] is None and all(r is not None for r in al.rotations[:4])


def test_extrinsics_file_overrides_the_nominal_pointing(tmp_path):
    import shutil
    from pathlib import Path

    cfg = tmp_path / "config"
    shutil.copytree(Path(__file__).resolve().parents[2] / "config", cfg,
                    ignore=shutil.ignore_patterns("calibration"))
    r = rot_z(10) @ camera_rotation(0, 90)
    (cfg / "calibration").mkdir()
    (cfg / "calibration" / "extrinsics.yaml").write_text(
        "cameras:\n  zenith:\n    rotation: " + str(r.tolist()) + "\n")
    assert np.allclose(load_extrinsics(cfg)["zenith"], r)
    cams = {c.name: c for c in calibrated_cameras(cfg)}
    assert np.allclose(cams["zenith"].rotation, r)
    assert np.allclose(cams["north"].rotation, camera_rotation(0, 30))


def test_bad_rotation_is_rejected(tmp_path):
    (tmp_path / "calibration").mkdir()
    (tmp_path / "calibration" / "extrinsics.yaml").write_text(
        "cameras:\n  zenith:\n    rotation: [[1, 0, 0], [0, 2, 0], [0, 0, 1]]\n")
    with pytest.raises(ValueError):
        load_extrinsics(tmp_path)


def test_shared_hfov_is_recovered():
    from skydome_pipeline.overlap_align import PixelPairs, fit_shared_hfov

    true_lens = FisheyeLens.from_hfov(3840, 2160, 112)
    cams = [Camera(c.name, true_lens, c.rotation) for c in perturbed()]
    rng = np.random.default_rng(3)
    d = rng.normal(size=(6000, 3))
    d /= np.linalg.norm(d, axis=1, keepdims=True)
    pp = []
    for i in range(5):
        for j in range(i + 1, 5):
            ui, vi, oki = cams[i].world_to_pixel(d)
            uj, vj, okj = cams[j].world_to_pixel(d)
            m = oki & okj
            if m.sum() >= 20:
                noise = rng.normal(scale=0.5, size=(m.sum(), 4))
                pp.append(PixelPairs(i, j, np.c_[ui[m], vi[m]] + noise[:, :2], np.c_[uj[m], vj[m]] + noise[:, 2:]))
    h = fit_shared_hfov(pp, lambda h: FisheyeLens.from_hfov(3840, 2160, h), 5, 95, 135)
    assert abs(h - 112) < 1.0
