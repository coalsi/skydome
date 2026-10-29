# SkyDome

Five-camera all-sky observatory on a Jetson Orin Nano Super. Start with
[`handoff.md`](handoff.md) (the spec) and [`DECISIONS.md`](DECISIONS.md)
(what's been decided and what's waiting on the owner).

## Status

| Area | State |
|---|---|
| Geometry, lens models, dome + equirect projections | done, tested |
| Stitch tables + CPU reference stitcher | done, tested on a simulated sky |
| Sky coverage analysis | done — side cameras at 35° (DECISIONS.md D-009); landscape required (F-004) |
| Jetson | JetPack 7.2.1 on NVMe, MAXN SUPER, chrony, uv, mediamtx; tests pass on it (`docs/jetson-setup.md`) |
| Phase 0 bring-up script, camera identification, udev rules | dry-run on the Jetson; waiting for cameras |
| Lens calibration (`tools/calibrate_intrinsics.py`) | done, tested on rendered fisheye/rectilinear views |
| Capture, CUDA stitch, encode, mediamtx, server, web UI, astro | not started |

## Layout

```
config/     site, cameras, capture modes (YAML)
pipeline/   skydome_pipeline: geometry, camera models, projections, stitch tables, sky simulator
tools/      coverage_report.py, identify_cameras.py, gen_udev_rules.py, calibrate_intrinsics.py,
            bringup/phase0.sh
astro/ server/ web/ systemd/   (to come)
```

## Development (Mac or Jetson)

```bash
cd pipeline && uv sync && uv run pytest
```

Sky coverage for different side-camera elevations and lens types:

```bash
uv run --project pipeline tools/coverage_report.py --png docs/coverage/coverage.png
```

## Camera day

Step by step: [`docs/camera-day.md`](docs/camera-day.md). Print
[`docs/charuco_board.pdf`](docs/charuco_board.pdf) at 100% first.

## Day one on the Jetson

Install first: [`docs/jetson-setup.md`](docs/jetson-setup.md) (JetPack 7.2.1 from a Mac,
straight to NVMe; DECISIONS.md D-010).

```bash
tools/bringup/phase0.sh --gst                 # inventory + 5-camera load tests
python3 tools/identify_cameras.py             # snapshots at http://skydome.local:8090
python3 tools/identify_cameras.py --assign north=<ID_PATH> ...
sudo python3 tools/gen_udev_rules.py --install   # creates /dev/skycam-<name>
```

## License

MIT, see [`LICENSE`](LICENSE).
