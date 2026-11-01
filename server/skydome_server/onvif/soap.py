"""SOAP 1.2 plumbing for the ONVIF services: parsing, envelopes, faults and
WS-Security UsernameToken authentication (ONVIF Core §5.9)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from xml.sax.saxutils import escape

NS = {
    "s": "http://www.w3.org/2003/05/soap-envelope",
    "tds": "http://www.onvif.org/ver10/device/wsdl",
    "trt": "http://www.onvif.org/ver10/media/wsdl",
    "timg": "http://www.onvif.org/ver20/imaging/wsdl",
    "tt": "http://www.onvif.org/ver10/schema",
    "ter": "http://www.onvif.org/ver10/error",
    "wsse": "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd",
    "wsu": "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd",
}
_ENVELOPE_NS = " ".join(f'xmlns:{p}="{u}"' for p, u in NS.items() if p not in ("wsse", "wsu"))
_DIGEST = "#PasswordDigest"
MAX_REQUEST = 256 * 1024


class Fault(Exception):
    """A SOAP fault. code is "Sender" (the client's mistake, HTTP 400) or
    "Receiver" (ours, HTTP 500); subcodes are ONVIF's ter: codes, outermost first."""

    def __init__(self, code: str, subcodes: list[str], reason: str):
        super().__init__(reason)
        self.code, self.subcodes, self.reason = code, subcodes, reason

    @property
    def status(self) -> int:
        return 400 if self.code == "Sender" else 500


def not_supported(what: str) -> Fault:
    return Fault("Receiver", ["ter:ActionNotSupported"], f"{what} is not supported")


def invalid(subcode: str, reason: str) -> Fault:
    return Fault("Sender", ["ter:InvalidArgVal", subcode], reason)


def envelope(body: str) -> bytes:
    return (f'<?xml version="1.0" encoding="UTF-8"?>\n<s:Envelope {_ENVELOPE_NS}>'
            f"<s:Body>{body}</s:Body></s:Envelope>").encode()


def fault_envelope(f: Fault) -> bytes:
    code = ""
    for sub in reversed(f.subcodes):
        code = f"<s:Subcode><s:Value>{sub}</s:Value>{code}</s:Subcode>"
    return envelope(f"<s:Fault><s:Code><s:Value>s:{f.code}</s:Value>{code}</s:Code>"
                    f'<s:Reason><s:Text xml:lang="en">{escape(f.reason)}</s:Text></s:Reason></s:Fault>')


def parse(data: bytes) -> tuple[ET.Element | None, ET.Element]:
    """(Header or None, the operation element inside Body)."""
    if len(data) > MAX_REQUEST:
        raise Fault("Sender", ["ter:InvalidArgs"], "request too large")
    if b"<!DOCTYPE" in data or b"<!ENTITY" in data:
        raise Fault("Sender", ["ter:InvalidArgs"], "DTDs are not accepted")
    try:
        root = ET.fromstring(data)
    except ET.ParseError as e:
        raise Fault("Sender", ["ter:WellFormed"], f"not well-formed XML: {e}") from None
    body = root.find("s:Body", NS)
    if body is None or len(body) == 0:
        raise Fault("Sender", ["ter:InvalidArgs"], "no SOAP 1.2 Body")
    return root.find("s:Header", NS), body[0]


def split_tag(el: ET.Element) -> tuple[str, str]:
    """'{ns}Local' -> (ns, 'Local')"""
    ns, _, local = el.tag[1:].partition("}") if el.tag.startswith("{") else ("", "", el.tag)
    return ns, local


def child_text(el: ET.Element | None, path: str) -> str | None:
    if el is None:
        return None
    found = el.find(path, NS)
    return found.text.strip() if found is not None and found.text is not None else None


def xml_bool(text: str | None, default: bool = False) -> bool:
    return default if text is None else text.strip().lower() in ("true", "1")


class UsernameTokenAuth:
    """Checks the WS-Security UsernameToken every ONVIF client sends. Digest =
    Base64(SHA-1(nonce + created + password)); a nonce may be used once, and
    `created` must be within `window` seconds of our clock (chrony keeps it right)."""

    def __init__(self, users: dict[str, str], window: float = 3600.0):
        self.users = users
        self.window = window
        self._seen: dict[str, float] = {}
        self._lock = threading.Lock()

    def check(self, header: ET.Element | None, now: float | None = None) -> str:
        """Returns the user name, or raises Fault ter:NotAuthorized."""
        now = time.time() if now is None else now
        token = header.find("wsse:Security/wsse:UsernameToken", NS) if header is not None else None
        if token is None:
            raise _unauthorized("this request needs a WS-Security UsernameToken")
        user = child_text(token, "wsse:Username")
        pw_el = token.find("wsse:Password", NS)
        given = (pw_el.text or "").strip() if pw_el is not None else ""
        expected = self.users.get(user or "")
        if expected is None:
            raise _unauthorized("unknown user or wrong password")
        if pw_el is not None and pw_el.get("Type", "").endswith(_DIGEST):
            nonce = child_text(token, "wsse:Nonce") or ""
            created = child_text(token, "wsu:Created") or ""
            try:
                raw_nonce = base64.b64decode(nonce, validate=True)
            except ValueError:
                raise _unauthorized("bad nonce") from None
            digest = base64.b64encode(hashlib.sha1(raw_nonce + created.encode() + expected.encode()).digest())
            if not hmac.compare_digest(digest, given.encode()):
                raise _unauthorized("unknown user or wrong password")
            stamp = _parse_time(created)
            if stamp is None or abs(now - stamp) > self.window:
                raise _unauthorized("the token's Created time is too far from the camera's clock")
            with self._lock:
                self._seen = {n: t for n, t in self._seen.items() if now - t <= self.window}
                if nonce in self._seen:
                    raise _unauthorized("nonce already used")
                self._seen[nonce] = now
        elif not hmac.compare_digest(given.encode(), expected.encode()):
            raise _unauthorized("unknown user or wrong password")
        return user


def _unauthorized(reason: str) -> Fault:
    return Fault("Sender", ["ter:NotAuthorized"], reason)


def _parse_time(text: str) -> float | None:
    try:
        t = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (t if t.tzinfo else t.replace(tzinfo=timezone.utc)).timestamp()
