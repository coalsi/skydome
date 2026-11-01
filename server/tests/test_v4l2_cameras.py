"""The real ioctls against the connected cameras (Jetson only; read-only)."""

import pytest

from skydome_server.v4l2 import Device, find_cameras

cams = find_cameras()
pytestmark = pytest.mark.skipif(not cams, reason="no SkyDome cameras connected")


def test_controls_match_phase0():
    with Device(cams[0].device) as d:
        c = d.controls()
        assert (c["exposure_time_absolute"].minimum, c["exposure_time_absolute"].maximum) == (1, 10000)
        assert (c["gain"].minimum, c["gain"].maximum) == (0, 190)
        assert d.get(c["brightness"].id) in range(c["brightness"].minimum, c["brightness"].maximum + 1)
