"""The ZWO all-sky camera on the server side (D-029): its settings file and
ranges, Night/Day by the Sun, ONVIF's exposure in its units, and its 16-bit
frames in the raw download."""

from __future__ import annotations

import json
import threading
import time
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timezone
import io
import sys

import pytest
from fastapi.testclient import TestClient

from skydome_server.app import ASI_PRESETS, Live, create_app
from skydome_server.asi_settings import AsiSettings
from skydome_server.onvif.services import imaging_changes
from skydome_server.onvif.soap import NS
from skydome_server.onvif.http import SnapshotSource
from skydome_server.sky import Sky
from skydome_server.system import System

CAPS = {"camera": "ZWO ASI676MC", "width": 3552, "height": 3552, "bayer": 0,
        "controls": {"Gain": {"min": 0, "max": 400, "default": 100, "auto": True, "writable": True},
                     "Exposure": {"min": 32, "max": 2000000000, "default": 10000, "auto": True, "writable": True},
                     "Offset": {"min": 0, "max": 80, "default": 8, "auto": False, "writable": True}}}


def quiet(*a, **k):
    pass


def test_ranges_come_from_the_camera_once_it_has_opened(tmp_path):
    s = AsiSettings(tmp_path / "asi_settings.yaml", tmp_path / "asi_caps.json", log=quiet)
    assert s.controls()["gain"].maximum == 600            # before: a generous guess
    (tmp_path / "asi_caps.json").write_text(json.dumps(CAPS))
    c = s.controls()
    assert (c["gain"].maximum, c["auto_max_gain"].maximum) == (400, 400)
    assert (c["offset"].maximum, c["offset"].default) == (80, 8)   # the camera's own default
    assert c["exposure_us"].maximum == 60_000_000                  # not its 2000 s: the slider stays usable
    assert s.effective()["auto_exposure"] == 1


def test_settings_are_saved_for_skydome_live_and_checked(tmp_path):
    f = tmp_path / "asi_settings.yaml"
    s = AsiSettings(f, None, log=quiet)
    s.update({"auto_exposure": 0, "exposure_us": 5_000_000, "gain": 150})
    text = f.read_text()
    assert "exposure_us: 5000000" in text and "auto_exposure: 0" in text and "gain: 150" in text
    with pytest.raises(ValueError):
        s.update({"exposure_us": 10})                  # shorter than the camera does
    with pytest.raises(ValueError):
        s.update({"brightness": 3})                    # a UVC control: not this camera's
    again = AsiSettings(f, None, log=quiet)
    assert again.values == {"auto_exposure": 0, "exposure_us": 5_000_000, "gain": 150}


def test_onvif_exposure_in_microseconds(tmp_path):
    s = ET.fromstring(f'<timg:ImagingSettings xmlns:timg="{NS["timg"]}" xmlns:tt="{NS["tt"]}">'
                      "<tt:Exposure><tt:Mode>MANUAL</tt:Mode><tt:ExposureTime>2500000</tt:ExposureTime>"
                      "<tt:Gain>120</tt:Gain></tt:Exposure></timg:ImagingSettings>")
    assert imaging_changes(s, {}, AsiSettings.ONVIF_EXPOSURE) == {"auto_exposure": 0, "exposure_us": 2_500_000,
                                                                  "gain": 120}


def app_for(tmp_path):
    settings = AsiSettings(tmp_path / "asi_settings.yaml", tmp_path / "asi_caps.json", log=quiet)
    stats = tmp_path / "live.json"
    raw = tmp_path / "raw"
    raw.mkdir()
    lv = Live(tmp_path / "mode.yaml", [stats], paths=lambda: {}, raw_dir=raw, stretch_file=tmp_path / "stretch.txt",
              stack_file=tmp_path / "stack.txt")
    app = create_app(settings, lv, System(tmp_path), SnapshotSource(tmp_path / "s.jpg", timeout=0.1))
    return app, settings, stats, raw


def test_presets_set_the_picture_not_the_exposure(tmp_path):
    app, settings, *_ = app_for(tmp_path)
    client = TestClient(app)
    r = client.post("/api/preset/night").json()
    assert r["stretch"] == ASI_PRESETS["night"]["stretch"] and r["stack"] == 3
    assert settings.effective()["gamma"] == 100 and settings.effective()["auto_exposure"] == 1
    client.post("/api/preset/day")
    assert settings.effective()["gamma"] == 220
    assert (tmp_path / "stack.txt").read_text().strip() == "0"


