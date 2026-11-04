from pathlib import Path

import pytest
from pydantic import ValidationError

from skydome_pipeline.config import Capture, load_cameras, load_modes, load_site

CONFIG = Path(__file__).resolve().parents[2] / "config"


def test_repo_config_loads():
    site = load_site(CONFIG)
    assert site.timezone == "America/Phoenix"
    cams = load_cameras(CONFIG)
    assert [c.name for c in cams.nominal_cameras()] == ["sky"]   # the all-sky camera (D-029)
    assert cams.cameras[0].device == "asi"
    modes = load_modes(CONFIG)
    assert {"day", "night", "deep", "meteor"} <= set(modes.modes)


def test_4k_yuyv_is_rejected():
    with pytest.raises(ValidationError):
        Capture(format="YUYV", width=3840, height=2160, fps=30)
    Capture(format="YUYV", width=1920, height=1080, fps=15)
    Capture(format="MJPG", width=3840, height=2160, fps=30)


def test_cameras_need_unique_names():
    from skydome_pipeline.config import CamerasConfig
    base = {"sensor": {"width": 100, "height": 100}, "lens": {"hfov_deg": 160, "projection": "equidistant"}}
    one = {"name": "sky", "device": "asi", "pointing": {"az_deg": 0, "alt_deg": 90}}
    CamerasConfig.model_validate({**base, "cameras": [one]})
    with pytest.raises(ValidationError):
        CamerasConfig.model_validate({**base, "cameras": [one, one]})
    with pytest.raises(ValidationError):
        CamerasConfig.model_validate({**base, "cameras": []})
