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
| Web page (live dome, camera settings, Detail/Smooth, health) + API, one server with ONVIF (D-021) | working: http://skydome.local |
| Brightness/colour matching between cameras in the stitch (D-022) | working: live, ~1 ms a second |
| Detail 5120² live; full-sharpness zoom, 6656² stills and raw camera frames (D-023) | working |
| Ground mask: hide houses and trees, auto-detected and editable on the page (D-024) | working |
| Night: 1 s exposures, live stacking, star stretch, Night/Day presets (D-025, D-026) | working |
| Shared exposure control, vignetting, recording, astro, the rest of the planetarium console | not started |

## Layout

```
config/     site, cameras, capture modes, mediamtx (YAML)
pipeline/   skydome_pipeline: geometry, camera models, projections, stitch tables, sky simulator
            native/: skydome-live (C++/CUDA capture, GPU stitch, encode)
tools/      coverage_report.py, identify_cameras.py, gen_udev_rules.py, calibrate_intrinsics.py,
            build_stitch_lut.py, run_live.py, run_server.py, bringup/phase0.sh
server/     skydome_server: web server + API (app.py), ONVIF (device, media, imaging, discovery),
            camera settings, live mode/stats, Jetson health
web/        the web page (React + Vite + Tailwind); web/dist is the built page, committed
systemd/    mediamtx.service, skydome-live.service, skydome-server.service
astro/      (to come)
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
uv run --project pipeline tools/run_live.py                 # the mode picked on the web page
uv run --project pipeline tools/run_live.py --mode smooth   # fixed: 2048² × 30 -> /dome-smooth
```

Watch at http://skydome.local (the web page), http://skydome.local:8889/dome
(bare WebRTC) or `rtsp://skydome.local:8554/dome`. Needs mediamtx
(`systemd/mediamtx.service`). The cameras are found by their USB ports
(`config/cameras.yaml`); `--camera zenith=/dev/video2 ...` overrides.

## Web page and ONVIF (port 80)

```bash
sudo cp systemd/skydome-server.service /etc/systemd/system/ && sudo systemctl enable --now skydome-server
uv run --project pipeline tools/run_server.py --set-password   # ONVIF login; then restart skydome-server
cd web && npm install && npm run build                       # after changing the page (on the Mac); commit web/dist
```

http://skydome.local: the live dome (pan/zoom, snapshot, full screen), the
camera settings for all five cameras, the Detail/Smooth switch (Smooth turns
itself off after 30 minutes) and the rig's health; a red night theme.
Zoomed in, the picture sharpens to the cameras' own detail; "Full detail"
saves a 6656² still and "Raw" the five camera frames as sent (D-023).
"Hide the ground" masks houses and trees below a horizon line you can
edit (D-024; start one with `tools/detect_horizon.py`). No login on the
LAN. API docs at http://skydome.local/api/docs.

ONVIF: 
NVRs (Frigate, Blue Iris, Synology, ...) find "SkyDome" by discovery, or
add `http://skydome.local/onvif/device_service` by hand (port 80). Login:
`var/onvif_credentials.yaml` (user `admin`, random password until you set
one). The camera's image settings in the NVR (exposure, gain, brightness,
contrast, saturation, sharpness, backlight, white balance) change all five
cameras together and are kept in `var/camera_settings.yaml`, the same
settings as the web page's. The snapshot needs skydome-live running.
Details: DECISIONS.md D-020, D-021.

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
