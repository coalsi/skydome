"""The GPU stitch tables and their NumPy twin agree with the full-resolution
reference stitcher (DECISIONS.md D-005, D-016)."""

import numpy as np
import pytest

from skydome_pipeline.camera import FisheyeLens, PinholeLens
from skydome_pipeline.gpu_lut import GridLUT, build_grid_lut, grid_stitch, source_coords
from skydome_pipeline.lut import build_lut, stitch
from skydome_pipeline.projection import AllSkyProjection
from skydome_pipeline.simulate import SkyScene, find_peaks, random_stars, render_camera

from test_stitch import DOME, rig


@pytest.fixture(scope="module")
def dome():
    return AllSkyProjection(size=DOME)


# The test rig is 1/8 scale (test_stitch.SCALE), so a 1 px grid step here is the
# production 8 px step at full size
STEP = 1


@pytest.fixture(scope="module", params=[FisheyeLens, PinholeLens])
def setup(request, dome):
    cams = rig(request.param)
    return cams, build_grid_lut(dome, cams, step=STEP), build_lut(dome, cams)


def test_save_load_round_trip(setup, tmp_path):
    _, glut, _ = setup
    glut.save(tmp_path / "t.lut")
    back = GridLUT.load(tmp_path / "t.lut")
    assert back.names == glut.names and back.step == glut.step and back.canvas_size == glut.canvas_size
    assert np.array_equal(back.cam_size, glut.cam_size)
    assert np.array_equal(back.mask, glut.mask)
    assert np.array_equal(back.u, glut.u, equal_nan=True)


def test_grid_coords_match_exact_projection(setup, dome):
    cams, glut, _ = setup
    rng = np.random.default_rng(0)
    px, py = rng.uniform(0, DOME - 1, (2, 20000))
    dirs, inside = dome.pixel_to_world(px, py)
    for c, cam in enumerate(cams):
        u, v, ok = cam.world_to_pixel(dirs)
        gu, gv = source_coords(glut, c, px, py)
        m = ok & inside
        assert np.nanmax(np.hypot(gu - u, gv - v)[m]) < 0.05


def test_every_covered_pixel_is_in_some_cell_mask(setup):
    _, glut, lut = setup
    n = glut.nodes
    py, px = np.nonzero(lut.covered)
    cell = glut.mask[np.minimum(py // glut.step, n - 2), np.minimum(px // glut.step, n - 2)]
    for c in range(len(glut.names)):
        uses = lut.weight[c][py, px] > 0
        assert np.all((cell[uses] >> c) & 1), f"camera {c} missing from the mask where it has weight"


def test_twin_matches_full_resolution_stitch(setup):
    cams, glut, lut = setup
    scene = SkyScene(grid_step_deg=15, grid_width_deg=1.5)
    images = [render_camera(c, scene) for c in cams]
    ref = stitch(lut, images)
    out = grid_stitch(glut, images)
    m = lut.covered
    err = np.abs(out - ref)[m]
    assert err.max() < 0.02
    assert err.mean() < 0.001


def test_twin_keeps_stars_single(setup, dome):
    cams, glut, lut = setup
    stars = random_stars(300, seed=5, min_alt_deg=0)
    images = [render_camera(c, SkyScene(stars=stars, glow=False), psf_sigma_px=1.0) for c in cams]
    out = grid_stitch(glut, images)
    peaks = find_peaks(out, threshold=0.15)
    su, sv = dome.world_to_pixel(stars.dirs)
    cu, cv = np.clip(np.round(su).astype(int), 0, DOME - 1), np.clip(np.round(sv).astype(int), 0, DOME - 1)
    d = np.hypot(su[:, None] - su[None], sv[:, None] - sv[None]) + np.eye(len(su)) * 1e9
    check = lut.covered[cv, cu] & (d.min(axis=1) > 8)
    for i in np.nonzero(check)[0]:
        near = np.hypot(peaks[:, 0] - su[i], peaks[:, 1] - sv[i])
        assert (near < 6).sum() == 1
        assert near.min() < 0.5
    assert check.sum() > 100


def test_missing_camera_leaves_the_rest_intact(setup):
    cams, glut, lut = setup
    scene = SkyScene(grid_step_deg=15, grid_width_deg=1.5)
    images = [render_camera(c, scene) for c in cams]
    full = grid_stitch(glut, images)
    east = glut.names.index("east")
    images[east] = None
    part = grid_stitch(glut, images, fill=-1.0)
    others = sum(lut.weight[i] for i in range(len(cams)) if i != east) > 0
    # Wherever another camera sees the sky, the picture is still right
    err = np.abs(part - full)[others & lut.covered]
    assert err.max() < 0.02
    # Only east saw it -> left empty
    only_east = lut.covered & ~others
    assert only_east.any() and np.all(part[only_east] == -1.0)


def test_binned_input_uses_the_same_table(setup):
    cams, glut, lut = setup
    scene = SkyScene(grid_step_deg=15, grid_width_deg=1.5)
    full = grid_stitch(glut, [render_camera(c, scene) for c in cams])
    half = grid_stitch(glut, [render_camera(c.scaled(0.5), scene) for c in cams])
    err = np.abs(half - full)[lut.covered]
    assert np.percentile(err, 99) < 0.05


def test_half_size_output_matches_downsampled_full(setup):
    """The chroma pass: half-size output from half-width source planes."""
    cams, glut, lut = setup
    scene = SkyScene(grid_step_deg=15, grid_width_deg=3.0, glow=True)
    imgs = [render_camera(c, scene) for c in cams]
    full = grid_stitch(glut, imgs)
    # 4:2:2 chroma: half width, full height
    chroma = [render_camera(c, scene)[:, ::2] for c in cams]
    half = grid_stitch(glut, chroma, out_div=2)
    ref = full.reshape(DOME // 2, 2, DOME // 2, 2).mean(axis=(1, 3))
    inner = lut.covered.reshape(DOME // 2, 2, DOME // 2, 2).all(axis=(1, 3))
    assert np.percentile(np.abs(half - ref)[inner], 99) < 0.08
