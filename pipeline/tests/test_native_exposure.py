"""The all-sky camera's automatic exposure (native/src/exposure.h, D-029):
daylight stays smooth video at gain 0, twilight raises the gain before the
exposure gets long, night goes to long exposures at the top gain, and a sky
too dark for the longest stops there."""

import shutil
import subprocess
from pathlib import Path

import pytest

NATIVE = Path(__file__).resolve().parents[1] / "native"
BUILT = NATIVE / "build" / "skydome-exposure-check"


@pytest.fixture(scope="module")
def check(tmp_path_factory) -> Path:
    if BUILT.exists():
        return BUILT
    cxx = shutil.which("c++") or shutil.which("g++") or shutil.which("clang++")
    if not cxx:
        pytest.skip("no C++ compiler and no native build")
    out = tmp_path_factory.mktemp("exposure") / "skydome-exposure-check"
    subprocess.run([cxx, "-std=c++17", "-O2", "-I", str(NATIVE / "src"), str(NATIVE / "src" / "exposure_check.cpp"),
                    "-o", str(out)], check=True)
    return out


def settle(check: Path, brightness: float) -> tuple[int, int, float]:
    out = subprocess.run([str(check), str(brightness)], capture_output=True, text=True, check=True).stdout.split()
    return int(out[0]), int(out[1]), float(out[2])


def test_daylight_is_a_short_exposure_at_no_gain(check):
    exposure, gain, level = settle(check, 1e-4)
    assert gain == 0 and exposure < 66667
    assert level == pytest.approx(0.18, rel=0.05)


def test_twilight_raises_the_gain_and_keeps_the_video_smooth(check):
    exposure, gain, level = settle(check, 1e-6)    # 0.18 needs 180 ms at gain 0: 1/15 s and ~9 dB
    assert exposure == 66667 and 0 < gain < 200
    assert level == pytest.approx(0.18, rel=0.05)


def test_night_is_a_long_exposure_at_the_top_gain(check):
    exposure, gain, level = settle(check, 1e-9)    # needs ~18 s at 20 dB
    assert gain == 200 and 66667 < exposure <= 20_000_000
    assert level == pytest.approx(0.18, rel=0.05)


def test_a_darker_sky_stops_at_the_longest_exposure(check):
    exposure, gain, level = settle(check, 1e-12)
    assert (exposure, gain) == (20_000_000, 200)
    assert level < 0.18


def test_an_overexposed_frame_comes_back_down(check):
    exposure, gain, level = settle(check, 1.0)     # saturated even at the shortest: stays there
    assert (exposure, gain) == (32, 0)
