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
**Hint, 2026-10-31:** fitting an equidistant lens to the overlaps of four
cameras on the bench (D-017) gives **110°** horizontal, not 120°. Board
calibration still to do; if 110° holds, rerun the coverage report (F-003).

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

### F-009 · Unattended outdoor runs (48 h+, winter nights)
Handoff §2: "deployed outdoors in Arizona only in dry conditions while
being monitored", and §9/§2: no weatherproofing, no dew heaters. The owner
now wants to leave it out unattended for up to 48 h, including winter
nights. The software side is D-015. What software can't fix:
* **Dew/frost on the lens windows** on clear, still winter nights (most
  likely after rain). The app can *detect* it (star count and contrast
  collapse while the sky model says it's clear) and flag it, but not
  clear it.
* **Rain** (no weatherproofing) and **wind** on the light stand (D-012).
* **Cold:** a Phoenix winter night (~0–10 °C) is inside the Jetson
  module's rated range; no action needed.
**Owner decision:** accept the risk for dry forecasts only, or revisit the
"no heaters / no weatherproofing" scope (e.g. a passive dew shield or a
small lens-window heater).

### F-010 · USB 3 camera capture drops frames; USB 2 runs clean (2026-10-31) — **cause found: USB 3 link power management**
**Resolution (evening):** USB 3 Link Power Management (U1/U2) was on for
every camera and port. With it off, all four cameras straight into the
Jetson's USB-A ports at 5000M streamed 195 frames each in 15 s
(full 15 fps) with **0 damaged**; with it on, the same test gave one camera
0 frames and the others 4–36 damaged (plain GStreamer, no SkyDome code).
The live dome then ran all four on USB 3 at 15 fps with nothing dropped.
Fix: the usbcore quirk `32e4:6678:k` (USB_QUIRK_NO_LPM) for these cameras,
on the kernel command line so it holds from boot: added to
`/boot/extlinux/extlinux.conf` on 2026-10-31 with the owner's OK (backup
`extlinux.conf.before-usb-lpm`; `docs/jetson-setup.md` step 22). Corrections to the notes below: Jetson ports 1 and 2
are fine (a USB 3 drive and the cameras link at 5000M there); the USB 2
links came from cables and an L adapter that only carry USB 2.

