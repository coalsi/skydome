"""The focus page's numbers, on made-up frames."""

import json
import threading
import time

import cv2
import numpy as np
from fastapi.testclient import TestClient

from skydome_server import focus
from skydome_server.app import Live, create_app
from skydome_server.asi_settings import AsiSettings
from skydome_server.onvif.http import SnapshotSource
from skydome_server.system import System


def starfield(sigma: float, n: int = 40, size: tuple[int, int] = (1080, 1920), seed: int = 1) -> np.ndarray:
    rng = np.random.default_rng(seed)
    img = np.zeros(size, np.float32)
    for x, y, b in zip(rng.integers(40, size[1] - 40, n), rng.integers(40, size[0] - 40, n), rng.uniform(0.3, 1, n)):
        img[y, x] = b
    img = cv2.GaussianBlur(img, (0, 0), sigma)
    img = img / img.max() * 30000 + 3000
    return img + rng.normal(0, 40, size).astype(np.float32)


def test_stars_grow_as_they_blur():
    sharp, soft = focus.star_sizes(starfield(1.5)), focus.star_sizes(starfield(4.0))
    assert len(sharp) >= 20 and len(soft) >= 20
    assert 2 < np.median(sharp) < 5
    assert np.median(soft) > 1.8 * np.median(sharp)


def test_a_star_well_out_of_focus_still_counts():
    img = np.full((1080, 1920), 3000, np.float32) + np.random.default_rng(4).normal(0, 120, (1080, 1920)).astype(np.float32)
    for x, y in ((300, 300), (900, 500), (1500, 800), (700, 900)):
        cv2.circle(img, (x, y), 18, 3700, -1)
    sizes = focus.star_sizes(img)
    assert len(sizes) == 4 and 18 < np.median(sizes) < 32


def test_an_edge_is_not_a_star():
    img = np.full((1080, 1920), 3000, np.float32) + np.random.default_rng(5).normal(0, 40, (1080, 1920)).astype(np.float32)
    img[:, 900:] += 8000
    assert focus.star_sizes(img) == []


def test_a_hot_pixel_is_not_a_star():
    img = (3000 + np.random.default_rng(2).normal(0, 40, (1080, 1920))).astype(np.float32)
    img[500, 500] = img[300, 800] = 60000
    assert focus.star_sizes(img) == []


def test_saturated_stars_are_left_out():
    img = starfield(2.0)
    assert len(focus.star_sizes(np.clip(img * 6, 0, 65535))) < len(focus.star_sizes(img))


def test_sharpness_falls_with_blur_not_with_exposure():
    scene = cv2.resize(np.random.default_rng(3).uniform(2000, 40000, (60, 60)).astype(np.float32), (600, 600),
                       interpolation=cv2.INTER_NEAREST)
    sharp, soft = focus.sharpness(scene), focus.sharpness(cv2.GaussianBlur(scene, (0, 0), 6))
    assert soft < sharp / 3
    assert abs(focus.sharpness(scene * 0.5) - sharp) < 0.01 * sharp


def test_read_pgm(tmp_path):
    img = np.array([[1, 2, 3], [65535, 256, 0]], ">u2")
    (tmp_path / "f.pgm").write_bytes(b"P5\n3 2\n65535\n" + img.tobytes())
    assert focus.read_pgm(tmp_path / "f.pgm").tolist() == img.astype(float).tolist()


def test_measure_only_counts_stars_in_a_long_frame():
    img = starfield(2.0)
    night = focus.measure(img, 0.5, 0.5, 512, 2_000_000)
    assert night["star_size"] and night["stars"] >= focus.MIN_STARS
    assert night["crop"].startswith("data:image/jpeg")
    assert focus.measure(img, 0.5, 0.5, 512, 10_000)["star_size"] is None


def test_the_spot_stays_inside_the_frame():
    img = starfield(2.0)
    assert focus.measure(img, 1.0, 0.0, 512, 0)["spot"] == {"x": 1920 - 512, "y": 0, "size": 512}


def test_the_page_gets_a_frame_even_when_the_raw_folder_has_gone(tmp_path):
    """/tmp can be cleaned under skydome-live, which only makes the folder when it starts."""
    raw = tmp_path / "raw"
    stats = tmp_path / "live.json"
    stats.write_text(json.dumps({"t": time.time(), "cameras": {"sky": {"state": "live", "exposure_us": 2_000_000}}}))
    lv = Live(tmp_path / "mode.yaml", [stats], paths=lambda: {}, raw_dir=raw)
    app = create_app(AsiSettings(tmp_path / "s.yaml", tmp_path / "c.json"), lv, System(tmp_path),
                     SnapshotSource(tmp_path / "s.jpg", timeout=0.1))
    frame = np.clip(starfield(2.0), 0, 65535).astype(">u2")

    def camera():
        for _ in range(200):
            if (raw / "request").exists():
                (raw / "request").unlink()
                (raw / "sky.pgm").write_bytes(b"P5\n1920 1080\n65535\n" + frame.tobytes())
                return
            time.sleep(0.02)

    threading.Thread(target=camera, daemon=True).start()
    d = TestClient(app).get("/api/focus", params={"x": 0.25, "y": 0.75}).json()
    assert d["width"] == 1920 and d["exposure_us"] == 2_000_000 and d["star_size"]
    assert d["spot"] == {"x": 224, "y": 554, "size": 512}


def one_star_frame(sigma: float, peak: float = 12000) -> np.ndarray:
    img = np.zeros((1080, 1920), np.float32)
    img[500, 960] = 1
    img = cv2.GaussianBlur(img, (0, 0), sigma)
    img = img / img.max() * peak + 3000
    return img + np.random.default_rng(6).normal(0, 40, img.shape).astype(np.float32)


def test_the_spots_own_star_shrinks_as_it_sharpens():
    soft = focus.spot_star(one_star_frame(5.0), 800, 300, 512)
    sharp = focus.spot_star(one_star_frame(2.0), 800, 300, 512)
    assert soft["size"] > 2 * sharp["size"]
    assert (sharp["x"], sharp["y"]) == (960, 500) and not sharp["saturated"]
    assert sharp["crop"].startswith("data:image/jpeg")


def test_a_saturated_star_is_flagged_not_lost():
    star = focus.spot_star(np.clip(one_star_frame(2.0, 200000), 0, 65535), 800, 300, 512)
    assert star["saturated"] and star["size"] > 1


def test_a_star_outside_the_spot_is_not_it():
    assert focus.spot_star(one_star_frame(2.0), 100, 100, 512) is None
