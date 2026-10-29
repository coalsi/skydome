# DECISIONS

Running log of decisions for the SkyDome stack (handoff §1, §9).

* **D-nnn** — decisions made within the handoff's latitude.
* **F-nnn** — **flagged**: departs from, or conflicts with, `handoff.md`, or
  needs the owner's call. Nothing marked F is applied silently.
* **Phase 0 findings** — filled in from `tools/bringup/phase0.sh` on day one.

---

## Flagged — needs the owner

### F-001 · USB topology: handoff vs. enclosure plan — **resolved → D-011**
`handoff.md` §2.1: zenith/north/south direct into Jetson USB-A ports 1–3, the
hub's uplink in port 4, east/west on the hub; 5× USB-C→A camera cables.
The enclosure design session assumed all five cameras go through the RSH-H7C
hub (USB-C→C cables) and the hub uplinks to one Jetson port.

Both funnel into the same single 10 Gbps link inside the Jetson, so bandwidth
is the same. The handoff layout keeps N/S/zenith alive if the hub drops,
which is a real advantage. **Recommendation: follow the handoff.** The
enclosure supports it (the Jetson's USB-A ports face an open area of the
tray). Only the cable list changes: 3× right-angle USB-C→A + 2× right-angle
USB-C→C for the cameras, 1× right-angle USB-C→A 10 Gbps for the hub uplink.
Note the RSH-H7C has 7 USB-C downstream ports, not the 4 the handoff's table
implies; the udev rules key on whichever ports are used.

*Update 2026-10-28:* the enclosure geometry now favours the handoff layout.
The hub's middle top ports sit under the north camera. The fit checks
assume only ports 4–6 (counted from the DC-jack end, starting at 0) are
used, which allows at most 3 cameras on the hub. All five through the hub
would crowd the north camera's plug.

### F-002 · Mount: handoff text is out of date — **resolved → D-012**
`handoff.md` §2 describes "a pentagonal crown on a 1¼″ pipe mast with
cradles". What has actually been designed (OpenSCAD, `../skycam_mount.scad`,
STLs in `../stl/`) is a two-piece enclosure: a square-pyramid camera head
that snaps onto a tray holding the Jetson (in its stock base) and the USB
hub, with magnet feet and a centre cup for a 5/8″ light-stand stud.
A 1¼″ pipe mast would need a different adapter. **Owner to confirm the mast**;
then §2 of the handoff gets updated to match.

### F-003 · Side-camera elevation: 45° may leave a gap at the horizon — **resolved → D-009**
`tools/coverage_report.py` (120° HFOV, 16:9, landscape) gives:

| lens projection | side cams at 45° | side cams at 35° |
|---|---|---|
| equidistant (fisheye-like) | gap-free only above **11.5°**; 88% of sky seen | gap-free down to 1.5° |
| rectilinear | gap-free down to 1.0° | gap-free down to 0° |

Seam overlaps at 35° stay generous (≥ 31° between side cameras near the
horizon, ≥ 32° between side and zenith), which plate solving and blending
need. **Recommendation: 35° elevation** (mount `tilt = 55`), unless day-one
calibration shows the lens is close to rectilinear. The enclosure model
passes all collision checks at `tilt = 55`. Owner decision before printing.

### F-004 · Side cameras must be mounted landscape
With the sensor turned 90° (portrait), neighbouring side cameras don't
overlap near the horizon at all: gap-free coverage only above ~38°.
The long side of the sensor must be parallel to the horizon.

*Update 2026-10-28:* the camera's USB-C socket is a vertical socket under
the bottom board, facing straight back (product photo; owner). The hub no
longer has edge slots, the seats are square, and the camera can turn to any
of 4 positions at install. **No longer a print dependency.** At install,
choose the landscape rotation whose socket edge has room for the plug; the
`skycam_mount.scad` header lists the edge per camera. Fit checks pass with
right-angle plug heads up to 15 mm long (socket assumed 9.5 mm deep).

### F-005 · Lens projection and true HFOV unknown
"120° HFOV" is from the listing. Whether the lens is fisheye-like or close
to rectilinear (and whether 120° is horizontal or diagonal) changes coverage
a lot (F-003). Settled by `calibrate_intrinsics.py` on day one; until then
`config/cameras.yaml` assumes equidistant, the conservative case.