Original notes:
Handoff §2.1 wants every camera at 5000M. On camera day the opposite
showed up, with 4 cameras on the bench (one camera's USB-C socket was
broken by the Jetson's fan, see the enclosure repo; a backup is ordered):
* Cameras that linked at **USB 2** (480M, by accident: plugs not fully
  seated) captured 4K MJPEG cleanly: 589 frames, 0 errors (uvcvideo
  stats). Two of them share one USB 2 path through the RSH-H7C at
  ~12.6 fps each at 4K15, 30 fps each at 4K30 in the live pipeline.
* Cameras at **USB 3** (5000M, isochronous alt setting 7) lose frames:
  13 errors in 92 frames, uneven rates (7–25 fps when 15 is asked), and
  the kernel logs `tegra-xusb: WARN Event TRB for slot N ep 2 with no TDs
  queued?`. With **two** USB 3 cameras streaming, the second gets almost
  nothing (2 frames in 15 s); the live pipeline restarts it in a loop.
* Not the new software: plain `v4l2-ctl --stream-mmap` shows the same.
**To try when all five are cabled:** reseat so every link is what we
expect; `usbcore.autosuspend=-1`; uvcvideo `quirks`/`nodrop`; capping the
isochronous alternate setting; the cameras' other formats. **Fallback:**
USB 2 is enough for 4K15 MJPEG per camera if each has its own USB 2 path,
which needs a different wiring plan than D-011. Owner input needed only
if the fallback is the answer.

**Later the same day:**
* **Jetson USB-A ports 1 and 2 never link at USB 3** (their SuperSpeed
  side stays "not attached" with anything plugged in), which is why the
  RSH-H7C ran at USB 2 all morning. Ports 3 and 4 do; the hub in port 3
  links at 10000M. Port numbers are the kernel's (`2-1-portN`); which
  physical sockets they are is still to map.
* Disabling a camera's SuperSpeed port in sysfs doesn't make it fall back
  to USB 2; it just disconnects (re-enabled, it comes back).
* **Working layout, 4 cameras:** all on the hub, hub in Jetson port 3.
  North and south link at 5000M through the hub, up and east at 480M
  (their cable/plug). All four stream together into the dome at a steady
  15 fps output (USB 2 cameras 15 fps, USB 3 ones 12.5–14.5). Two USB 3
  cameras behind the hub coexist; on the Jetson's own ports 3 and 4 they
  starved each other.
* **Damaged frames (the owner saw fast streaks):** of 150 frames per
  camera, the USB 3 ones had 17 and 14 damaged JPEGs (cut short, or
  holes: missing restart markers); the USB 2 ones none. Fixed in the
  capture path: every MJPEG frame is checked before the hardware decoder
  (start/end markers and the 269 restart markers these cameras write,
  in order: a byte scan, no decoding) and dropped if damaged; the dome
  keeps that camera's last good frame. `skydome-jpeg-check` agreed with a
  full libjpeg decode on all 600 recorded frames. Drops show in the live
  stats as "damaged dropped".

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
**Superseded by D-019 (30°).**

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
**Superseded (2026-10-31/11-01):** no USB hub. After F-010 the 4 side
cameras go in the Jetson's USB-A ports and the zenith camera in its USB-C
port; the enclosure's tray no longer has room for the hub.

### D-012 · Mast: a photography light stand (owner, 2026-10-29)
Resolves F-002. The standard light-stand top is a 5/8″ (16 mm) spigot
with a 1/4″-20 male thread on its tip. The tray already fits it: a
16.5 mm cup, 15 mm deep, takes the spigot, and the 1/4″-20 × 8 mm heat-set
insert above the cup takes the threaded tip, so the enclosure screws onto
the stand. Check that the stand's threaded tip is at most ~9 mm long (the
insert hole is 9.5 mm deep). Outdoors, weigh the stand's legs down
(sandbag) against wind. Handoff §2's pipe-mast text stays as written; this
entry supersedes it.
**Dropped (owner, 2026-11-01):** the tray has no centre pole mount any
more (that let it shrink from 52 to 27 mm); the rig stands on its magnet
feet.

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

### D-015 · Hands-off Auto mode and an NVR-friendly stream (owner, 2026-10-29)
Extends handoff §5.4 ("Auto mode scheduling by sun altitude") and §6–7.
The rig must run for days with nobody touching it, and still be fully
controllable from the web app when wanted.
* **Auto is the default at every boot.** The mode (Day / Dusk-Night /
  Deep) follows **both** the Sun's altitude (from the D-013 location and
  UTC) **and the measured sky brightness** (median luminance and the
  current exposure/gain of the cameras), so moonlit nights, overcast
  days and storms get the right mode. Hysteresis plus a minimum dwell
  (~5 min) so it can't flap at a threshold.
* **Smooth transitions:** through twilight, exposure and gain ramp a
  little every frame instead of jumping, and all five cameras follow one
  shared exposure target so the seams stay invisible.
* **NVR stream never changes shape:** the RTSP/ONVIF output keeps one
  resolution and frame rate around the clock (Detail, 4096² × 15, D-014).
  When night exposures are longer than 1/15 s, the latest (stacked)
  frame is repeated, so the stream never stalls or reconnects. Mode
  changes alter the picture, not the stream format. The Smooth toggle is
  manual and reverts to Detail after a timeout (default 30 min) unless
  pinned, so an unattended rig always returns to the NVR's format.
* **The NVR is the owner's own software**, so it takes the full 4096²
  H.264 stream as is. Its decoder must accept frames beyond H.264 level
  5.2 (4096 × 2304): FFmpeg/libavcodec software decode does; many
  hardware decoders don't. Snapshot (ONVIF `GetSnapshotUri`) is the
  full-res hardware JPEG, and mode/exposure/alignment are available to it
  from the REST API.
* **Self-healing:** every service is a systemd unit with automatic
  restart; the Jetson's hardware watchdog reboots a hung system; a lost
  camera keeps the dome running on four and re-attaches (§5.1); the disk
  watchdog prunes (§5.6); cold boot goes straight to streaming with no
  keyboard or monitor (§3). Recording continues locally if the network
  drops.
