"""The CUDA stitch kernel matches its NumPy twin (DECISIONS.md D-016).

Runs only where pipeline/native has been built (the Jetson):
    cmake -S pipeline/native -B pipeline/native/build && cmake --build pipeline/native/build -j
"""

import subprocess
from pathlib import Path

import numpy as np
import pytest

from skydome_pipeline.camera import FisheyeLens
from skydome_pipeline.gpu_lut import build_grid_lut, grid_stitch
from skydome_pipeline.projection import AllSkyProjection
from skydome_pipeline.simulate import SkyScene, render_camera

from test_stitch import DOME, rig

TOOL = Path(__file__).resolve().parents[1] / "native" / "build" / "skydome-stitch-files"
pytestmark = pytest.mark.skipif(not TOOL.exists(), reason="native stitch tool not built (Jetson only)")


def to_u8(x):
    return np.clip(np.round(x), 0, 255).astype(np.uint8)


def run_tool(lut_path, inputs, out_path):
    args = [str(TOOL), "--lut", str(lut_path), "--out", str(out_path)]
    for name, (path, w, h, fmt) in inputs.items():
        args += ["--input", f"{name}={path}:{w}x{h}:{fmt}"]
    subprocess.run(args, check=True, capture_output=True, text=True)
    raw = np.fromfile(out_path, np.uint8)
    y = raw[:DOME * DOME].reshape(DOME, DOME)
    c = DOME // 2
    u = raw[DOME * DOME:DOME * DOME + c * c].reshape(c, c)
    v = raw[DOME * DOME + c * c:].reshape(c, c)
    return y, u, v


@pytest.mark.parametrize("fmt", ["y42b", "i420"])
def test_kernel_matches_twin(tmp_path, fmt):
    cams = rig(FisheyeLens)
    glut = build_grid_lut(AllSkyProjection(size=DOME), cams, step=1)
    lut_path = tmp_path / "t.lut"
    glut.save(lut_path)

    # Different patterns in Y, U and V so a plane mix-up shows
    ys, us, vs, inputs = [], [], [], {}
    for cam in cams:
        y = to_u8(30 + 200 * render_camera(cam, SkyScene(grid_step_deg=15, grid_width_deg=1.5)))
        full_u = to_u8(128 + 300 * render_camera(cam, SkyScene(grid_step_deg=20, grid_width_deg=2.5, glow=False)) - 60)
        full_v = to_u8(90 + 250 * render_camera(cam, SkyScene(grid_step_deg=10, grid_width_deg=2.0)))
        if fmt == "y42b":
            u, v = full_u[:, ::2], full_v[:, ::2]
        else:
            u, v = full_u[::2, ::2], full_v[::2, ::2]
        u, v = np.ascontiguousarray(u), np.ascontiguousarray(v)
        p = tmp_path / f"{cam.name}.yuv"
        p.write_bytes(y.tobytes() + u.tobytes() + v.tobytes())
        inputs[cam.name] = (p, cam.width, cam.height, fmt)
        ys.append(y)
        us.append(u)
        vs.append(v)
    # One camera missing, as when it's unplugged
    del inputs["east"]
    east = glut.names.index("east")
    ys[east] = us[east] = vs[east] = None

    gy, gu, gv = run_tool(lut_path, inputs, tmp_path / "out.i420")
    ry = to_u8(grid_stitch(glut, ys, fill=0))
    ru = to_u8(grid_stitch(glut, us, fill=128, out_div=2))
    rv = to_u8(grid_stitch(glut, vs, fill=128, out_div=2))
    for name, got, ref in (("Y", gy, ry), ("U", gu, ru), ("V", gv, rv)):
        diff = np.abs(got.astype(int) - ref.astype(int))
        # float32 on both sides; only rounding at .5 boundaries may differ
        assert diff.max() <= 1, f"{name} differs by up to {diff.max()}"
        assert (diff > 0).mean() < 0.01, f"{name}: {(diff > 0).mean():.2%} of pixels differ"