### F-006 · Flashing host
SDK Manager needs an **x86_64 Ubuntu** host; the owner's Mac is Apple
Silicon, where SDK Manager's flashing doesn't run reliably even in Docker.
Options: (a) any x86_64 Ubuntu 22.04 machine or VM with USB passthrough,
(b) NVIDIA's JetPack SD-card image first, then move the root filesystem to
NVMe. **Owner: is an x86 Linux box available?**

*Update 2026-10-28:* the owner has only Macs. NVIDIA's current method
(JetPack 7.2+) needs no Linux host at all. Write the Jetson ISO to a
USB stick with Etcher on the Mac, boot the Jetson from it, and install
straight onto the NVMe. SD-card images are discontinued from 7.2 onwards.
See F-007 for the version choice this forces.

### F-007 · JetPack 7.2.1 (Ubuntu 24.04) vs. the handoff's JetPack 6.x (22.04) — **resolved → D-010**
The handoff (§4) says "JetPack 6.x (latest GA) on Ubuntu 22.04". Since then
NVIDIA has shipped JetPack 7.2.1 for the Orin Nano (Jetson Linux r39.2.1,
Ubuntu 24.04, CUDA 13.2). It is now the documented default, installed by
ISO from a USB stick.

* **7.2.1 via ISO.** Mac-only friendly and installs straight to NVMe
  (handoff: "do not run from microSD"). Python becomes 3.12, so D-002
  moves from 3.10 to 3.12; the pipeline code has no 3.10-specific
  dependencies. Risk: newer and less community-tested on Orin. Phase 0 must
  prove `nvv4l2decoder mjpeg=1` and `nvv4l2h265enc` before anything is
  built on it.
* **6.2.x via SD-card image** (still downloadable from the archive). It
  matches the handoff, but it boots from microSD and the system then has
  to be moved to the NVMe by hand.

*Update 2026-10-29:* installed and booting from NVMe (see Phase 0 findings).
First-boot setup only appeared after a second firmware capsule update ran
on a later boot; before that the display showed only a cursor and the
system had no user account.

Requirement for either: Jetson UEFI firmware ≥ 36.0. To check, press Esc
at the NVIDIA logo. Older firmware needs NVIDIA's microSD bridge
(JetPack 5.1.3 image), which Etcher on a Mac can also write.
Sources: NVIDIA Orin Nano dev kit Quick Start and firmware-update pages,
JetPack 7.2.1 release notes. Either way, reinstalling
costs about an hour, so Phase 0 results can reverse the choice.

### F-008 · The Orin Nano has no hardware video encoder — **resolved → D-014**
Handoff §5 records the ring buffer with `nvv4l2h265enc`, and the live
stream path (§4 diagram) encodes "H.264/H.265 HW". On the installed system
there is no `nvv4l2h265enc` (or any `nvv4l2*enc`) and no encoder device
node. That matches the hardware: NVIDIA ships NVENC only on Orin NX and
AGX Orin; the Orin Nano encodes on the CPU (NVIDIA rates it at about one
1080p30 stream on 1–2 cores). The hardware **decoder** is present
(`nvv4l2decoder`), so MJPEG capture is unaffected. `x264enc` and
`x265enc` are installed.

Options:
* **(a) CPU encode** with `x264enc` (ultrafast/zerolatency) for both the
  live stream and the ring buffer, at a reduced size/rate (e.g. 2048²
  dome at 10–15 fps, or 1440² at 30). Six A78AE cores are otherwise
  mostly idle since decode is on NVDEC and stitching on the GPU.
* **(b) Swap the module** for an Orin NX 16 GB (same carrier, has NVENC).
  Keeps the handoff's 4K30 H.265 targets; costs money.

*Measured 2026-10-29* (MAXN_SUPER, frames pre-rendered to RAM so only the
encoder is timed; 150 frames each; `x264enc speed-preset=ultrafast
tune=zerolatency bitrate=12000`). Content made little difference (noisy
night sky, pure noise and a smooth day sky all within ~10%):

| dome size | x264, 6 threads | x264, 4 threads | x265 ultrafast |
|---|---|---|---|
| 1440² | 88–101 fps | 73 fps | 5–15 fps |
| 2048² | 52–54 fps | 42–43 fps | 3–8 fps |
| 2880² | 29–32 fps | 23–25 fps | — |
| 4096² | 17–18 fps | 14–15 fps | — |

