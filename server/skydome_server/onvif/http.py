"""The ONVIF endpoint: SOAP services under /onvif/*, the login NVRs use,
and the dome's snapshot (a hardware JPEG from skydome-live). Served by the
web server in skydome_server/app.py."""

from __future__ import annotations

import os
import secrets
import sys
import threading
import time
from pathlib import Path

import yaml

from .services import Request, Services
from .soap import MAX_REQUEST, Fault, UsernameTokenAuth, envelope, fault_envelope, parse


def load_credentials(path: Path) -> dict[str, str]:
    """{user: password}. The first run creates 'admin' with a random password
    (the file is readable by its owner only); change it with --set-password."""
    if not path.exists():
        save_credentials(path, "admin", secrets.token_urlsafe(12))
        print(f"onvif: created {path} (user admin, random password)", file=sys.stderr)
    data = yaml.safe_load(path.read_text()) or {}
    return {str(data["username"]): str(data["password"])}


def save_credentials(path: Path, user: str, password: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write("# ONVIF login for NVRs (tools/run_server.py --set-password)\n"
                + yaml.safe_dump({"username": user, "password": password}))
    tmp.replace(path)


class SnapshotSource:
    """Asks skydome-live for a still (PATH.request -> PATH) and waits for it.
    A still under `fresh` seconds old is reused, so several clients polling at
    once cost one JPEG."""

    def __init__(self, path: Path, timeout: float = 3.0, fresh: float = 1.0):
        self.path = Path(path)
        self.request = self.path.with_name(self.path.name + ".request")
        self.timeout = timeout
        self.fresh = fresh
        self._lock = threading.Lock()

    def get(self) -> bytes | None:
        with self._lock:
            if self._age() < self.fresh:
                return self._read()
            asked = time.time()
            try:
                self.request.touch()
            except OSError:
                return None
            while time.time() - asked < self.timeout:
                time.sleep(0.02)
                try:
                    if self.path.stat().st_mtime >= asked:
                        return self._read()
                except FileNotFoundError:
                    pass
            self.request.unlink(missing_ok=True)   # skydome-live isn't running
            return None

    def _age(self) -> float:
        try:
            return time.time() - self.path.stat().st_mtime
        except OSError:
            return float("inf")

    def _read(self) -> bytes | None:
        try:
            return self.path.read_bytes()
        except OSError:
            return None


class OnvifEndpoint:
    """SOAP in, SOAP out, for every service under /onvif/ (the web server in
    skydome_server/app.py does the HTTP). Operations the dome doesn't support
    are logged once each; NVRs poll constantly, so nothing else is."""

    def __init__(self, services: Services, auth: UsernameTokenAuth, log=print):
        self.services = services
        self.auth = auth
        self.log = log
        self.unsupported: set[str] = set()

    def post(self, data: bytes, host: str, port: int) -> tuple[int, bytes]:
        """(HTTP status, SOAP envelope). host and port are the address the
        client reached us on; the URLs in the answers point there."""
        if len(data) > MAX_REQUEST:
            return 413, fault_envelope(Fault("Sender", ["ter:InvalidArgVal"], "request too large"))
        try:
            header, op = parse(data)
            user = self.auth.check(header) if self.services.needs_auth(op) else None
            body = self.services.handle(Request(op, host, port, user))
        except Fault as f:
            if f.subcodes[:1] != ["ter:ActionNotSupported"]:
                self.log(f"onvif: {f.subcodes[-1]}: {f.reason}", file=sys.stderr)
            elif f.reason not in self.unsupported:
                self.unsupported.add(f.reason)
                self.log(f"onvif: {f.reason}", file=sys.stderr)
            return f.status, fault_envelope(f)
        except Exception as e:   # a bug in a handler: answer, log, keep serving
            self.log(f"onvif: internal error: {e!r}", file=sys.stderr)
            return 500, fault_envelope(Fault("Receiver", ["ter:Action"], "internal error"))
        return 200, envelope(body)
