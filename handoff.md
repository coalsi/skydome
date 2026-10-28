# SKYDOME — All-Sky Observation Rig · Agent Handoff

> This document is the single source of truth for building the SkyDome software stack. Read it fully before touching anything. Hardware is already purchased and on its way. Your job is the OS, the pipeline, and the web control surface.

---

## 1. What we are building (one paragraph)

A five-camera all-sky observatory running entirely on an NVIDIA Jetson Orin Nano Super. Four Sony IMX678 4K USB3 cameras point N/S/E/W at ~45° elevation and a fifth points at zenith. On-device, the five streams are undistorted, projected, and blended into one seamless hemispherical "sky dome" image at up to 30 fps, which is then (a) recorded to NVMe, (b) live-streamed to browsers over WebRTC with low latency, (c) exposed on the LAN as an RTSP + ONVIF Profile S camera so NVRs and other software can ingest it, and (d) served through a local web app that works like a planetarium console: pan/zoom the dome, switch day/night/deep-sky exposure modes, overlay a star map, and — the clever part — plate-solve the live frames against a star catalog so the rig *calibrates its own orientation from the stars*, knows where north and zenith are, and can label every star, planet, and satellite it sees.

The owner is a senior software developer and systems architect. Write production-grade code, not demos. Ask before making irreversible choices; otherwise make reasonable decisions and record them in `DECISIONS.md`.

---

## 2. Hardware (as ordered)

| Item | Spec | Notes |
|---|---|---|
| Compute | **NVIDIA Jetson Orin Nano Super Developer Kit, 8GB** | 6× Cortex‑A78AE, 1024‑core Ampere GPU, 8GB LPDDR5 shared CPU/GPU. 4× USB 3.2 Gen2 Type‑A (all behind one internal hub — treat total USB bandwidth as ~1 GB/s shared). 1× GbE. M.2 2280 NVMe slot. No eMMC — boots from NVMe. Power: 7–25W via included 19V barrel brick. |
| Storage | **512GB NVMe M.2 2280** | OS + ring-buffer recordings + clips + timelapses. |
| USB hub | **Powered USB‑C hub, 10 Gbps per port (USB 3.2 Gen2)** | Two cameras on the hub, three direct to Jetson ports. The hub's uplink to the Jetson must be a USB‑A‑to‑C **10 Gbps data cable**, not a charge cable. See §2.1 for the exact port map. |
| Cameras ×5 | **SVPRO 4K 60fps USB 3.0, Sony IMX678** (1/1.8″ Starvis 2, 3840×2160), **L39 M12 lens, 120° HFOV** | UVC class, driver-free. Outputs MJPEG and YUY2. 4K@60 / 1080p@60 on USB3. Likely the same board as the ELP 4K60 IMX678 module. |
| Cables | 5× short USB‑C→A USB3 cables | |
| Mount | 3D‑printed pentagonal "crown" on a 1¼″ pipe mast | Owner prints this. 4 cradles at 45° elevation facing N/S/E/W, 1 cradle at zenith. **No weatherproofing, no dew heaters** — rig is deployed outdoors in Arizona only in dry conditions while being monitored. |

### 2.1 USB topology & what plugs into what

The Jetson Orin Nano dev kit's four USB‑A ports all sit behind **one internal hub on a single 10 Gbps link**, so everything plugged into the Jetson shares ~800–900 MB/s real-world. The external hub adds no bandwidth; it only adds ports. The cameras themselves are USB 3.2 Gen1 (5 Gbps) devices.

Physical wiring (owner does this; agent writes the udev rules to match):

| Jetson port | Device |
|---|---|
| USB‑A #1 (top-left, nearest Ethernet) | **Zenith** camera, direct |
| USB‑A #2 | **North** camera, direct |
| USB‑A #3 | **South** camera, direct |
| USB‑A #4 | **External hub uplink** (USB‑A → USB‑C 10 Gbps cable) |
| Hub port 1 | **East** camera |
| Hub port 2 | **West** camera |
| Hub port 3–4 | free (keyboard/debug, USB GPS later) |

Rationale: three cameras direct spreads load across the Jetson's internal hub rather than piling five streams onto one uplink; the two hub cameras are the opposing pair (E/W) so if the hub ever drops, the dome still has N/S/zenith coverage. The hub **must be powered by its own supply** — don't let five cameras pull power through the Jetson.

Verification on bring-up: `lsusb -t` must show every camera at **5000M** and the hub uplink at **10000M**. If the hub shows 5000M, the uplink cable is Gen1 or a charge cable — replace it. Record the tree in `DECISIONS.md`.