x265 is unusable on this CPU. The hardware **JPEG** encoder (`nvjpegenc`,
NVJPG) is present and did 4096² at 28 fps.

**Recommendation (approved and revised, D-014):** option (a), no new hardware.
* One `x264` H.264 encode of the dome at **2048² × 30 fps** on 4 threads
  (~70% of what 4 threads can do, leaving 2 cores for everything else),
  shared by the live stream and the ring buffer. H.264 also plays in every
  browser over WebRTC; H.265 over WebRTC is patchy.
* Full-resolution 4096² frames (stills, timelapse frames, plate solving,
  raw dumps) go through the hardware JPEG encoder, not video.
* Night/Deep modes run at a few frames per second (long exposures), so the
  video encode can step up to 2880² or 4096² there.
* Revisit (b) only if 4K30 *video* turns out to matter.
Not yet measured: the same encode with the 5-camera decode and the CUDA
stitch running alongside (camera day).

---

## Decisions

### D-001 · Repository layout
Monorepo at `SkyCamera/skydome/` exactly as handoff §3. The enclosure CAD
stays one level up (`SkyCamera/skycam_mount.scad`, `stl/`, `viewer/`) so the
open OpenSCAD session and the 3D viewer keep working; it can move into
`hardware/` later.

### D-002 · Python toolchain
Python 3.12 (what JetPack 7.2.1 / Ubuntu 24.04 ships; was 3.10 before
D-010) with `uv`, one `pyproject.toml` per component (`pipeline/` now;
`server/`, `astro/` later). Development on the Mac uses the same version.

### D-003 · Coordinate conventions
* World frame: local ENU — x east, y north, z up.
* Azimuth from north toward east; altitude above the horizon.
* Camera frame: OpenCV — x right, y down, z forward.
* Rotations are world-from-camera (columns = camera axes in ENU).
* Dome image: zenith centre, horizon at the rim, north up, **east left**
  (the conventional view of the sky from below; `east_left=False` gives a map
  view).
* Equirect: azimuth 0–360 left to right starting at north, zenith at top.

### D-004 · Camera roll convention
Roll 0 = image rows level with the horizon, image "up" toward the zenith.
Positive roll turns the camera clockwise as seen from behind it.

### D-005 · Stitch tables on a coarse grid
Full-resolution tables for a 4096² dome would be 5 cameras × 4096² × 3
floats ≈ 1 GB — too much of the 8 GB shared memory. The GPU path will store
the tables every 8th pixel (≈ 16 MB) and interpolate per pixel in the kernel.
The NumPy implementation in `pipeline/skydome_pipeline/lut.py` builds them at
full resolution and is the correctness reference the CUDA kernel is tested
against.

### D-006 · Feathered blending first
Per-camera weight ramps smoothly to 0 over the outer 6% of each image width,
normalised across cameras. Multi-band blending only if feathering shows
visible seams on real daytime clouds.

### D-007 · Simulator-backed acceptance test
`pipeline/tests/test_stitch.py` renders a synthetic sky into the five
cameras, stitches it, and asserts every isolated star is found exactly once
within 0.5 px (handoff §7 "single points, not doubles") for both lens
projections. Passing today with ideal calibration.

### D-008 · Custom GStreamer + CUDA, not DeepStream (pending §8 confirmation)
Following the handoff's recommendation; DeepStream only if phase 3 wants
its inference plugins.

### D-009 · Side cameras at 35° elevation (owner, 2026-10-28)
Departs from handoff §1/§2 ("~45° elevation"), which stays verbatim; this
entry supersedes it. Reason: F-003. Applied:

* `../skycam_mount.scad`: `tilt = 55`. The hub grows from 49.4 to 55.0 mm
  tall (footprint unchanged); the tray and test plate are unchanged. All
  eight collision checks pass, with a deliberate-overlap control confirming
  the checks detect contact.
* Printed upside down, a 55° face would make the edge of each side
  camera's window nearest the skirt a 35° overhang. `seat_cut()` now leans
  the window walls 10° (`tilt − 45`) toward the skirt so every window edge
  is ≥ 45°. The board recess and M2 holes stay square to the face.
