# DECISIONS

Running log of decisions for the SkyDome stack (handoff §1, §9).

* **D-nnn** — decisions made within the handoff's latitude.
* **F-nnn** — **flagged**: departs from, or conflicts with, `handoff.md`, or
  needs the owner's call. Nothing marked F is applied silently.
* **Phase 0 findings** — filled in from `tools/bringup/phase0.sh` on day one.

---

## Flagged — needs the owner

### F-001 · USB topology: handoff vs. enclosure plan
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

### F-002 · Mount: handoff text is out of date
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

Requirement for either: Jetson UEFI firmware ≥ 36.0. To check, press Esc
at the NVIDIA logo. Older firmware needs NVIDIA's microSD bridge
(JetPack 5.1.3 image), which Etcher on a Mac can also write.
Sources: NVIDIA Orin Nano dev kit Quick Start and firmware-update pages,
JetPack 7.2.1 release notes. Either way, reinstalling
costs about an hour, so Phase 0 results can reverse the choice.

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

---

## Open questions (handoff §8)

* Exact site coordinates (or wait for GPS)? `config/site.yaml` has the
  handoff's approximate Surprise, AZ values.
* Will the rig ever run over cellular/Starlink?
* Clips: local only, or sync to a NAS?
* Mast: 1¼″ pipe or light stand (F-002)?

---

## Phase 0 findings

*Not run yet — the hardware hasn't arrived.* On day one:
`tools/bringup/phase0.sh --gst`, then paste `reports/phase0-*/summary.md`
here: USB tree speeds, V4L2 format lists, real exposure/gain ranges, and the
5× MJPEG 4K30 and 5× YUY2 1080p load-test results.