**Bandwidth budget (per camera → total for five):**

| Format | Per camera | ×5 | Fits? |
|---|---|---|---|
| YUY2 4K30 (uncompressed) | ~500 MB/s | ~2.5 GB/s | **No** — never |
| MJPEG 4K30 | ~40–80 MB/s | ~300–400 MB/s | Yes |
| YUY2 1080p30 (uncompressed, 2×2 binned) | ~125 MB/s | ~620 MB/s | Yes, with headroom on a 10 Gbps topology |
| YUY2 1080p15 | ~62 MB/s | ~310 MB/s | Yes, comfortably |

**Capture format by mode (this is the rule):**
- **Day / Meteor (4K):** MJPEG 3840×2160 @ 30 fps. Hardware-decode with `nvjpegdec` / `nvv4l2decoder mjpeg=1`.
- **Night / Deep (1080p):** **YUY2 1920×1080 uncompressed**, 10–15 fps (30 if the budget holds). 1080p on the IMX678 is 2×2 binned → larger effective pixels, no JPEG artifacts in the noise floor, cleaner stacking. Confirm with `v4l2-ctl --list-formats-ext` that 1080p YUY2 is offered at the needed rates; if the firmware only offers YUY2 at low fps, fall back to 1080p MJPEG and note it.
- Mode switches re-negotiate the V4L2 format per camera; do it on all five in parallel and expect ~1 s of blackout, which the UI should mask.

**Non-negotiable constraints that come from this hardware:**

1. **Never capture YUY2 at 4K.** Follow the format-by-mode rule above. MJPEG is decoded with the Jetson's hardware JPEG engine (`nvjpegdec` / `nvv4l2decoder mjpeg=1` in GStreamer, or `nvjpeg` in CUDA), **not** on the CPU.
2. **Memory is 8GB shared.** Budget it. Use NVMM/zero-copy buffers between decode → CUDA → encode. Don't let Python hold full-res frames in system RAM.
3. **Camera identity must be tied to physical USB port**, not `/dev/videoN` enumeration order. Write udev rules keyed on `ID_PATH` so `/dev/skycam-north`, `-east`, `-south`, `-west`, `-zenith` are stable across reboots. Provide a `tools/identify_cameras.py` that shows a live thumbnail from each `/dev/video*` so the owner can map port → direction once.
4. **Verify manual exposure controls on day one.** Run `v4l2-ctl -d /dev/skycam-zenith --list-ctrls-menus` and record the real range of `exposure_time_absolute` (units of 100µs), `gain`, `exposure_auto`, `white_balance_automatic`, `power_line_frequency`. The night and deep-sky modes depend on how long the firmware allows exposure. If the ceiling is low (e.g. ≤ 33 ms), the stacking engine (§5.5) has to do the heavy lifting; note it in `DECISIONS.md`.

---

## 3. Operating system & base setup