* **Health at a glance:** the web app's status bar and a `/health` API
  show mode, fps, dropped frames, temperatures, disk, alignment age and
  any dew/fog flag (F-009), so a 48 h run can be checked from a phone.
* **Manual control is always an override:** any mode or exposure set by
  hand holds until released or until the next Auto transition, whichever
  the user picks.

### D-016 · Live dome pipeline: zero-copy GPU stitch (2026-10-31)
Phase 1's hot path, `pipeline/native` (C++/CUDA, handoff §4), started by
`tools/run_live.py`:
* **Capture:** per camera `v4l2src → nvv4l2decoder mjpeg=1` (hardware
  JPEG). The decoder's output is pitch-linear planar **Y42B** (4:2:2) in
  NVMM. `NvBufSurfaceMapCudaBuffer` is "not supported on this platform"
  on the Orin Nano, so frames reach CUDA through
  `NvBufSurfaceMapEglImage` + `cuGraphicsEGLRegisterImage`: no copy,
  verified pixel-identical to a CPU read. The newest frame per camera is
  held; nothing queues.
* **Stitch:** one CUDA kernel per plane remaps and feather-blends all
  cameras straight from those buffers into an I420 dome, in YUV (no RGB
  round trip). Tables per D-005 on an 8 px grid (513² nodes, 10.8 MB for
  4096²; interpolation error < 0.01 px), built by Python
  (`gpu_lut.py`, `tools/build_stitch_lut.py`) and consumed by the kernel;
  the feather weight is computed per pixel from the interpolated
  coordinates. A per-cell mask means most pixels touch 1–2 cameras.
  `gpu_lut.grid_stitch` is the kernel's NumPy twin; tests check
  twin = full-resolution reference and kernel = twin (±1 level).
* **Missing cameras** are left out per frame and the rest renormalised,
  so the dome keeps running on four (handoff §5.1). A camera that errors
  or stops sending is torn down and retried every 3 s.
* **One table for every capture size:** coordinates scale to the frame's
  real size, so 1080p binned capture (same field of view, confirmed on
  camera day) uses the 4K tables.
* **Output:** the GPU writes into pinned host memory that x264 reads
  directly (no copy) → `x264enc` ultrafast/zerolatency → RTSP into
  mediamtx (`config/mediamtx.yml`): `/dome` (Detail) and `/dome-smooth`.
  Full-range BT.601 is kept from the JPEG decode and flagged in the
  stream. `jetson_clocks` runs while it's up.

