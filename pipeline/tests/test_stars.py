"""Star positions and the star-based camera solve (DECISIONS.md D-027)."""

from datetime import datetime, timezone

import numpy as np

from skydome_pipeline.camera import Camera, FisheyeLens
from skydome_pipeline.geometry import camera_rotation, rot_x, rot_z
from skydome_pipeline.stars import (Catalogue, Detections, _precession, apparent_altaz, julian_date,
                                    refraction_deg, solve_camera)

POLARIS = Catalogue(np.array([37.95456]), np.array([89.26411]), np.array([1.97]))   # J2000


def test_polaris_precessed_to_2026():
    # Published apparent place 2026.0 is about RA 3h04.9m, Dec +89°22.3' (precession dominates)
    v = np.array([np.cos(np.radians(89.26411)) * np.cos(np.radians(37.95456)),
                  np.cos(np.radians(89.26411)) * np.sin(np.radians(37.95456)), np.sin(np.radians(89.26411))])
    d = _precession(julian_date(datetime(2026, 1, 1, tzinfo=timezone.utc))) @ v
    ra = np.degrees(np.arctan2(d[1], d[0])) / 15
    dec = np.degrees(np.arcsin(d[2]))
    assert abs(ra - (3 + 4.9 / 60)) < 0.05          # hours (3 minutes of RA ~ 1' of arc this close to the pole)
    assert abs(dec - (89 + 22.3 / 60)) < 2 / 60


def test_polaris_sits_at_the_latitude_due_north():
    for hour in range(0, 24, 3):
        t = datetime(2026, 11, 3, hour, tzinfo=timezone.utc)
        az, alt = apparent_altaz(POLARIS, 33.63, -112.37, t)
        assert abs(alt[0] - 33.63) < 0.75           # it circles the pole at 0.63° + refraction
        assert min(az[0], 360 - az[0]) < 1.0


def test_refraction_is_half_a_degree_at_the_horizon():
    assert 0.45 < refraction_deg(np.array([0.0]))[0] < 0.6
    assert refraction_deg(np.array([45.0]))[0] < 1.1 / 60


def test_solve_recovers_a_turned_camera_and_its_lens():
    rng = np.random.default_rng(3)
    # A sky of 600 stars, magnitudes 1-6
    n = 600
    z = rng.uniform(0.1, 1, n)
    phi = rng.uniform(0, 2 * np.pi, n)
    dirs = np.stack([np.sqrt(1 - z**2) * np.cos(phi), np.sqrt(1 - z**2) * np.sin(phi), z], axis=-1)
    mags = rng.uniform(1, 6, n)
    nominal = Camera("north", FisheyeLens.from_hfov(3840, 2160, 116.0), camera_rotation(0, 30, 0))
    # The real camera: turned ~3° and with a 2% longer lens, centre 6 px off
    true_lens = FisheyeLens(3840, 2160, nominal.lens.fx * 1.02, nominal.lens.fy * 1.02,
                            nominal.lens.cx + 6, nominal.lens.cy - 4)
    truth = Camera("north", true_lens, nominal.rotation @ rot_z(2.5) @ rot_x(-1.5))
    u, v, ok = truth.world_to_pixel(dirs, margin=15)
    keep = ok & (mags < 5.5)
    det = Detections(u[keep] + rng.normal(0, 0.3, keep.sum()), v[keep] + rng.normal(0, 0.3, keep.sum()),
                     10 ** (-0.4 * mags[keep]) * 1e4, np.full(keep.sum(), 20.0))
    sol = solve_camera(nominal, det, dirs, mags)
    assert sol is not None and sol.matches > 60 and sol.fit_lens
    assert sol.rms_px < 1.0
    tu, tv, _ = truth.world_to_pixel(dirs[keep])
    gu, gv, _ = sol.camera.world_to_pixel(dirs[keep])
    assert np.max(np.hypot(tu - gu, tv - gv)) < 2.0   # the solved camera puts every star where the real one does


def test_solve_finds_a_camera_turned_a_quarter_turn():
    # The config is a quarter turn out (cameras renamed, a camera remounted): found blind
    rng = np.random.default_rng(5)
    n = 900
    z = rng.uniform(0.05, 1, n)
    phi = rng.uniform(0, 2 * np.pi, n)
    dirs = np.stack([np.sqrt(1 - z**2) * np.cos(phi), np.sqrt(1 - z**2) * np.sin(phi), z], axis=-1)
    mags = rng.uniform(1, 6, n)
    nominal = Camera("zenith", FisheyeLens.from_hfov(3840, 2160, 116.0), camera_rotation(0, 90, 0))
    true_lens = FisheyeLens(3840, 2160, nominal.lens.fx * 1.04, nominal.lens.fy * 1.04,
                            nominal.lens.cx, nominal.lens.cy + 40)
    truth = Camera("zenith", true_lens, nominal.rotation @ rot_z(88.0) @ rot_x(2.0))
    u, v, ok = truth.world_to_pixel(dirs, margin=15)
    keep = ok & (mags < 5.0)
    det = Detections(u[keep] + rng.normal(0, 0.3, keep.sum()), v[keep] + rng.normal(0, 0.3, keep.sum()),
                     10 ** (-0.4 * mags[keep]) * 1e4, np.full(keep.sum(), 20.0))
    sol = solve_camera(nominal, det, dirs, mags)
    assert sol is not None and sol.matches > 40 and sol.rms_px < 1.0
    tu, tv, _ = truth.world_to_pixel(dirs[keep])
    gu, gv, _ = sol.camera.world_to_pixel(dirs[keep])
    assert np.max(np.hypot(tu - gu, tv - gv)) < 2.0