* `config/cameras.yaml`: side `alt_deg: 35`. The stitch tests and the
  `coverage_report.py --png` maps use 35° by default.

Auto-align still measures the real pointing; 35° is only the starting guess.

### D-010 · JetPack 7.2.1, installed by ISO straight to NVMe (owner, 2026-10-28)
Departs from handoff §4 (JetPack 6.x on Ubuntu 22.04); see F-007. Jetson
Linux r39.2.1, Ubuntu 24.04, CUDA 13.2. The ISO is written to a USB stick
with balenaEtcher on the Mac and boots on the Jetson, which installs onto
the NVMe. No microSD and no Linux host are needed, which also settles
F-006. Fallback if Phase 0 shows hardware MJPEG decode or H.265 encode
broken on r39: JetPack 6.2.x by SD-card image, moved to NVMe.
Step-by-step: `docs/jetson-setup.md`.

### D-011 · USB wiring: 3 cameras direct, 2 on the hub (owner, 2026-10-29)
Resolves F-001 as the handoff has it: zenith, north and south into the
Jetson's USB-A ports; east and west on the RSH-H7C, hub ports 4–6 only
(the enclosure's fit checks), with only the used ports switched on. All
USB-A ports share the carrier's one 10 Gbps link either way, so this costs
no bandwidth and keeps three cameras running if the hub fails. Cameras
are named by physical port (udev `ID_PATH`), so cables must not move
between ports after `identify_cameras.py` has run.

### D-012 · Mast: a photography light stand (owner, 2026-10-29)
Resolves F-002. The standard light-stand top is a 5/8″ (16 mm) spigot
with a 1/4″-20 male thread on its tip. The tray already fits it: a
16.5 mm cup, 15 mm deep, takes the spigot, and the 1/4″-20 × 8 mm heat-set
insert above the cup takes the threaded tip, so the enclosure screws onto
the stand. Check that the stand's threaded tip is at most ~9 mm long (the
insert hole is 9.5 mm deep). Outdoors, weigh the stand's legs down
(sandbag) against wind. Handoff §2's pipe-mast text stays as written; this
entry supersedes it.