Measured 2026-10-31 on the Jetson with 3 live cameras (the 4th is
F-010's starved USB 3 camera), `jetson_clocks` on:

| mode | output | stitch | CPU | GPU |
|---|---|---|---|---|
| Detail 4096² | 15.0 fps steady | 9.3 ms | 52% | 15% |
| Smooth 2048² | 30.0 fps steady | 2.7 ms | — | — |

Kernel alone with five 4K inputs (random noise, worst case): 12.4 ms at
4096², 3.2 ms at 2048². **Chrome on macOS plays the 4096² stream over
WebRTC** (mediamtx's page) at 15.2 fps with no dropped frames, so D-014's
"two halves" fallback isn't needed there; Safari and phones still to try.
Not yet: per-camera exposure matching (seams show where auto exposure
differs), manual orientation from the UI, the web app, the ring buffer.

### D-017 · Camera pointing from the overlaps (2026-10-31)
`tools/align_from_overlap.py` (`overlap_align.py`) measures how the
cameras really point: SIFT matches between every pair of stills,
per-pair RANSAC rotation, then all rotations refined together (robust
Gauss-Newton). It writes `config/calibration/extrinsics.yaml`, the file
handoff §5.3 names for alignment, which the stitch now prefers over the
nominal pointing. `--frame nominal` turns the solved set to best fit the
mount design (the rig); `--frame centered` puts loose bench cameras'
mean view at the zenith. Until a lens has a board calibration, it also
fits one shared field of view to the overlaps (`lens_hfov_deg`, used by
the stitch). It's the daylight complement to plate solving: it fixes the
cameras relative to each other, the stars fix north and level.
On the bench (4 cameras, a garage 1–4 m away) it lands within ~0.5° RMS;
the rest is parallax between camera positions, which vanishes on the sky.
Also from camera day: the dome now addresses cameras by
`/dev/v4l/by-path/…` (USB port), because replugging the hub swapped
`/dev/videoN` numbers.

### D-018 · Dome edge 10° below the horizon (owner, 2026-10-31)
Handoff §5.4 puts the horizon at the edge of the dome image. With the
side cameras at ~33° elevation, their lower corners reach 6–10° below the
horizon (about 3% of each side image), which that cut off. The owner wants
to see every pixel, so the edge is now a setting, `dome.edge_zenith_deg`
in `config/modes.yaml`, set to **100**: the horizon is a circle at 90% of
the radius, and the sky gets ~10% fewer pixels per degree (20.5 instead
of 22.8 px/° at 4096²). Set it back to 90 for the handoff's layout. It's
stored in the stitch table header, so overlays and plate solving read the
same value.

### D-019 · Side cameras at 30°, taller camera head (owner, 2026-10-31)
Supersedes D-009's 35°. The bench fit of the lens (D-017, F-005 hint)
says ~110° wide, not 120°, and at 35° a 110° lens leaves the bottom ~4°
of sky uncovered all the way round (96.4% of the sky). At 30° the sky is
covered down to the horizon (−0.8°) with no gaps, for any equidistant
lens from 108° up; below ~106° a gap opens at ~60° altitude between the
zenith and side cameras, so the board calibration should confirm ≥ 108°.
Overlap seen by 2+ cameras goes from 46% to 41%.
* `../skycam_mount.scad`: `tilt = 60`, window walls lean 15°, and a new
  26 mm straight `riser_h` wall above the skirt lifts every camera and its
  USB-C plug ~28–30 mm clear of the Jetson's fan with the original 52 mm
  tray (the 78 mm tray from earlier the same day is dropped). The west
  camera now installs socket edge uphill (north-side grazed its window at
  60°). All eight fit checks pass. Hub 183.6 × 183.6 × 83.3 mm.
* `config/cameras.yaml`: side `alt_deg: 30`; tests and coverage defaults
  follow.

### D-020 · ONVIF: a small Python service, imaging settings for all cameras (owner asked, 2026-11-01)
Handoff §5.7 (ONVIF Profile S), brought forward from Phase 2 at the
owner's request, "with controls via ONVIF where any apply".
* **Custom, not rpos.** The handoff says to evaluate `rpos` first. It is a
  Node.js server written around a single Raspberry Pi camera: it would add
  Node to the Jetson, and the parts this rig needs (one imaging setting
  driving five cameras, a snapshot from the GPU dome) would still have to
  be written. The handoff's alternative, a minimal custom SOAP service,
  is `server/skydome_server/onvif/` (stdlib + PyYAML, run with the
  pipeline environment). Since D-021 it is served by the FastAPI server
  (`tools/run_server.py`, unit `systemd/skydome-server.service`).
* **What it offers:** WS-Discovery (Probe/Resolve/Hello/Bye), Device
  (information, capabilities, services, time, scopes, network), Media
  ver10 with one fixed profile `dome` = the Detail stream
  (`rtsp://<jetson>:8554/dome`, 4096² H.264 at 15 fps, D-014/D-015) and
  `GetSnapshotUri`, and Imaging ver20. Port 80 (systemd grants
  CAP_NET_BIND_SERVICE), so NVRs that assume 80 work.
* **Snapshot:** full-resolution hardware JPEG (`nvjpegenc`) of the dome,
  made by skydome-live on request (`--snapshot PATH`: the server creates
  `PATH.request`, the next output frame is encoded to `PATH`). ~0.1 s,
  no effect on the 15 fps output.
* **Imaging = all five cameras at once** (D-015's shared exposure target):
  exposure AUTO/MANUAL (MANUAL: time 100 µs–1 s, gain 0–190, the cameras'
  own units and ranges), brightness, contrast, saturation, sharpness,
  backlight compensation, white balance AUTO/MANUAL. Kept in
  `var/camera_settings.yaml` and re-applied when a camera re-enumerates
  (a replugged UVC camera forgets its controls). Not offered because the
  cameras can't: focus (fixed M12), IR-cut filter, WDR, white-balance
  gains (the cameras take a colour temperature instead; set
  `white_balance_temperature` in the file), anti-flicker (ONVIF has no
  field; set `power_line_frequency: 2` in the file for 60 Hz lights, the
  cameras default to 50 Hz, Phase 0 findings). When D-015's Auto mode exists, ONVIF exposure AUTO
  should mean "hand back to Auto" and MANUAL a manual override.
* **Not offered:** PTZ, audio, events, I/O, profile editing.
  `SetVideoEncoderConfiguration` succeeds only if the resolution, codec
  and frame rate are unchanged (the stream never changes shape, D-015);
  bitrate/GOP/quality an NVR pushes along are ignored. Time is chrony's
  (`SetSystemDateAndTime` accepts NTP only).
* **Login:** WS-Security UsernameToken (digest or text), single user in
  `var/onvif_credentials.yaml` (mode 600), created as `admin` with a random
  password on first start; `tools/run_server.py --set-password` changes it.
  Nonces are single-use and the token's time must be within an hour.
  The RTSP stream and the snapshot URL stay open on the LAN, as before
  (handoff §5.7).
* **Tested 2026-11-01:** the Mac's WS-Discovery probe lists SkyDome
  next to the house's other cameras; onvif-zeep (the library Frigate uses)
  reads the device, profile, stream and snapshot URIs, downloads the
  snapshot, and its imaging changes reach every camera (`v4l2-ctl`
  confirms); wrong passwords and out-of-range values are refused.
  `server/tests`: 25 tests (24 on the Mac).

### D-021 · Web page v1: one FastAPI server for the page, its API and ONVIF (owner asked, 2026-11-03)
The first slice of handoff §5.8/§5.9, built to its stack: FastAPI on the
Jetson, React + TypeScript + Vite + Tailwind for the page, WebRTC (WHEP).
* **One process on port 80** (`tools/run_server.py`,
  `systemd/skydome-server.service`, replacing `skydome-onvif.service`):
  the page from `web/dist`, the API under `/api` (OpenAPI at
  `/api/docs`), ONVIF under `/onvif` (D-020's SOAP code, now behind
  `OnvifEndpoint`). Port 80 can only have one owner, and NVRs assume it.
* **What the page does:** the live dome, played from mediamtx over WebRTC
  with wheel/pinch zoom, drag to pan, snapshot download and full screen;
  the cameras' settings (one set for all five, the same store ONVIF writes,
  so either one sees the other's changes); the Detail/Smooth switch; health
  (output and per-camera fps, restarts, dropped frames, stitch time,
  viewers, temperatures, CPU, memory, disk, Wi-Fi, uptime); a red
  night-vision theme. Not yet: the sphere view, overlays, the diagnostics
  history, auto-align, recording — later slices of §5.8.
* **WebRTC signalling goes through the server** (`/api/webrtc/...` passed
  on to mediamtx's WHEP): the page needs only its own address, no
  cross-port requests. The video itself still flows straight from mediamtx.
* **Detail/Smooth:** the page writes `var/live_mode.yaml`; `run_live.py`
  (without `--mode`) follows it and restarts skydome-live in the new mode
  (a few seconds of no picture; the page reconnects by itself). Smooth
  reverts to Detail after 30 minutes (D-015).
* **No login** for the page or the API on the LAN (handoff §5.7: basic
  auth optional, off by default). ONVIF keeps its own login.
* **The built page is committed** (`web/dist`, ~180 kB): the Jetson
  deploys with `git pull` and never needs Node. Rebuild on the Mac with
  `npm run build` after changing `web/src`.
* **Tested 2026-11-03** on the rig outdoors: the page plays the 4096² dome,
  Detail → Smooth → Detail switches and reconnects, health and settings
  read live values; phone layout and the night theme checked.
  `server/tests`: 34 tests (33 on the Mac).

### D-022 · Cameras matched to each other in the stitch (owner asked, 2026-11-03)
The owner: the cameras' pictures are all different; make them blend. Each
camera runs its own auto exposure and white balance, so the one facing the
sun darkens itself and the zenith camera came out a lighter, bluer square.
* **PhotoBalance** (`pipeline/native/src/balance.cu`), in skydome-live and
  `skydome-stitch-files --balance`: at start-up it lists every stitch-grid
  node in the sky (≤ 88° from the zenith) that two cameras both see, away
  from the outer 1% of each picture (~107k points on the rig). Once a
  second one GPU pass compares the two cameras at those points, skipping
  dark (Y ≤ 10) and clipped (Y ≥ 240) ones, and sums per pair the
  brightness ratio (in logs) and the colour (U, V). A small weighted least
  squares gives one gain and one colour offset per camera that make the
  overlaps agree, as small as possible (they average out to no change, so
  the dome's brightness is still the cameras' own). Each update moves a
  quarter of the way (no flicker; settles in a few seconds); gains are
  held to 0.5–2, offsets to ±40. Too little usable overlap (night, fog)
  keeps the last values. The stitch applies Y × gain and
  U, V = 128 + gain × (c − 128) + offset; ~1 ms a second, the stitch itself
  is unchanged at ~11 ms. `--no-balance` turns it off. The NumPy twin
  (`gpu_lut.grid_stitch`) has no balance; the kernel matches it with none.
* **Shown** on the web page's Health tab ("Match" per camera) and in the
  live stats (`balance` per camera, `balance_points`).
* **Not done yet:** lens vignetting (each picture darkens toward its
  edges, which still leaves a faint cross where four corners meet) and a
  shared exposure target that drives the cameras themselves (D-015;
  matters at night, where a digital gain only amplifies noise).
* **Tested 2026-11-03** outdoors at sunset: stills stitched with and
  without (zenith ×0.72, south ×1.31: the patches and the zenith square
  gone); live, 15 fps, corrections settled to zenith ×0.82, south ×1.23.
  `test_native_stitch.py`: a camera made 40% darker and bluer is brought
  back (gain within 5% of 1/0.6, colour within 1).

### D-023 · More detail: 5120² live, full-detail zoom, stills and raw (owner asked, 2026-11-03)
Revises D-014's Detail size. The cameras see ~33 px/° (3840 px over a
116° lens); the 4096² dome (200° across, D-018) held ~20.5 px/°, so 40% of
their sharpness was thrown away. The Orin Nano encodes on the CPU, so the
live dome can't simply match them (~6600², 44 MP).
* **Live Detail is 5120² × 15 (~26 px/°).** Measured on the rig with
  jetson_clocks: 4096² 2.3 cores, 5120² 3.4, 5632² 4.0, 6144² 4.0 (x264
  stops scaling at 4 threads; 6144² is also past H.264's largest frame).
  5120² leaves room for the zoom stream. Bitrate 28 Mb/s.
* **jetson_clocks really runs now:** `--store` refuses to overwrite its
  file and failed every start since Oct 31 (reported as "no password").
  An existing file is kept (it holds the normal clocks). JetPack 7.2.1's
  `--restore` stops part-way (a missing GPU file), so the clocks can stay
  high after the dome stops: harmless.
* **Zoom at full sharpness:** zoomed in ≥1.4× on the page, it sends the
  rectangle in view (`PUT /api/zoom`, ~1/s); skydome-live renders just
  that part straight from the cameras (the stitch kernel takes any
  rectangle at any scale) to a 1600 × 1200 stream on `/dome-zoom`, and
  the page fades it in once the view holds still. The zoom encoder starts
  on the first request and stops 15 s after the last.
* **Full-detail still** (`/api/full.jpg`, "Full detail" on the page):
  6656² (~33 px/°), from a second stitch table, hardware JPEG; a few
  seconds per still.
* **Raw** (`/api/raw.zip`, "Raw" on the page): each camera's next frame
  exactly as it sent it (MJPEG 3840 × 2160, saved before decoding), zipped
  with cameras.yaml and the alignment. Every pixel there is.

### D-024 · Ground mask (owner asked, 2026-11-03)
Hide houses, trees and fences: a horizon altitude for each degree of
azimuth; below it the stitch outputs black (the live dome, the NVR stream,
snapshots, stills and zoom alike). `var/horizon.txt` is the mask in use,
`var/horizon.off.txt` the same mask switched off; skydome-live rereads it
within a second. `tools/detect_horizon.py` finds a first line from a
daylight dome: walking down each azimuth, ground is much darker than the
open sky above it, or (when darker than 0.8 of it) not blue and busy —
foliage measured blueness −0.1…−0.7 and texture 21–27 against sky +0.2…+0.4
and 2–5; a sunset glow and sunlit clouds stay sky. Sunlit treetops can
still get through, so the page has an editor: drag on the dome to move the
line, save, switch it on and off.

### D-025 · Night: long exposures and a star stretch (owner asked, 2026-11-03)
Measured on the rig the first dark evening (light-polluted, some cloud):
* **Exposure is capped by the frame rate** unless the camera may slow
  down: `exposure_dynamic_framerate` = 1 lets a 1 s exposure (the most
  the cameras take) run at 1 fps; without it the 15 fps capture stops each
  frame at ~66 ms. It's now one of the picture controls (page, file).
  skydome-live keeps showing each camera's latest frame, so the dome still
  streams at 15 fps and the sky updates once a second.
* **Stars are there but faint:** at 1 s and full gain (190) they sit
  ~15-25 levels above a ~36-38 background (the cameras' black level at
  brightness 132). The cameras' contrast/gamma/brightness moved them by
  2-3 levels at most, and contrast above 65 crushed the sky to 0.
  Uncompressed 1080p (YUYV) was no better than 4K MJPEG, so 4K stays.
* **Night stretch in the stitch:** Y → 255·asinh(k·x)/asinh(k), x = (Y −
  black)/(255 − black), after blending (`var/stretch.txt` "black k",
  reread within a second; `/api/stretch`; sliders on the page). Tuned
  live: black 34, k 30 gave the most clear stars before noise took over.
  With a black point, camera matching (D-022) compares and scales the
  light above it, since the black level dominates at night.
* **Presets** (`/api/preset/night|day`, buttons on the page): Night =
  manual 1 s, slow-down on, gain 190, backlight 0, saturation 60, stretch
  34/30; Day = the cameras' auto exposure, defaults, no stretch.
* **Watch:** an NVR that pushes ONVIF imaging settings when it connects
  (this one set auto exposure and backlight 160 at 18:49) undoes Night.
* **Not yet:** stacking frames (handoff's Night/Deep modes) and switching
  presets by sun altitude (D-015 Auto).

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

**Cameras (2026-10-31, 4 of 5; `phase0.sh --gst`, report on the Jetson):**
* USB ID 32e4:6678 "4K U3 Camera", UVC 1.00. **All five report the same
  serial number (2601230006)**, so they can only be told apart by USB
  port (`ID_PATH`), as planned (handoff §2.1 rule 3).
* Formats: MJPEG 3840×2160 at 60/30/25/23/20/15/10 fps and smaller sizes
  (2880×2160, 2592×1944, 2560×1440, 2048×1536, …, 1920×1080 up to 60);
  YUY2 3840×2160 only at ≤ 23 fps (never used, §9), YUY2 1920×1080 offered.
* **1080p is binned, not cropped:** the 1920×1080 and 3840×2160 frames
  show the same field of view (`fov-video0-*.jpg`).
* Controls: `exposure_time_absolute` 1–10000 (×100 µs, so **up to 1 s**),
  `gain` 0–190 (default 140), `auto_exposure` manual (1) or aperture
  priority (3), white balance auto or 2800–6500 K, `power_line_frequency`
  defaults to **50 Hz** (set it to 60 Hz or off), plus focus/zoom/pan/tilt
  controls the fixed M12 lens ignores.
* Hardware MJPEG decode of 4K costs no measurable GPU and little CPU.
* USB: see F-010. The cameras link at 5000M when seated, but capture is
  only clean at 480M so far. Indoors in a dim room, auto exposure also
  lowers the frame rate (aperture-priority mode stretches the shutter).
* Load test, 4 cameras: 4K30 MJPEG 30 fps on three, the fourth starved
  (F-010); YUY2 1080p15 not negotiated on the USB 2 links (expected:
  uncompressed 1080p doesn't fit USB 2).
* Still to do with all five on final cabling: the full 5× load tests,
  USB tree at 5000M for every camera, and the F-010 experiments.
