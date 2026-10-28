import numpy as np
import pytest

from skydome_pipeline.camera import Camera, FisheyeLens, PinholeLens
from skydome_pipeline.geometry import altaz_to_vec, angle_between_deg, camera_rotation, vec_to_altaz
from skydome_pipeline.projection import AllSkyProjection, EquirectHemisphere


def test_altaz_cardinals():
    np.testing.assert_allclose(altaz_to_vec(0, 0), [0, 1, 0], atol=1e-12)    # north
    np.testing.assert_allclose(altaz_to_vec(90, 0), [1, 0, 0], atol=1e-12)   # east
    np.testing.assert_allclose(altaz_to_vec(123, 90), [0, 0, 1], atol=1e-12)  # zenith


def test_altaz_round_trip():
    rng = np.random.default_rng(1)
    az, alt = rng.uniform(0, 360, 1000), rng.uniform(-89, 89, 1000)
    az2, alt2 = vec_to_altaz(altaz_to_vec(az, alt))
    np.testing.assert_allclose(alt2, alt, atol=1e-9)
    np.testing.assert_allclose((az2 - az + 180) % 360 - 180, 0, atol=1e-9)


@pytest.mark.parametrize("az,alt,roll", [(0, 45, 0), (90, 45, 0), (200, 30, 12), (0, 90, 0), (37, 90, -20)])
def test_camera_rotation_is_proper(az, alt, roll):
    r = camera_rotation(az, alt, roll)
    np.testing.assert_allclose(r.T @ r, np.eye(3), atol=1e-12)
    assert np.linalg.det(r) == pytest.approx(1.0)
    np.testing.assert_allclose(r[:, 2], altaz_to_vec(az, alt), atol=1e-12)


def test_side_camera_rows_are_level_and_up_is_up():
    r = camera_rotation(0, 45)
    right, down = r[:, 0], r[:, 1]
    assert right[2] == pytest.approx(0.0)   # image rows parallel to the horizon
    assert down[2] < 0                      # image "down" points toward the ground


@pytest.mark.parametrize("lens", [
    FisheyeLens(3840, 2160, 1800, 1800, 1919.5, 1079.5, (0.02, -0.01, 0.003, -0.0005)),
    PinholeLens(3840, 2160, 1100, 1100, 1919.5, 1079.5, (-0.04, 0.004, -0.0002)),
])
def test_lens_round_trip(lens):
    u, v = np.meshgrid(np.linspace(0, lens.width - 1, 41), np.linspace(0, lens.height - 1, 23))
    rays = lens.unproject(u, v)
    u2, v2, ok = lens.project(rays)
    assert ok.all()
    np.testing.assert_allclose(u2, u, atol=1e-6)
    np.testing.assert_allclose(v2, v, atol=1e-6)


@pytest.mark.parametrize("cls", [FisheyeLens, PinholeLens])
def test_from_hfov_spans_the_width(cls):
    lens = cls.from_hfov(3840, 2160, 120)
    left = lens.unproject(np.array(-0.5), np.array(lens.cy))
    right = lens.unproject(np.array(lens.width - 0.5), np.array(lens.cy))
    assert angle_between_deg(left, right) == pytest.approx(120, abs=1e-6)


def test_camera_world_round_trip():
    cam = Camera("north", FisheyeLens.from_hfov(3840, 2160, 120), camera_rotation(0, 45, 3))
    u, v = np.meshgrid(np.linspace(10, 3830, 9), np.linspace(10, 2150, 7))
    dirs = cam.pixel_to_world(u, v)
    u2, v2, ok = cam.world_to_pixel(dirs)
    assert ok.all()
    np.testing.assert_allclose(u2, u, atol=1e-6)
    np.testing.assert_allclose(v2, v, atol=1e-6)


def test_north_camera_sees_north_at_image_centre():
    cam = Camera("north", PinholeLens.from_hfov(3840, 2160, 120), camera_rotation(0, 45))
    u, v, ok = cam.world_to_pixel(altaz_to_vec(0, 45))
    assert ok and u == pytest.approx(cam.lens.cx) and v == pytest.approx(cam.lens.cy)
    # Higher altitude appears higher in the image (smaller v)
    _, v_up, _ = cam.world_to_pixel(altaz_to_vec(0, 60))
    assert v_up < v
    # East of north appears to the right
    u_e, _, _ = cam.world_to_pixel(altaz_to_vec(20, 45))
    assert u_e > u


def test_allsky_layout():
    p = AllSkyProjection(size=1001)
    c = (p.size - 1) / 2
    u, v = p.world_to_pixel(altaz_to_vec(0, 90))
    assert (u, v) == (pytest.approx(c), pytest.approx(c))
    u, v = p.world_to_pixel(altaz_to_vec(0, 0))          # north horizon: top edge
    assert v == pytest.approx(-0.5) and u == pytest.approx(c)
    u, v = p.world_to_pixel(altaz_to_vec(90, 0))         # east horizon: left edge
    assert u == pytest.approx(-0.5) and v == pytest.approx(c)
    u, _ = AllSkyProjection(size=1001, east_left=False).world_to_pixel(altaz_to_vec(90, 0))
    assert u == pytest.approx(p.size - 0.5)


@pytest.mark.parametrize("canvas", [AllSkyProjection(size=512), EquirectHemisphere(1024, 256)])
def test_canvas_round_trip(canvas):
    u, v = np.meshgrid(np.arange(3, canvas.width, 17.0), np.arange(3, canvas.height, 13.0))
    dirs, inside = canvas.pixel_to_world(u, v)
    u2, v2 = canvas.world_to_pixel(dirs)
    m = inside
    np.testing.assert_allclose(u2[m], u[m], atol=1e-6)
    np.testing.assert_allclose(v2[m], v[m], atol=1e-6)