### D-013 · Site, time and settings come from the web UI (owner, 2026-10-29)
No coordinates in the repo. On first start the web app shows a **setup
page**, and every setting stays editable there afterwards
(`config/site.yaml` is just where it's saved):
* **Location:** "use this device's location" via the browser Geolocation
  API, or pick on a map / type lat, lon, elevation. Browsers only allow
  geolocation on a secure origin, so the app must serve **HTTPS** on the
  LAN (a self-signed or local-CA certificate for `skydome.local`), with
  manual entry as the fallback.
* **Time:** UTC from `chrony` (NTP) on the Jetson; the setup page shows the
  clock offset and warns if it isn't synchronised.
* **Later, from the sky:** once plate solving works, a solved zenith plus
  UTC gives latitude and longitude to within a few km; the app can offer
  that as a check on the entered location.
A USB GPS stays optional (handoff §3) for off-grid use: it gives time and
position without a network. A compass module is not worth adding: it's
good to a few degrees at best, and the magnet feet and the electronics in
the box would throw it off. Orientation comes from the stars (and can come
from the Sun's position by day), and the rig keeps it until it's moved.

### D-014 · Video: 4096² at 15 fps on the CPU by default, 2048² × 30 "Smooth" mode (owner, 2026-10-29)
Resolves F-008 with option (a), no new hardware; departs from handoff §5
(`nvv4l2h265enc` ring buffer) and the §4 "encode H.264/H.265 HW" box,
which stay verbatim. (Revised the same day from 2048² × 30 after the
full-load measurements below.)
* **Detail (default):** one `x264enc` H.264 encode of the dome at
  **4096 × 4096, 15 fps**, ultrafast/zerolatency, all cores, about
  20 Mbps. Pixels outside the dome circle are written black (they cost
  ~25% of the encode otherwise).
* **Smooth (switch in the web UI):** 2048 × 2048 at 30 fps, same encoder,
  for fast motion (birds, aircraft, storms). One mode at a time, never two
  encodes.
* **Night/Deep:** frames come at the exposure rate (a few fps), always at
  4096².
* One stream per mode: mediamtx serves it to WebRTC/RTSP/HLS and the ring
  buffer records the same stream.
* **Browser limit:** 4096 × 4096 is beyond H.264 level 5.2 (largest frame
  4096 × 2304), which many hardware decoders cap at. If a browser won't
  play it, send the Detail dome as two 4096 × 2048 halves (two encoders,
  same total cost) and let the three.js viewer join them. Decide when the
  web player is built.
* **Clocks:** run `jetson_clocks` while the pipeline is up (+15–20%
  encode); restore normal clocks when it stops.
* **Cameras:** capture at 15–20 fps in Detail mode so the shared hardware
  JPEG decoder (5 × 4K tops out at ~31 fps each) keeps headroom for the
  full-res `nvjpegenc` stills; 30 fps in Smooth mode.
* **Full resolution:** 4096² stills, timelapse frames, plate-solving input
  and raw dumps go through `nvjpegenc` (hardware NVJPG).

Measured 2026-10-29 with 5 simulated 4K30 MJPEG cameras decoding in
hardware at real time (9% of the CPU) while x264 encoded a black-cornered
dome:

| dome | normal clocks | `jetson_clocks` | CPU at target |
|---|---|---|---|
| 4096² | 21.7 fps | 25.3 fps | 15 fps ≈ 60–70% |
| 3456² | 28.8 fps | 33.4 fps | — |
| 2880² | 38.7 fps | 46.1 fps | — |
| 2048² | ~53 fps (encoder alone) | — | 30 fps ≈ 55–70% |

4096² at 30 fps is out of reach (the ceiling is ~25 fps with nothing else
running). **Still to check on camera day:** real USB bandwidth, the CUDA
stitch running alongside, and heat in the enclosure outdoors. If Detail
can't hold 15 fps, drop to 3456² before lowering the frame rate. An Orin
NX module (NVENC, same carrier) stays the upgrade path if 4K30 video
becomes a requirement.

---

## Open questions (handoff §8)

* ~~Exact site coordinates~~ → set in the web UI's setup page (D-013).
* Will the rig ever run over cellular/Starlink?
* Clips: local only, or sync to a NAS?
* ~~Mast~~ → light stand (D-012).

---

## Phase 0 findings

**System (2026-10-29, before cameras):**
* Jetson Linux R39.2.1, Ubuntu 24.04.4, kernel 6.8.12-1021-tegra; model
  "Orin Nano Engineering Reference Developer Kit Super"; 7.3 GiB RAM.
* Root on `/dev/nvme0n1p1` (456 GB). Host `skydome` on Ethernet (DHCP),
  reachable as `skydome.local`; SSH is key-only.
* Power modes: 0 = 15W, 1 = 25W (factory default), 2 = MAXN_SUPER.
  Set to MAXN_SUPER (`nvpmodel -m 2`, no reboot needed): CPU max
  1.728 GHz, GPU max 1.02 GHz.
* `nvidia-jetpack` 7.2.1-b49: CUDA 13.2.86, TensorRT 10.16.2, cuDNN 9,
  VPI. 35 GB of 456 GB used afterwards.
* `nvv4l2decoder` present with the `mjpeg` property; `nvjpegdec` present.
  No hardware encoder (F-008). Python 3.12.3. v4l2-ctl 1.26.1.
* `skydome.local` resolves from the Mac once `avahi-daemon` restarted
  after the hostname was set.
* USB: every USB-A port sits behind the carrier's onboard Realtek hub
  (0bda:0489), one 10000M link to the SoC on bus 2.
* RSH-H7C (no devices yet): two cascaded VIA Labs VL822-family hubs
  (2109:0822 USB 3 + 2109:2822 USB 2 + billboard 2109:8822 each), both at
  **10000M** ("SuperSpeed Plus Gen 2x1"). A camera on the far chip is
  3 hubs deep (onboard → hub 1 → hub 2); USB allows 5. Which of the
  hub's ports 4–6 sit on which chip is mapped on camera day.

*Cameras not connected yet.* On camera day:
`tools/bringup/phase0.sh --gst`, then paste `reports/phase0-*/summary.md`
here: USB tree speeds, V4L2 format lists, real exposure/gain ranges, and the
5× MJPEG 4K30 and 5× YUY2 1080p load-test results.
