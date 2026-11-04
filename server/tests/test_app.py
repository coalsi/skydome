"""The web page's API (skydome_server/app.py), live mode file and system stats."""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from skydome_server import live
from skydome_server.app import Live, create_app
from skydome_server.camera_settings import FALLBACK_CONTROLS, CameraSettings
from skydome_server.onvif.http import SnapshotSource
from skydome_server.system import System
from skydome_server.v4l2 import CameraNode


class FakeDevice:
    log: list = []

    def __init__(self, path):
        self.path = path

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass

    def controls(self):
        return dict(FALLBACK_CONTROLS)

    def set(self, cid, value):
        name = next(c.name for c in FALLBACK_CONTROLS.values() if c.id == cid)
        FakeDevice.log.append((self.path, name, value))


@pytest.fixture
def rig(tmp_path):
    FakeDevice.log = []
    cams = [CameraNode("/dev/video0", ("video0", "2-1.1", "3")), CameraNode("/dev/video2", ("video2", "2-2", "4"))]
    settings = CameraSettings(tmp_path / "camera_settings.yaml", finder=lambda: list(cams), opener=FakeDevice,
                              log=lambda *a, **k: None)
    stats = tmp_path / "live.json"
    paths = {"dome": {"ready": True, "readers": 2}}
    lv = Live(tmp_path / "live_mode.yaml", [tmp_path / "missing.json", stats], paths=lambda: paths)
    snap = tmp_path / "snap.jpg"
    app = create_app(settings, lv, System(tmp_path), SnapshotSource(snap, timeout=0.2), web_dir=tmp_path / "dist")
    return TestClient(app), settings, lv, stats, snap


def test_status_without_the_live_dome(rig):
    client, *_ = rig
    s = client.get("/api/status").json()
    assert s["mode"] == "detail" and s["smooth_until"] is None
    assert s["live"] is None                                   # no stats file
    assert s["stream"] == {"path": "dome", "webrtc_port": 8889, "ready": True, "viewers": 2}
    assert "temps_c" in s["system"] and "disk" in s["system"]


def test_status_reads_fresh_stats_only(rig):
    client, _, _, stats, _ = rig
    stats.write_text(json.dumps({"output_fps": 15.0, "cameras": {"north": {"fps": 15.0, "state": "live"}}}))
    assert client.get("/api/status").json()["live"]["output_fps"] == 15.0
    old = time.time() - live.STALE_S - 1
    os.utime(stats, (old, old))
    assert client.get("/api/status").json()["live"] is None   # skydome-live stopped


def test_settings_list_and_patch_applies_to_every_camera(rig):
    client, settings, *_ = rig
    ctrls = {c["name"]: c for c in client.get("/api/settings").json()["controls"]}
    assert ctrls["gain"] == {"name": "gain", "min": 0, "max": 190, "step": 1, "default": 140, "value": 140,
                             "set": False}
    r = client.patch("/api/settings", json={"auto_exposure": 1, "exposure_time_absolute": 50})
    assert r.status_code == 200
    ctrls = {c["name"]: c for c in r.json()["controls"]}
    assert ctrls["exposure_time_absolute"]["value"] == 50 and ctrls["exposure_time_absolute"]["set"]
    assert {(p, n) for p, n, _ in FakeDevice.log} >= {("/dev/video0", "exposure_time_absolute"),
                                                      ("/dev/video2", "exposure_time_absolute")}
    assert settings.values == {"auto_exposure": 1, "exposure_time_absolute": 50}   # same store as ONVIF


def test_settings_reject_bad_values(rig):
    client, settings, *_ = rig
    assert client.patch("/api/settings", json={"gain": 999}).status_code == 400
    assert client.patch("/api/settings", json={"zoom": 1}).status_code == 400
    assert settings.values == {}


def test_mode_switch_and_smooth_timeout(rig, tmp_path):
    client, _, lv, *_ = rig
    r = client.put("/api/mode", json={"mode": "smooth"}).json()
    assert r["mode"] == "smooth" and r["stream_path"] == "dome-smooth" and r["smooth_until"]
    assert client.get("/api/status").json()["stream"]["path"] == "dome-smooth"
    assert client.put("/api/mode", json={"mode": "fast"}).status_code == 400
    # Smooth picked 31 minutes ago has run out (D-015)
    live.write_mode(lv.mode_file, "smooth", datetime.now(timezone.utc) - timedelta(minutes=live.SMOOTH_MINUTES + 1))
    assert live.read_mode(lv.mode_file) == ("detail", None)
    assert client.put("/api/mode", json={"mode": "detail"}).json()["mode"] == "detail"


def test_mode_file_missing_or_junk_means_detail(tmp_path):
    assert live.read_mode(tmp_path / "none.yaml") == ("detail", None)
    (tmp_path / "junk.yaml").write_text("mode: smooth\nsince: yesterday\n")
    assert live.read_mode(tmp_path / "junk.yaml") == ("detail", None)


def test_snapshot(rig):
    client, _, _, _, snap = rig
    assert client.get("/api/snapshot.jpg").status_code == 503   # nothing answers the request
    snap.write_bytes(b"\xff\xd8 dome \xff\xd9")                  # a fresh still is reused
    r = client.get("/api/snapshot.jpg")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"


def test_page_or_placeholder(rig, tmp_path):
    client, *_ = rig
    assert "isn't built" in client.get("/").text
    dist = tmp_path / "dist2"
    dist.mkdir()
    (dist / "index.html").write_text("<title>SkyDome</title>")
    _, settings, lv, *_ = rig
    app = create_app(settings, lv, System(tmp_path), SnapshotSource(tmp_path / "s.jpg"), web_dir=dist)
    assert "<title>SkyDome</title>" in TestClient(app).get("/").text
    assert TestClient(app).get("/api/status").status_code == 200   # the page doesn't hide the API


def test_system_stats_from_fake_proc(tmp_path):
    proc, sysfs = tmp_path / "proc", tmp_path / "sys"
    (proc / "net").mkdir(parents=True)
    (proc / "stat").write_text("cpu  100 0 100 800 0 0 0 0 0 0\n")
    (proc / "meminfo").write_text("MemTotal:  8000 kB\nMemAvailable:  3000 kB\n")
    (proc / "uptime").write_text("123.4 456.7\n")
    (proc / "net" / "wireless").write_text("h1\nh2\n wlP1p1s0: 0000   63.  -47.  -256  0 0 0 0 0 0\n")
    for i, (name, milli) in enumerate([("cpu-thermal", 60281), ("gpu-thermal", 59562), ("unused", -40000)]):
        z = sysfs / "class" / "thermal" / f"thermal_zone{i}"
        z.mkdir(parents=True)
        (z / "type").write_text(name + "\n")
        (z / "temp").write_text(f"{milli}\n")
    s = System(tmp_path, proc=proc, sys_root=sysfs)
    assert s.temps() == {"cpu": 60.3, "gpu": 59.6}
    assert s.cpu_pct() == 20.0
    (proc / "stat").write_text("cpu  150 0 150 900 0 0 0 0 0 0\n")   # +100 busy, +100 idle
    assert s.cpu_pct() == 50.0
    assert s.memory() == {"used": 5000 * 1024, "total": 8000 * 1024}
    assert s.wifi_dbm() == -47 and s.uptime() == 123.4
