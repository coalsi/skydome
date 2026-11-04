"""The sky watcher (skywatch.py, D-030) on a simulated sky: an aircraft its
ADS-B report identifies, a bird nothing explains, a meteor's lone streak at
night, learning from labels, and the log."""

from __future__ import annotations

import math

import numpy as np
import pytest

from skydome_pipeline.camera import Camera, FisheyeLens
from skydome_pipeline.geometry import altaz_to_vec, camera_rotation
from skydome_pipeline.skymodel import SkyModel, aircraft_look
from skydome_pipeline.skywatch import (Classifier, Frame, Watcher, labelled_examples, log_track, open_log,
                                       shower_of, track_features)

LAT, LON = 33.63, -112.37
T0 = 1793800000.0


class FakeFeed:
    fetched_at = 0.0


def sky_cam() -> Camera:
    return Camera("sky", FisheyeLens.from_hfov(400, 400, 185), camera_rotation(0, 90))


def blank(rng, level=40.0, noise=2.0):
    return (level + rng.normal(0, noise, (400, 400))).astype(np.float32)


def spot(img, u, v, amp=40.0, s=1.2):
    yy, xx = np.mgrid[0:400, 0:400]
    img += amp * np.exp(-((xx - u) ** 2 + (yy - v) ** 2) / (2 * s * s))


def plane_at(t):
    """A light aircraft flying north past the site at 2 km, 200 kt."""
    dist_km = 103 / 1000 * (t - T0)            # 200 kt = 103 m/s
    return {"hex": "abc123", "flight": "TEST1", "lat": LAT - 0.03 + math.degrees(dist_km / 6371),
            "lon": LON + 0.02, "alt_ft": 6500.0, "speed_kt": 200.0, "track": 0.0, "age_s": 0.0, "category": "A1"}


def test_an_aircraft_and_a_bird_by_day():
    rng = np.random.default_rng(1)
    cam = sky_cam()
    model = SkyModel(LAT, LON, 350)
    model._adsb = FakeFeed()
    w = Watcher(cam, model)
    tracks = []
    for k in range(40):
        t = T0 + 0.2 * k
        img = blank(rng)
        p = plane_at(t)
        look = aircraft_look([p], LAT, LON, 350)[0]
        u, v, ok = cam.world_to_pixel(altaz_to_vec(look["az"], look["alt"]))
        if k and ok:
            spot(img, float(u), float(v))
        if 5 <= k < 15:                          # a bird crossing fast, nothing reported there
            spot(img, 60 + 9 * (k - 5), 300, amp=60, s=1.5)
        model._adsb.fetched_at = t
        tracks += w.process(Frame(k, t, 0.001, 0, img), {"sun_alt": 30.0, "bodies": [], "planes": [p]})
    tracks += w.flush()
    recs = [w.identify(tr, 30.0) for tr in tracks]
    planes = [r for r in recs if r["kind"] == "plane"]
    assert len(planes) == 1 and planes[0]["match"] == "TEST1" and planes[0]["source"] == "adsb"
    birds = [r for r in recs if r["kind"] == "bird"]
    assert len(birds) == 1 and birds[0]["frames"] >= 8 and birds[0]["source"] == "rules"


def test_a_lone_streak_at_night_is_a_meteor():
    rng = np.random.default_rng(2)
    w = Watcher(sky_cam(), SkyModel(LAT, LON, 350))
    snap = {"sun_alt": -30.0, "bodies": [], "planes": None}
    for k in range(6):
        img = blank(rng)
        if k == 2:
            for s in np.linspace(0, 1, 120):     # 50 px, fading in and out
                spot(img, 150 + 40 * s, 120 + 30 * s, amp=35 * math.sin(math.pi * s) + 5, s=1.0)
        w.process(Frame(k, T0 + 10 * k, 10.0, 200, img), snap)
    recs = [w.identify(tr, -30.0) for tr in w.flush()]
    assert [r["kind"] for r in recs] == ["meteor"]
    assert recs[0]["features"]["streak_deg"] > 15


def test_it_learns_from_labels():
    bird = {"speed_dps": 8, "straightness": 0.95, "duration_s": 2, "size_deg": 0.6, "streak_deg": 0, "brightness": 2.5,
            "blink": 0.2, "frames": 10, "alt": 40, "night": 0, "streak_frac": 0}
    bug = {**bird, "speed_dps": 25, "straightness": 0.5, "size_deg": 2.0, "duration_s": 0.6, "frames": 3}
    examples = [({**bird, "speed_dps": 8 + i}, "bird") for i in range(4)] + [({**bug, "alt": 30 + i}, "insect")
                                                                          for i in range(4)]
    c = Classifier(examples)
    assert c.classify({**bird, "speed_dps": 9.5})[0] == "bird"
    assert c.classify({**bug, "speed_dps": 22})[0] == "insect"
    assert c.classify({**bird, "speed_dps": 1e4, "brightness": 9, "size_deg": 50})[0] == "unknown"   # like nothing it knows


def test_the_log_keeps_pictures_and_labels(tmp_path):
    rng = np.random.default_rng(3)
    w = Watcher(sky_cam(), SkyModel(LAT, LON, 350))
    for k in range(10):
        img = blank(rng)
        if k >= 2:
            spot(img, 100 + 5 * k, 200, amp=50)
        w.process(Frame(k, T0 + 0.2 * k, 0.001, 0, img), {"sun_alt": 30.0, "bodies": [], "planes": None})
    (tr,) = w.flush()
    db = open_log(tmp_path / "objects.db")
    oid = log_track(db, w.identify(tr, 30.0), tr, tmp_path / "media")
    thumb, clip = db.execute("SELECT thumb, clip FROM objects WHERE id = ?", (oid,)).fetchone()
    assert (tmp_path / "media" / thumb).exists() and (tmp_path / "media" / clip).exists()
    db.execute("UPDATE objects SET label = 'bird', kind = 'bird' WHERE id = ?", (oid,))
    db.commit()
    (ex,) = labelled_examples(db)
    assert ex[1] == "bird" and ex[0]["frames"] == len(tr.dets)


def test_a_leonid_points_back_to_leo():
    # 17 Nov, 5 am in Arizona: Leo high in the east. A streak whose line runs back to the radiant
    from datetime import datetime, timezone

    from skydome_pipeline.stars import _altaz_of_date, julian_date

    t = datetime(2026, 11, 17, 12, 0, tzinfo=timezone.utc)
    az, alt = _altaz_of_date(np.radians([152.0]), np.radians([22.0]), LAT, LON, julian_date(t))
    r = altaz_to_vec(az[0], alt[0])
    # Two points along a great circle away from the radiant (the meteor moves away from it)
    away = np.cross(r, [0, 0, 1.0])
    away /= np.linalg.norm(away)
    a = np.cos(np.radians(20)) * r + np.sin(np.radians(20)) * away
    b = np.cos(np.radians(35)) * r + np.sin(np.radians(35)) * away
    from skydome_pipeline.geometry import vec_to_altaz
    ends = tuple(tuple(float(x) for x in vec_to_altaz(p)) for p in (a, b))
    assert shower_of(ends, t.timestamp(), LAT, LON) == "Leonids"
    # Sideways to the radiant: not a Leonid
    side = np.cross(r, away)
    c = np.cos(np.radians(30)) * r + np.sin(np.radians(30)) * side
    ends2 = (tuple(float(x) for x in vec_to_altaz(c - 0.1 * away)), tuple(float(x) for x in vec_to_altaz(c + 0.1 * away)))
    assert shower_of(ends2, t.timestamp(), LAT, LON) is None
