"""The ONVIF HTTP endpoint: SOAP services under /onvif/*, and the dome's
snapshot at /onvif/snapshot.jpg (a hardware JPEG from skydome-live)."""

from __future__ import annotations

import os
import secrets
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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
        f.write("# ONVIF login for NVRs (tools/run_onvif.py --set-password)\n"
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


class OnvifServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, services: Services, auth: UsernameTokenAuth, snapshot: SnapshotSource):
        super().__init__(address, _Handler)
        self.services = services
        self.auth = auth
        self.snapshot = snapshot
        self.unsupported: set[str] = set()   # logged once each


class _Handler(BaseHTTPRequestHandler):
    server: OnvifServer
    protocol_version = "HTTP/1.1"
    server_version = "SkyDome-ONVIF"

    def do_POST(self) -> None:
        if not self.path.startswith("/onvif/"):
            return self._send(404, b"not found", "text/plain")
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_REQUEST:
            return self._send(413, b"too large", "text/plain")
        data = self.rfile.read(length)
        try:
            header, op = parse(data)
            user = self.server.auth.check(header) if self.server.services.needs_auth(op) else None
            host, port = self.connection.getsockname()[:2]
            body = self.server.services.handle(Request(op, host, port, user))
        except Fault as f:
            if f.subcodes[:1] != ["ter:ActionNotSupported"]:
                self.log_error("%s: %s", f.subcodes[-1], f.reason)
            elif f.reason not in self.server.unsupported:
                self.server.unsupported.add(f.reason)
                self.log_error("%s", f.reason)
            return self._send(f.status, fault_envelope(f))
        except Exception as e:   # a bug in a handler: answer, log, keep serving
            self.log_error("internal error: %r", e)
            return self._send(500, fault_envelope(Fault("Receiver", ["ter:Action"], "internal error")))
        self._send(200, envelope(body))

    def do_GET(self) -> None:
        if self.path.split("?", 1)[0] != "/onvif/snapshot.jpg":
            return self._send(404, b"not found", "text/plain")
        jpeg = self.server.snapshot.get()
        if jpeg is None:
            return self._send(503, b"no dome image (is skydome-live running?)", "text/plain")
        self._send(200, jpeg, "image/jpeg")

    def _send(self, status: int, payload: bytes, ctype: str = "application/soap+xml; charset=utf-8") -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_request(self, code="-", size="-") -> None:
        pass   # NVRs poll constantly; only errors are logged
