"""Star names for the page and calibrating from the stars (skydome_server/sky.py, D-028)."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from skydome_pipeline.star_names import describe
from skydome_server import sky as sky_mod
from skydome_server.app import Live, create_app
from skydome_server.camera_settings import CameraSettings
from skydome_server.onvif.http import SnapshotSource
from skydome_server.sky import Sky
from skydome_server.system import System



def bsc_line(hr: int, name: str, ra: str, dec: str, mag: str) -> str:
    line = [" "] * 197
    def put(col: int, text: str):   # 1-based start column, as in the BSC5 readme
        line[col - 1:col - 1 + len(text)] = text
    put(1, f"{hr:4d}")
    put(5, name)
    put(76, ra)
    put(84, dec)
    put(103, mag)
    return "".join(line)


def make_catalogue(path):
    path.write_text("\n".join([
        bsc_line(7001, "  3Alp Lyr", "183656.3", "+384701", " 0.03"),
        bsc_line(424, "  1Alp UMi", "023148.7", "+891551", " 2.02"),
        bsc_line(8086, " 61    Cyg", "210653.9", "+384458", " 5.21"),
    ]) + "\n")


def test_names():
    assert describe("  3Alp Lyr", 7001) == ("Vega", "α Lyr", "Lyra")
    assert describe("  5Alp1Cen", 5459) == ("Rigil Kentaurus", "α¹ Cen", "Centaurus")
    assert describe("  8Alp2Cen", 5460) == ("α² Cen", "α² Cen", "Centaurus")   # no name of its own
    assert describe(" 61    Cyg", 8086) == ("61 Cyg", "61 Cyg", "Cygnus")
    assert describe(" 27    Tau", 1178) == ("Atlas", "27 Tau", "Taurus")
    assert describe("          ", 1) == ("HR 1", "HR 1", "")


def make_sky(tmp_path, when):
    cat = tmp_path / "bsc5.dat"
    make_catalogue(cat)
    clock = {"t": when}
    s = Sky(cat, 33.63, -112.37, tmp_path / "calibration.json", tmp_path / "calibration.auto",
            [sys.executable, "-c", "import time; time.sleep(0.3)"], clock=lambda: clock["t"])
    return s, clock


NIGHT = datetime(2026, 11, 4, 4, 30, tzinfo=timezone.utc)    # 9:30 pm in Arizona
DAY = datetime(2026, 11, 4, 19, 0, tzinfo=timezone.utc)      # noon


def test_stars_up_with_names(tmp_path):
    s, _ = make_sky(tmp_path, NIGHT)
    doc = s.stars(max_mag=5.5)
    assert doc["sun_alt"] < -20 and doc["catalogue"]
    rows = {r[0]: r for r in doc["stars"]}
    assert set(rows) == {"Vega", "Polaris", "61 Cyg"}
    pol = rows["Polaris"]
    assert pol[1:4] == ["α UMi", "Ursa Minor", 2.02]
    assert abs(pol[5] - 33.6) < 1.0 and min(pol[4], 360 - pol[4]) < 1.5     # due north at the latitude
    assert {r[0] for r in s.stars(max_mag=3)["stars"]} == {"Vega", "Polaris"}   # cached per magnitude


def test_sun_by_day_and_night(tmp_path):
    s, clock = make_sky(tmp_path, DAY)
    assert s.sun_alt() > 30
    clock["t"] = NIGHT
    assert s.sun_alt() < -20


def test_automatic_calibration_rules(tmp_path):
    s, clock = make_sky(tmp_path, DAY)
    assert s.due() is None                                   # daylight
    clock["t"] = NIGHT
    assert s.due() == "first night"
    s.set_auto(False)
    assert s.due() is None
    s.set_auto(True)
    stamp = lambda t: t.strftime("%Y-%m-%dT%H:%M:%SZ")
    s._started = NIGHT.timestamp() - 3600                    # the server's been up an hour
    s.status_file.write_text(json.dumps({"state": "done", "started_utc": stamp(NIGHT - timedelta(minutes=30))}))
    assert s.due() is None                                   # done tonight
    s.status_file.write_text(json.dumps({"state": "failed", "started_utc": stamp(NIGHT - timedelta(minutes=10))}))
    assert s.due() is None                                   # cloudy: wait before trying again
    s.status_file.write_text(json.dumps({"state": "failed", "started_utc": stamp(NIGHT - timedelta(minutes=25))}))
    assert s.due() == "trying again"
    s._started = NIGHT.timestamp() - 20 * 3600               # up since yesterday
    s.status_file.write_text(json.dumps({"state": "done", "started_utc": stamp(NIGHT - timedelta(hours=13))}))
    assert s.due() == "nightly"
    s._started = NIGHT.timestamp() - 60                      # just set up again
    s.status_file.write_text(json.dumps({"state": "done", "started_utc": stamp(NIGHT - timedelta(hours=2))}))
    assert s.due() == "dark after start-up"


def test_calibration_api(tmp_path):
    s, clock = make_sky(tmp_path, DAY)
    settings = CameraSettings(tmp_path / "cs.yaml", finder=lambda: [], log=lambda *a, **k: None)
    app = create_app(settings, Live(tmp_path / "m.yaml", [tmp_path / "x.json"], paths=lambda: {}), System(tmp_path),
                     SnapshotSource(tmp_path / "s.jpg", timeout=0.1), web_dir=tmp_path / "dist", sky=s)
    client = TestClient(app)
    assert {r[0] for r in client.get("/api/sky").json()["stars"]} >= {"Vega"}
    st = client.get("/api/calibration").json()
    assert st["state"] == "never" and st["auto"] and not st["dark_enough"]
    r = client.post("/api/calibration")
    assert r.status_code == 409 and "dark sky" in r.text
    clock["t"] = NIGHT
    r = client.post("/api/calibration")
    assert r.status_code == 200 and r.json()["state"] == "running"
    assert client.post("/api/calibration").status_code == 409             # one at a time
    s._proc.wait()
    assert client.get("/api/calibration").json()["state"] == "failed"     # ended without a result
    assert client.put("/api/calibration", json={"auto": False}).json()["auto"] is False
