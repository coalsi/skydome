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


def run_tool(lut_path, inputs, out_path, extra=()):
    args = [str(TOOL), "--lut", str(lut_path), "--out", str(out_path), *extra]
    for name, (path, w, h, fmt) in inputs.items():
        args += ["--input", f"{name}={path}:{w}x{h}:{fmt}"]
    run_tool.stdout = subprocess.run(args, check=True, capture_output=True, text=True).stdout
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
    # A stacked camera hands the stitch float planes: same picture (D-026)
    fy, fu, fv = run_tool(lut_path, inputs, tmp_path / "outf.i420", ["--float"])
    for name, a, b in (("Y", gy, fy), ("U", gu, fu), ("V", gv, fv)):
        assert np.abs(a.astype(int) - b.astype(int)).max() <= 1, f"float {name} differs"
    ry = to_u8(grid_stitch(glut, ys, fill=0))
    ru = to_u8(grid_stitch(glut, us, fill=128, out_div=2))
    rv = to_u8(grid_stitch(glut, vs, fill=128, out_div=2))
    for name, got, ref in (("Y", gy, ry), ("U", gu, ru), ("V", gv, rv)):
        diff = np.abs(got.astype(int) - ref.astype(int))
        # float32 on both sides; only rounding at .5 boundaries may differ
        assert diff.max() <= 1, f"{name} differs by up to {diff.max()}"
        assert (diff > 0).mean() < 0.01, f"{name}: {(diff > 0).mean():.2%} of pixels differ"


def test_balance_evens_out_a_mismatched_camera(tmp_path):
    """PhotoBalance (D-022): one camera exposed darker and white-balanced bluer
    than the rest is brought back in line with its neighbours."""
    cams = rig(FisheyeLens)
    glut = build_grid_lut(AllSkyProjection(size=DOME), cams, step=1)
    lut_path = tmp_path / "t.lut"
    glut.save(lut_path)
    inputs = {}
    for cam in cams:
        y = 40 + 150 * render_camera(cam, SkyScene(grid_step_deg=15, grid_width_deg=1.5))
        u = np.full((cam.height // 2, cam.width // 2), 128.0)
        if cam.name == "north":
            y, u = y * 0.6, u + 15
        p = tmp_path / f"{cam.name}.yuv"
        v = np.full_like(u, 128.0)
        p.write_bytes(to_u8(y).tobytes() + to_u8(u).tobytes() + to_u8(v).tobytes())
        inputs[cam.name] = (p, cam.width, cam.height, "i420")
    run_tool(lut_path, inputs, tmp_path / "out.i420", ["--balance"])
    corr = {}
    for line in run_tool.stdout.splitlines():
        parts = line.split()
        if parts[:1] == ["balance"] and "gain" in parts:
            corr[parts[1]] = {k: float(parts[parts.index(k) + 1]) for k in ("gain", "u", "v")}
    assert set(corr) == {c.name for c in cams}
    others = [corr[c.name] for c in cams if c.name != "north"]
    g_other = np.mean([o["gain"] for o in others])
    assert corr["north"]["gain"] / g_other == pytest.approx(1 / 0.6, rel=0.05)
    # North's U ends up where the others' is: 128 + g*15 + u_north == 128 + u_other
    u_other = np.mean([o["u"] for o in others])
    assert corr["north"]["gain"] * 15 + corr["north"]["u"] == pytest.approx(u_other, abs=1.0)
    assert all(abs(c["v"]) < 1.0 for c in corr.values())
    # Corrections stay as small as they can: brightness is shared out, not all put on north
    assert np.exp(np.mean([np.log(c["gain"]) for c in corr.values()])) == pytest.approx(1.0, rel=0.1)
