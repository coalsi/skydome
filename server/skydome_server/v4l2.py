"""V4L2 camera controls straight through ioctl (no v4l2-ctl subprocesses).

The SkyDome cameras are found by USB ID in sysfs, so this works before the
udev names exist and doesn't care which /dev/videoN a camera got this boot.
Controls are named the way v4l2-ctl names them (exposure_time_absolute, ...).
"""

from __future__ import annotations

import errno
import fcntl
import os
import re
import struct
from dataclasses import dataclass
from pathlib import Path

CAMERA_USB_ID = ("32e4", "6678")   # SVPRO U3CAM4K60, "4K U3 Camera"

_QUERYCTRL = 0xC0445624   # _IOWR('V', 36, struct v4l2_queryctrl)
_G_CTRL = 0xC008561B      # _IOWR('V', 27, struct v4l2_control)
_S_CTRL = 0xC008561C      # _IOWR('V', 28, struct v4l2_control)
_NEXT_CTRL = 0x80000000
_FLAG_DISABLED = 0x0001
_TYPE_CTRL_CLASS = 6
_QC = struct.Struct("<II32siiiiI2I")
_CTRL = struct.Struct("<Ii")


@dataclass(frozen=True)
class Control:
    id: int
    name: str
    minimum: int
    maximum: int
    step: int
    default: int


@dataclass(frozen=True)
class CameraNode:
    """One camera's capture node. `key` changes when the camera re-enumerates
    (unplug/replug, USB reset), which is when its controls fall back to the
    firmware defaults."""
    device: str     # /dev/videoN
    key: tuple


def control_name(raw: bytes) -> str:
    """'Exposure Time, Absolute' -> 'exposure_time_absolute', as v4l2-ctl does."""
    s = raw.split(b"\0", 1)[0].decode(errors="replace").lower()
    return re.sub(r"[^a-z0-9]+", "_", s).strip("_")


def find_cameras(sysfs: Path = Path("/sys/class/video4linux"), usb_id: tuple[str, str] = CAMERA_USB_ID,
                 dev: Path = Path("/dev")) -> list[CameraNode]:
    """Capture nodes (index 0) of every connected camera with this USB ID."""
    found = []
    for node in sorted(sysfs.glob("video*")):
        try:
            if (node / "index").read_text().strip() != "0":
                continue
            usb = (node / "device").resolve().parent   # the interface's parent is the USB device
            if ((usb / "idVendor").read_text().strip(), (usb / "idProduct").read_text().strip()) != usb_id:
                continue
            devnum = (usb / "devnum").read_text().strip()
        except OSError:
            continue   # unplugged mid-scan
        found.append(CameraNode(str(dev / node.name), (node.name, usb.name, devnum)))
    return found


class Device:
    def __init__(self, path: str):
        self.path = path
        self.fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)

    def close(self) -> None:
        os.close(self.fd)

    def __enter__(self) -> Device:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def controls(self) -> dict[str, Control]:
        out = {}
        cid = _NEXT_CTRL
        while True:
            buf = bytearray(_QC.pack(cid, 0, b"", 0, 0, 0, 0, 0, 0, 0))
            try:
                fcntl.ioctl(self.fd, _QUERYCTRL, buf)
            except OSError as e:
                if e.errno == errno.EINVAL:
                    return out   # past the last control
                raise
            qid, qtype, name, lo, hi, step, default, flags, _, _ = _QC.unpack(buf)
            if qtype != _TYPE_CTRL_CLASS and not flags & _FLAG_DISABLED:
                c = Control(qid, control_name(name), lo, hi, step, default)
                out[c.name] = c
            cid = qid | _NEXT_CTRL

    def get(self, cid: int) -> int:
        buf = bytearray(_CTRL.pack(cid, 0))
        fcntl.ioctl(self.fd, _G_CTRL, buf)
        return _CTRL.unpack(buf)[1]

    def set(self, cid: int, value: int) -> None:
        fcntl.ioctl(self.fd, _S_CTRL, bytearray(_CTRL.pack(cid, value)))
