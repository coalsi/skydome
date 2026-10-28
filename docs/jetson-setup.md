# Jetson setup: JetPack 7.2.1 from a Mac, straight onto the NVMe

Decision D-010. Everything here is done from a Mac. No microSD card or
Linux PC is needed. Allow about 1–1.5 hours, mostly waiting.

Sources: NVIDIA Orin Nano Developer Kit Quick Start Guide and JetPack
7.2.1 release notes (developer.nvidia.com/embedded/jetpack/downloads/archive-7.2.1).

## You need

* A USB flash drive, **16 GB or larger**. It will be erased.
* A **DisplayPort** monitor (the Jetson has no HDMI port), a USB keyboard
  and a mouse. They're only needed for this setup.
* An Ethernet cable to your router (recommended; Wi-Fi works too).
* The NVMe drive and a small Phillips screwdriver.
* Leave the USB hub and cameras **unplugged** until the end.

---

## A · On the Mac (about 20 min)

1. Install **balenaEtcher** for macOS from https://etcher.balena.io.
2. Download the JetPack 7.2.1 installer ISO (several GB), the file marked
   "Download JetPack ISO Image" on NVIDIA's release page:
   `https://developer.nvidia.com/downloads/embedded/l4t/r39_release_v2.1/iso/jetsoninstaller-r39.2.1-2026-08-07-18-30-47-arm64.iso`
   One file covers all Jetsons. It may ask you to log in with a (free)
   NVIDIA developer account.
3. Plug in the USB stick. Open Etcher, then:
   **Flash from file** → the `.iso`, **Select target** → the USB stick (check
   the size, so you don't pick another drive), **Flash**.
   Dragging the file onto the stick in Finder will **not** work.
4. When Etcher finishes, macOS may say the disk is unreadable. Click
   **Eject**, not Initialize.

## B · Hardware (about 10 min)

5. Jetson **unplugged from power**. Fit the NVMe drive into the M.2 slot on
   the **underside of the carrier board**. Use the slot that takes the full
   80 mm (2280) drive; the shorter slot is for 2230 drives. Insert it at an
   angle, press it flat and fit the retaining screw.
6. Connect the DisplayPort monitor, keyboard, mouse, Ethernet and the
   **USB stick**.

## C · Firmware check and install (about 30–45 min)

7. Plug in the Jetson's power supply (it powers on by itself). When the
   **NVIDIA logo** appears, tap **Esc** repeatedly until a blue/grey setup
   menu opens.
8. **Firmware check:** read the version near the top of that menu.
   * **36.x or higher** → continue.
   * **35.x or lower** → stop here; the kit needs NVIDIA's one-time
     firmware bridge first (uses a microSD card, also doable from the Mac).
9. Choose **Boot Manager**, then the **USB drive**.
10. A **firmware update** prompt appears with a 30-second countdown.
    **Press Y.** If it times out, the install fails later; power-cycle and
    start again at step 7.
11. The firmware updates in two passes and may restart. If you find
    yourself back at the NVIDIA logo, repeat steps 7 and 9 to boot the USB
    stick again.
12. At the boot menu, choose **Install Jetson ISO r39.2.1**.
13. **Target:** choose the **NVMe drive** (named like `nvme0n1`, with
    your drive's size, ~512 GB). **Not** the USB stick. It erases the
    target. Confirm.
14. Wait for it to finish. When it says to reboot, **remove the USB stick**
    and reboot.

## D · First boot (about 15 min)

15. Accept the NVIDIA EULA, then choose the language, keyboard and time
    zone (**America/Phoenix**).
16. Connect to the network. Create your user and password.
    **Computer name: `skydome`**, so it's reachable as `skydome.local`.
17. **Power mode:** click the power-mode entry in the top bar → **Power
    Mode** → **MAXN SUPER**.
18. Open a **Terminal** and install the rest of JetPack (CUDA, TensorRT,
    multimedia) plus a few tools. This takes about 10–20 min:

    ```bash
    sudo apt update
    sudo apt install -y nvidia-jetpack v4l-utils gstreamer1.0-tools openssh-server avahi-daemon
    sudo reboot
    ```

## E · Checks to send back

19. After the reboot, open Terminal and paste this whole block, then copy
    everything it prints back to Claude:

    ```bash
    echo "== release";      cat /etc/nv_tegra_release
    echo "== os";           lsb_release -ds; uname -r
    echo "== power mode";   sudo nvpmodel -q
    echo "== root disk";    findmnt -no SOURCE /; df -h /
    echo "== jetpack";      apt list --installed 2>/dev/null | grep -E '^nvidia-jetpack'
    echo "== cuda";         ls -d /usr/local/cuda* 2>/dev/null
    echo "== hw decoder";   gst-inspect-1.0 nvv4l2decoder 2>&1 | grep -E 'Long-name|mjpeg|No such' 
    echo "== hw encoder";   gst-inspect-1.0 nvv4l2h265enc 2>&1 | grep -E 'Long-name|No such'
    echo "== python";       python3 --version
    echo "== ip";           hostname -I
    ```

    What to look for: the root disk should be `/dev/nvme0n1p…`, and the
    power mode should say MAXN SUPER. Both GStreamer plugins should be
    found; they are the hardware video decoder and encoder the whole
    pipeline relies on.
20. From the **Mac**, check remote access: `ssh <your-user>@skydome.local`.

## F · USB hub (5 min)

21. Plug the RSH-H7C's power supply into the hub, and connect its upstream
    USB-C port to one of the Jetson's USB-A ports with a 10 Gbps cable.
    Then run:

    ```bash
    lsusb -t
    ```

    The hub should appear at **10000M** (USB 3.2 Gen 2). At 5000M, the
    cable or port is limiting it. Send the output back.

Next (Saturday, cameras): `tools/bringup/phase0.sh --gst`, then camera
identification and udev names (README "Day one on the Jetson").