def test_night_and_day_follow_the_sun(tmp_path):
    clock = {"t": datetime(2026, 11, 4, 19, 0, tzinfo=timezone.utc)}   # noon in Arizona
    applied = []
    s = Sky(tmp_path / "none.dat", 33.63, -112.37, tmp_path / "c.json", tmp_path / "c.auto", [sys.executable],
            clock=lambda: clock["t"], preset_file=tmp_path / "preset.auto", apply_preset=applied.append)
    s.switch_preset()
    s.switch_preset()
    assert applied == ["day"]                          # once, at start-up
    clock["t"] = datetime(2026, 11, 5, 1, 0, tzinfo=timezone.utc)       # 6 pm: Sun ~6° down, in the gap
    s.switch_preset()
    assert applied == ["day"]
    clock["t"] = datetime(2026, 11, 5, 3, 0, tzinfo=timezone.utc)       # 8 pm
    s.switch_preset()
    assert applied == ["day", "night"]
    (tmp_path / "preset.auto").write_text("off\n")
    clock["t"] = datetime(2026, 11, 5, 19, 0, tzinfo=timezone.utc)
    s.switch_preset()
    assert applied == ["day", "night"]                 # switched off


def test_raw_download_has_the_16_bit_frame(tmp_path):
    app, _, stats, raw = app_for(tmp_path)
    stats.write_text(json.dumps({"cameras": {"sky": {"state": "live", "exposure_us": 200_000}}}))
    stop = threading.Event()

    def fake_live():
        while not stop.is_set():
            if (raw / "request").exists():
                (raw / "request").unlink()
                time.sleep(0.1)
                (raw / "sky.pgm").write_bytes(b"P5\n2 1\n65535\n\x00\x01\xff\xff")
            time.sleep(0.02)

    t = threading.Thread(target=fake_live)
    t.start()
    try:
        r = TestClient(app).get("/api/raw.zip")
    finally:
        stop.set()
        t.join()
    assert r.status_code == 200
    z = zipfile.ZipFile(io.BytesIO(r.content))
    assert z.read("sky.pgm").startswith(b"P5")


def test_object_log_and_labels(tmp_path):
    from skydome_pipeline.skywatch import open_log
    from skydome_server.objects import ObjectLog

    db = open_log(tmp_path / "objects.db")
    (tmp_path / "media" / "2026-11-04").mkdir(parents=True)
    (tmp_path / "media" / "2026-11-04" / "1.jpg").write_bytes(b"\xff\xd8 jpeg \xff\xd9")
    db.execute("INSERT INTO objects (start, end, frames, kind, auto_kind, confidence, source, unknown, path, "
               "features, thumb) VALUES (1793800000, 1793800004, 12, 'unknown', 'unknown', 0.3, 'rules', 1, "
               "'[[1793800000, 40, 50]]', '{\"speed_dps\": 2}', '2026-11-04/1.jpg')")
    db.commit()
    settings = AsiSettings(tmp_path / "s.yaml", None, log=quiet)
    lv = Live(tmp_path / "mode.yaml", [tmp_path / "live.json"], paths=lambda: {})
    app = create_app(settings, lv, System(tmp_path), SnapshotSource(tmp_path / "s.jpg", timeout=0.1),
                     objects=ObjectLog(tmp_path / "objects.db", tmp_path / "media"))
    client = TestClient(app)
    r = client.get("/api/objects?unknown=true").json()
    (o,) = r["objects"]
    assert o["kind"] == "unknown" and o["unknown"] and "plane" in r["labels"]
    assert client.get(o["thumb"]).content.startswith(b"\xff\xd8")
    assert client.get("/api/objects/media/2026-11-04/..%2F..%2Fobjects.db").status_code == 404   # stays in media/
    o = client.put(f"/api/objects/{o['id']}", json={"label": "Bird"}).json()
    assert (o["kind"], o["label"], o["unknown"], o["auto_kind"]) == ("bird", "bird", False, "unknown")
    assert client.get("/api/objects/summary").json()["labelled"] == 1
    assert db.execute("SELECT value FROM meta WHERE key = 'labels'").fetchone()   # the watcher relearns
    o = client.put(f"/api/objects/{o['id']}", json={"label": None}).json()
    assert (o["kind"], o["unknown"]) == ("unknown", True)
    assert client.put(f"/api/objects/{o['id']}", json={"label": "<b>"}).status_code == 400
