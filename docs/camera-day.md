# Camera day: from five cameras in a box to a calibrated rig

Everything here runs on the Jetson (`ssh skydome@skydome.local`) from
`~/skydome`, in this order. Steps 1–6 need nothing but the cameras; step 7
needs a clear night. Already done before the cameras arrived: JetPack 7.2.1,
MAXN SUPER, updates, `chrony` time sync, `uv`, `mediamtx` v1.21.2, this repo
with its tests passing, and a dry run of every bring-up tool.

**Have ready:** the five cameras, 3 right-angle USB-C→A and 2 right-angle
USB-C→C cables (USB 3, plug heads ≤ 15 mm), the printed test plate, and
`docs/charuco_board.pdf` printed at 100% and glued flat to card.

## 0 · Update the code (10 s)

```bash
cd ~/skydome && git pull
```

## 1 · Test-fit one camera before printing the hub (15 min)

Screw a camera to the printed test plate with its USB-C cable plugged in.
It should sit flat with the plug clear (the socket position in the model
came from a photo). If it doesn't, stop and send a photo; the hub needs a
tweak before printing.

## 2 · Plug in (D-011) and check focus (15 min)

| Camera | Plug into |
|---|---|
| zenith, north, south | Jetson USB-A ports |
| east, west | RSH-H7C hub ports 4–6 (switch only those on) |

Leave the Jetson's own USB-C port free. Then, on a bench or a table, point
each camera out a window at something far away and check focus in step 3's
snapshots. The M12 lenses should be focused at infinity; if one is soft,
turn its lens a few degrees at a time and re-snapshot.

## 3 · Phase 0 bring-up report (10 min)

```bash
tools/bringup/phase0.sh --gst
```

Writes `reports/phase0-<time>/summary.md`. It checks:

* 5 cameras found; each camera at **5000M** in the USB tree, the hub at 10000M
* each camera's real exposure and gain ranges (night mode depends on them)
* 1080p vs 4K field of view (binned or cropped)
* load tests: 5× 4K30 MJPEG and 5× 4K15 MJPEG through the hardware decoder,
  and 5× 1080p15 YUY2, with fps, drops and GPU load

Send the summary back; it goes into DECISIONS.md "Phase 0 findings".

## 4 · Name each camera by its port (10 min)

```bash
python3 tools/identify_cameras.py
```

Open http://skydome.local:8090: one snapshot per camera with its `ID_PATH`.
Cover one camera with your hand, rerun, and see which picture goes dark.
Then:

```bash
python3 tools/identify_cameras.py --assign zenith=<ID_PATH> north=<ID_PATH> east=<ID_PATH> south=<ID_PATH> west=<ID_PATH>
sudo python3 tools/gen_udev_rules.py --install
ls -l /dev/skycam-*
```

From now on, don't move cables between ports.

## 5 · Calibrate one lens (30 min, settles F-005)

All five cameras use the same lens, so one careful calibration answers the
fisheye-or-rectilinear question and the true field of view; the others can
be done later the same way.

```bash
uv run --project pipeline tools/calibrate_intrinsics.py capture --camera zenith --count 40
```

Hold the board about **1 m** from the lens. Between beeps (every 4 s) move it
so that over the 40 shots it visits the centre, all four edges and all four
corners of the picture, tilted different ways. Then measure one printed
square with a ruler and:

```bash
uv run --project pipeline tools/calibrate_intrinsics.py solve --camera zenith --square-mm 25.0
```

Good result: RMS under ~0.5 px and coverage ≥ 70%. It writes
`config/calibration/zenith.yaml` and prints the true horizontal, vertical
and diagonal field of view.

## 6 · Assemble and mount (30 min)

Install each camera in the printed hub turned as the model's header says
(side cameras landscape; socket edges: zenith toward north, north uphill,
west north-side, south east-side, east any). Snap the hub onto the tray and
screw the whole thing onto the light stand.

## 7 · First clear night: stars (Phase 2 prep)

Not ready yet: the live dome (Phase 1) and plate solving / auto-align
(Phase 2) are the next software to build. Steps 3–5 give them what they
need: camera names, exposure limits and the lens model.

## Then: the build order

1. **Phase 1**, the live dome: capture → CUDA stitch → H.264 4096² @15
   (D-014) → mediamtx → WebRTC page, with manual orientation from the mount
   design.
2. **Phase 2**: plate solving, auto-align from the stars, overlays,
   Deep-mode stacking, timelapse, ONVIF for the NVR.
3. **Phase 3**: meteor/transient detection, nightly report.