- **JetPack 6.x (latest GA)** on **Ubuntu 22.04**. Flash via NVIDIA SDK Manager from an x86 Ubuntu host (or the owner's Mac via Docker if needed) directly to NVMe — do not run from microSD. Include CUDA, cuDNN, TensorRT, Multimedia API, DeepStream (optional), and VPI.
- After first boot: `sudo nvpmodel -m 2` (MAXN SUPER / 25W) and `sudo jetson_clocks`. Make this persist at boot (systemd unit).
- Install: `nvidia-jetpack`, `gstreamer1.0-*` incl. `nvidia` plugins, `v4l-utils`, `python3.10`, `uv` or `pip`, `nodejs 20`, `docker` (nvidia-container-runtime). Prefer running the pipeline **natively** (not in Docker) for USB/NVMM access; Docker is fine for the web frontend build and auxiliary services.
- Set hostname `skydome`, enable mDNS so the UI is reachable at `http://skydome.local`.
- Time: install `chrony`; the rig must have accurate UTC for astrometry/satellite prediction. Optional later: USB GPS puck for time + position when off-grid.
- Create a `skydome` system user, all services as systemd units under it. Logs to journald. Everything must come up cleanly on a cold boot with no keyboard/monitor attached.
- Repo layout (monorepo, Git):
  ```
  skydome/
    handoff.md            ← this file
    DECISIONS.md          ← you maintain this
    config/               ← YAML config (site, cameras, calibration, modes)
    pipeline/             ← C++/CUDA + Python capture/stitch/encode
    astro/                ← plate solving, catalogs, ephemerides
    server/               ← FastAPI backend, WebRTC/RTSP glue, ONVIF
    web/                  ← Vite + React (or Svelte) planetarium UI
    tools/                ← calibration & diagnostics CLIs
    systemd/              ← unit files
    docs/
  ```

---

## 4. System architecture

```
 5× UVC cams (MJPEG 4K) ──┐
                           ▼
          [capture]  v4l2src × 5 → nvjpegdec (HW) → NVMM buffers
                           ▼
          [calibrate]  per-camera fisheye model + extrinsic rotation (from plate solving)
                           ▼
          [project+blend]  CUDA remap of all 5 into ONE dome canvas
                           (azimuthal-equidistant "all-sky" projection, 4096×4096,
                            plus an equirectangular hemisphere 8192×2048 for the 3D viewer)
                           ▼
          [enhance]  optional N-frame stacking (mean/median), dark-frame subtraction,
                     hot-pixel map, auto-stretch (asinh), per-mode tone curve
                           ├──► [encode H.264/H.265 HW] ──► mediamtx ──► WebRTC (browser)
                           │                                        ├─► RTSP (LAN)
                           │                                        ├─► HLS (fallback)
                           │                                        └─► ONVIF Profile S wrapper
                           ├──► [record]  ring buffer + clips + timelapse (NVMe)
                           └──► [analysis]  plate solve (every N s), star/planet/sat labels,
                                            transient/meteor detection (phase 3)
          [control plane]  FastAPI: modes, exposure, calibration state, overlays, clips
          [web UI]  planetarium console (three.js dome + 2D all-sky + overlays)
```

Language choices: **GStreamer (C/C++ custom CUDA element or `nvivafilter`) for the hot path**; Python (FastAPI) for control, astro, orchestration; TypeScript for the web. If Python-in-the-loop can't hold 30 fps at 4K, move the loop to C++ — do not downscale to make Python fit.

---

## 5. Functional requirements

### 5.1 Capture
- Five simultaneous captures. Day/Meteor: **3840×2160 MJPEG @ 30 fps**. Night/Deep: **1920×1080 YUY2 uncompressed** at 10–15 fps (see §2.1 for the bandwidth rule and §5.4 for modes).
- Per-camera control of exposure, gain, white balance, power-line frequency via V4L2 (UVC extension controls). Expose these through the API.
- Frame timestamps from the kernel (`v4l2` monotonic) mapped to UTC; keep a per-camera latency/drop counter; surface in the UI diagnostics panel.
- Graceful handling of a camera disappearing (USB glitch): keep the dome running with 4 cameras, auto-reattach, alert in UI.

### 5.2 Calibration (intrinsics)
- Each camera modeled with **OpenCV fisheye (Kannala‑Brandt)** or omnidirectional model; the 120° lens is wide but may be near-rectilinear — fit and compare both, pick lowest reprojection error.
- Tool `tools/calibrate_intrinsics.py`: capture a checkerboard/ChArUco sequence per camera, fit, save to `config/calibration/<cam>.yaml`. The owner will do this once per camera indoors.
- **Fallback / refinement from stars:** intrinsics can also be refined from plate-solved star positions across the whole field (astrometric distortion fit). Implement after the basic pipeline works.

### 5.3 Alignment from the stars (extrinsics) — core feature
- The rig must determine, from the sky itself, the exact orientation (rotation matrix) of every camera relative to the **local horizontal (alt/az) frame**, and from that, true north, zenith, and the horizon line.
- Approach:
  1. Grab a still from each camera in Night mode.
  2. **Plate solve** each with a local solver — evaluate in this order: **`tetra3` / `cedar-solve`** (fast, lost-in-space, pure Python, good for wide fields) → **`astrometry.net` solve-field** with local index files (robust, heavier). Fields are ~120°, so build/choose indexes for very wide FOV; it may be necessary to solve on a central crop and extend outward with the lens model.
  3. Solve gives RA/Dec of the optical axis + roll per camera. With site lat/long and UTC, convert to alt/az (`astropy` or `skyfield`). This yields each camera's rotation. Store as `config/calibration/extrinsics.yaml` with timestamp and residuals.
  4. Refine jointly: minimize star reprojection error across all 5 cameras simultaneously (overlap regions must agree). Output the final blend map.
- The rig is portable; the owner will re-level/re-point it. Provide **"Auto-align from sky"** as a single button in the UI that runs the above, shows solved/failed per camera, residual arcseconds, and the derived north offset. Also provide a manual fallback: owner enters compass heading + level, with a draggable north marker.
- Persist: last known good alignment, so the dome is usable immediately on boot and improves when a new solve succeeds.
- Site defaults in `config/site.yaml`: Surprise, AZ (approx 33.63 N, 112.37 W, 350 m) — owner will enter exact coordinates. Timezone America/Phoenix (no DST). Allow override/GPS.

### 5.4 Projection, blending, modes
- **Primary output: all-sky azimuthal-equidistant dome image** (zenith at center, horizon at the edge, north up after alignment). Resolution target 4096×4096. This is what gets recorded and streamed.
- **Secondary output: equirectangular hemisphere** for the 3D viewer (texture on a sky sphere).
- Blending: multi-band (Laplacian pyramid) or feathered linear blend in overlap zones with exposure/vignetting compensation. Seams must be invisible on a star field and on daytime clouds. Pre-compute remap LUTs on the GPU; per-frame cost should be only the remap + blend.
- **Modes** (switchable live from the UI, all parameters editable and saved as presets):
  - **Day:** 4K MJPEG, 30 fps, auto exposure with limits, auto WB, no stacking.
  - **Dusk/Night:** 1080p YUY2 (binned, uncompressed), 10–15 fps, manual exposure near the firmware max, gain elevated, stacking of the last 4–8 frames (rolling mean) → displayed at native fps but with reduced noise.
  - **Deep:** 1080p YUY2, 1–2 fps effective, exposure at max, stacking 15–60 frames (rolling median/mean with sigma clipping), asinh stretch, hot-pixel removal, optional dark-frame subtraction (tool to capture darks with lens caps on).
  - **Meteor:** 4K MJPEG, 30 fps, high gain, short exposure, feeds the transient detector (phase 3).
  - The dome canvas resolution stays constant across modes; 1080p inputs are remapped into the same 4096² canvas so overlays, alignment and the stream don't change when modes switch.
- Auto mode scheduling by sun altitude (civil/nautical/astronomical twilight) with manual override.

### 5.5 Enhancement
- Rolling stack implemented on the GPU (CUDA kernel or CuPy); frames are registered by the alignment (sky is static between frames at these rates; stars drift ~0.25°/min, so for stacks longer than ~10 s do a small shift-compensation based on sidereal rate).
- Hot-pixel map, dark frame, flat (vignetting) per camera.
- Tone/stretch presets; histogram shown in UI.

### 5.6 Recording
- **Ring buffer**: continuous H.265 (HW `nvv4l2h265enc`) of the dome output to NVMe, configurable retention (default last 3 hours, ~10 Mbps). Segmented MP4 (1‑minute files) so a crash loses ≤ 1 min.
- **Clips**: "Save last N minutes" and "mark event" from the UI; copies segments out of the ring into `/data/clips/` with sidecar JSON (mode, alignment, exposure, UTC).
- **Timelapse**: still capture every N seconds in Deep/Night mode → nightly MP4 assembly at dawn.
- **Raw stills**: option to dump per-camera full-res JPEG/PNG and the stacked dome PNG for later astrophotography processing.
- Disk watchdog: never fill the NVMe; prune ring first, then old timelapses, never clips.

### 5.7 Streaming & network
- Use **`mediamtx`** as the stream server. Pipeline publishes RTSP to it; mediamtx fans out **WebRTC (WHEP)** for the browser (sub-second), **RTSP** for the LAN, **HLS** as fallback.
- Streams: `dome` (4096² scaled to 2160² for stream, 30 fps), `dome-low` (1080²), and `cam-north/east/south/west/zenith` raw previews at 720p.
- **ONVIF Profile S**: expose `dome` as an ONVIF device so NVRs (Frigate, Blue Iris, Synology, etc.) can add it as a camera. Evaluate **`rpos`** (Node ONVIF server wrapping an RTSP stream) first; alternatives: a minimal custom ONVIF SOAP service (python-onvif-zeep / onvif-server). Must support WS-Discovery, GetCapabilities, GetProfiles, GetStreamUri, GetSnapshotUri. PTZ is not required.
- Snapshot endpoint: `GET /api/snapshot.jpg` (current dome frame).
- Everything on LAN only by default; no cloud. Basic auth optional; off by default.

### 5.8 Web UI — the planetarium console (`http://skydome.local`)
Single-page app. Must be excellent on a laptop and usable on a phone.

- **Views:**
  - *Dome* (default): the 2D all-sky image, north up, pan/zoom (mouse wheel/pinch), rotate, click any star for its name/magnitude/alt-az/RA-Dec.
  - *Sphere*: three.js sky sphere textured with the equirectangular stream; orbit controls so the owner can "look around" from inside the dome, with FOV zoom.
  - *Cameras*: 5 raw thumbnails with per-camera exposure/gain/WB sliders, histogram, focus assist (Laplacian variance number + zoomed crop) — needed when manually focusing the M12 lenses at infinity.
  - *Diagnostics*: fps per camera and per stage, GPU/CPU/mem/temp (tegrastats), USB bandwidth, drops, disk, alignment residuals, service health.
- **Overlays (toggleable, drawn in the browser over the live frame):**
  - Alt/az grid, horizon, cardinal points, zenith marker.
  - Constellation lines + names, bright star labels (Hipparcos/Yale BSC), planets, Moon phase, Sun position/twilight shading.
  - Satellites: ISS, Starlink trains, anything bright — from CelesTrak TLEs via `skyfield`, cached offline, refreshed daily when online. Show predicted passes for the next 2 h.
  - Meteor shower radiants (IMO list) for the current date.
  - Field boundaries of each camera (so the owner sees where seams are).
- **Controls:** mode switch (Day/Night/Deep/Meteor/Auto), presets editor, stacking depth, stretch, "Auto-align from sky", manual north/level adjust, record/clip/mark, timelapse on/off, snapshot/download, full-screen, dark UI theme with a red "night-vision" theme toggle.
- **Status bar:** UTC + local time, LST, sun/moon altitude, current mode, fps, alignment age/quality, disk free.
- Tech: Vite + React + TypeScript, Tailwind, three.js, WebRTC via WHEP client. Overlay geometry computed server-side (FastAPI returns projected screen coordinates for the current frame given alignment) so the browser just draws.

### 5.9 API
- FastAPI, OpenAPI docs at `/docs`. REST for state/config, WebSocket for live telemetry and overlay coordinates (10 Hz). All config changes are persisted to YAML and hot-applied without restarting capture where possible.

---

## 6. Phased delivery (do these in order; demo each before moving on)

**Phase 0 — Bring-up (day 1)**
- JetPack on NVMe, power mode, udev camera naming per the §2.1 port map, `identify_cameras.py`, `lsusb -t` check (cams at 5000M, hub uplink at 10000M), record real V4L2 control ranges and the full `--list-formats-ext` output per camera, prove **both** 5× 4K30 MJPEG (HW decode) and 5× 1080p YUY2 capture simultaneously (`gst-launch` first), log actual USB throughput and GPU load for each. Commit findings to `DECISIONS.md`.

**Phase 1 — Dome, stream, UI skeleton**
- Intrinsic calibration tool; manual extrinsics (owner enters rough orientation); CUDA remap+blend into the dome; H.265 ring buffer; mediamtx with WebRTC; web UI showing the live dome with pan/zoom and mode switching; per-camera controls.

**Phase 2 — Star alignment & planetarium**
- Plate solving, auto-align, joint refinement; overlays (grid, constellations, planets, satellites); click-to-identify; Deep mode stacking; timelapse; ONVIF.

**Phase 3 — Smarts**
- Transient/meteor detection (frame differencing + line detection first; TensorRT model later), event clips with auto-labels, nightly report page, optional GPS, optional push notifications.

---

## 7. Acceptance criteria (how we know it's done)
- Cold boot → live dome in browser in < 90 s with no human interaction.
- Day mode: 30 fps sustained for 1 h, GPU < 80 %, no dropped cameras.
- Seams invisible in a stacked night frame; stars in overlap zones are single points, not doubles.
- Auto-align succeeds on a clear night with ≥ 4/5 cameras solved and residual < 2 arcmin; north marker within 0.5° of a known reference.
- Clicking Vega labels it Vega.
- Frigate (or any ONVIF NVR) discovers `skydome` and plays the stream.
- 3 h ring buffer + "save last 10 min" works and survives a power pull.

---

## 8. Things to ask the owner before deciding
- Exact site coordinates (or wait for GPS).
- Whether the rig will ever be on a cellular/Starlink link (affects stream bitrates and TLE/catalog caching).
- Preferred clip format/location (local only vs. sync to a NAS).
- Whether to run DeepStream or a custom pipeline (recommend custom GStreamer + CUDA; DeepStream only if phase 3 wants its inference plugins).

## 9. Things NOT to do
- Don't capture YUY2 at 4K. Don't decode JPEG on the CPU. Don't downscale the cameras to make a slow pipeline fit.
- Don't hardcode `/dev/videoN`.
- Don't ship a Windows-only or cloud-dependent component.
- Don't add weatherproofing/heater control — it's out of scope for this rig.
- Don't silently change a decision in this document; record it in `DECISIONS.md` and flag it.
