"""Health of the Jetson itself, for the status panel: temperatures, CPU,
memory, disk, Wi-Fi, uptime. All read from /proc and /sys (no tegrastats
process to babysit)."""

from __future__ import annotations

import shutil
import threading
from pathlib import Path


class System:
    def __init__(self, data_dir: Path, proc: Path = Path("/proc"), sys_root: Path = Path("/sys")):
        self.data_dir = Path(data_dir)
        self.proc = proc
        self.sys = sys_root
        self._lock = threading.Lock()
        self._cpu_last: tuple[int, int] | None = None

    def snapshot(self) -> dict:
        return {"temps_c": self.temps(), "cpu_pct": self.cpu_pct(), "memory": self.memory(),
                "disk": self.disk(), "wifi_dbm": self.wifi_dbm(), "uptime_s": self.uptime()}

    def temps(self) -> dict[str, float]:
        """Thermal zones by name (cpu, gpu, soc0..2, tj), °C."""
        out = {}
        for zone in sorted((self.sys / "class" / "thermal").glob("thermal_zone*")):
            try:
                name = (zone / "type").read_text().strip().removesuffix("-thermal")
                milli = int((zone / "temp").read_text())
            except (OSError, ValueError):
                continue
            if milli > -40000:   # unused zones read -40 °C or lower
                out[name] = round(milli / 1000, 1)
        return out

    def cpu_pct(self) -> float | None:
        """Busy share of all cores since the last call (first call: since boot)."""
        try:
            fields = [int(x) for x in (self.proc / "stat").read_text().splitlines()[0].split()[1:]]
        except (OSError, ValueError, IndexError):
            return None
        idle = fields[3] + (fields[4] if len(fields) > 4 else 0)
        total = sum(fields)
        with self._lock:
            last, self._cpu_last = self._cpu_last, (idle, total)
        d_idle, d_total = (idle - last[0], total - last[1]) if last else (idle, total)
        return round(100 * (1 - d_idle / d_total), 1) if d_total > 0 else None

    def memory(self) -> dict | None:
        try:
            info = dict(line.split(":", 1) for line in (self.proc / "meminfo").read_text().splitlines())
            total = int(info["MemTotal"].split()[0]) * 1024
            avail = int(info["MemAvailable"].split()[0]) * 1024
        except (OSError, KeyError, ValueError):
            return None
        return {"used": total - avail, "total": total}

    def disk(self) -> dict | None:
        try:
            u = shutil.disk_usage(self.data_dir)
        except OSError:
            return None
        return {"free": u.free, "total": u.total}

    def wifi_dbm(self) -> int | None:
        """Signal level of the first Wi-Fi interface, or None on Ethernet."""
        try:
            lines = (self.proc / "net" / "wireless").read_text().splitlines()[2:]
            return int(float(lines[0].split()[3])) if lines else None
        except (OSError, ValueError, IndexError):
            return None

    def uptime(self) -> float | None:
        try:
            return float((self.proc / "uptime").read_text().split()[0])
        except (OSError, ValueError, IndexError):
            return None
