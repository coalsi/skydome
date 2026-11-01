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
| Phase 0 bring-up | run on 4 cameras 2026-10-31 (DECISIONS.md "Phase 0 findings"); USB 3 capture issue F-010 |
| Lens calibration (`tools/calibrate_intrinsics.py`) | done, tested on rendered fisheye/rectilinear views |
| Live dome: capture → CUDA stitch → H.264 → mediamtx (D-016) | working: 4096² × 15 and 2048² × 30 steady, plays in Chrome over WebRTC |
| ONVIF camera for NVRs: discovery, stream, snapshot, imaging settings (D-020) | working; tested with onvif-zeep |
| Exposure matching, web UI/API server, recording, astro | not started |

## Layout

```
config/     site, cameras, capture modes, mediamtx (YAML)
pipeline/   skydome_pipeline: geometry, camera models, projections, stitch tables, sky simulator
            native/: skydome-live (C++/CUDA capture, GPU stitch, encode)
tools/      coverage_report.py, identify_cameras.py, gen_udev_rules.py, calibrate_intrinsics.py,
            build_stitch_lut.py, run_live.py, run_onvif.py, bringup/phase0.sh
server/     skydome_server: ONVIF (device, media, imaging, discovery), camera settings
systemd/    mediamtx.service, skydome-live.service, skydome-onvif.service
astro/ web/   (to come)
```

## Development (Mac or Jetson)

```bash
cd pipeline && uv sync && uv run pytest
cd .. && uv run --project pipeline pytest server/tests
```

Sky coverage for different side-camera elevations and lens types:

```bash
uv run --project pipeline tools/coverage_report.py --png docs/coverage/coverage.png
```

## Live dome (on the Jetson)

```bash
cmake -S pipeline/native -B pipeline/native/build && cmake --build pipeline/native/build -j
uv run --project pipeline tools/run_live.py                 # Detail 4096² × 15 -> /dome
uv run --project pipeline tools/run_live.py --mode smooth   # 2048² × 30 -> /dome-smooth
```

Watch at http://skydome.local:8889/dome (WebRTC) or `rtsp://skydome.local:8554/dome`.
Needs mediamtx (`systemd/mediamtx.service`). Until the cameras are named,
pass them by hand: `--camera zenith=/dev/video2 --camera north=...`.

## ONVIF (NVRs)

```bash
sudo cp systemd/skydome-onvif.service /etc/systemd/system/ && sudo systemctl enable --now skydome-onvif
uv run --project pipeline tools/run_onvif.py --set-password   # then: sudo systemctl restart skydome-onvif
```

NVRs (Frigate, Blue Iris, Synology, ...) find "SkyDome" by discovery, or
add `http://skydome.local/onvif/device_service` by hand (port 80). Login:
`var/onvif_credentials.yaml` (user `admin`, random password until you set
one). The camera's image settings in the NVR (exposure, gain, brightness,
contrast, saturation, sharpness, backlight, white balance) change all five
cameras together and are kept in `var/camera_settings.yaml`. The snapshot
needs skydome-live running. Details: DECISIONS.md D-020.

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
