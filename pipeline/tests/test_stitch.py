"""End-to-end stitch checks on a simulated sky (handoff §7: stars in overlap
zones must be single points, seams invisible)."""

import numpy as np
import pytest

from skydome_pipeline.camera import Camera, FisheyeLens, PinholeLens
from skydome_pipeline.geometry import camera_rotation
from skydome_pipeline.lut import build_lut, stitch
from skydome_pipeline.projection import AllSkyProjection
from skydome_pipeline.simulate import SkyScene, find_peaks, random_stars, render_camera, render_canvas

# Small sizes keep the test fast; the geometry is resolution independent.
SCALE = 0.125      # 480 x 270 camera images
DOME = 512


def rig(lens_cls, side_alt=35.0, perturb=None):  # as built, DECISIONS.md D-009
    lens = lens_cls.from_hfov(3840, 2160, 120)
    perturb = perturb or {}
    cams = [Camera("zenith", lens, camera_rotation(0, 90))]
    for name, az in [("north", 0), ("east", 90), ("south", 180), ("west", 270)]:
        d = perturb.get(name, (0, 0, 0))
        cams.append(Camera(name, lens, camera_rotation(az + d[0], side_alt + d[1], d[2])))
    return [c.scaled(SCALE) for c in cams]


@pytest.fixture(scope="module")
def dome():
    return AllSkyProjection(size=DOME, edge_zenith_deg=90)


@pytest.mark.parametrize("lens_cls", [FisheyeLens, PinholeLens])
def test_smooth_scene_has_no_seams(dome, lens_cls):
    cams = rig(lens_cls)
    lut = build_lut(dome, cams)
    scene = SkyScene(grid_step_deg=15, grid_width_deg=1.5)
    out = stitch(lut, [render_camera(c, scene) for c in cams])
    truth = render_canvas(dome, scene)
    # Compare away from the canvas rim; differences come only from resampling
    m = lut.covered & (lut.overlap_count >= 1)
    err = np.abs(out - truth)[m]
    assert np.percentile(err, 99) < 0.08
    assert err.mean() < 0.01


@pytest.mark.parametrize("lens_cls", [FisheyeLens, PinholeLens])
def test_stars_are_single_points(dome, lens_cls):
    cams = rig(lens_cls)
    lut = build_lut(dome, cams)
    stars = random_stars(400, seed=3, min_alt_deg=0)
    scene = SkyScene(stars=stars, glow=False)
    out = stitch(lut, [render_camera(c, scene, psf_sigma_px=1.0) for c in cams])
    peaks = find_peaks(out, threshold=0.15)
    su, sv = dome.world_to_pixel(stars.dirs)
    cu, cv = np.round(su).astype(int), np.round(sv).astype(int)
    inb = (cu >= 0) & (cu < DOME) & (cv >= 0) & (cv < DOME)
    seen = np.zeros(len(stars), bool)
    seen[inb] = lut.covered[cv[inb], cu[inb]]
    # Skip stars close to each other: they legitimately merge at this resolution
    d = np.hypot(su[:, None] - su[None], sv[:, None] - sv[None]) + np.eye(len(su)) * 1e9
    isolated = d.min(axis=1) > 8
    checked = 0
    for i in np.nonzero(seen & isolated)[0]:
        near = np.hypot(peaks[:, 0] - su[i], peaks[:, 1] - sv[i])
        assert (near < 6).sum() == 1, f"star {i} found {(near < 6).sum()} times"
        assert near.min() < 0.5
        checked += 1
    assert checked > 150
