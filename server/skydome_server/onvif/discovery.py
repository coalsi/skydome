"""WS-Discovery (ONVIF Core §7): answer Probe/Resolve on 239.255.255.250:3702
and announce Hello/Bye, so NVRs list the dome without being told its address."""

from __future__ import annotations

import io
import socket
import struct
import sys
import threading
import uuid
import xml.etree.ElementTree as ET
from typing import Callable
from xml.sax.saxutils import escape

from ..net import interfaces, local_address_for

GROUP = "239.255.255.250"
PORT = 3702
_WSA = "http://schemas.xmlsoap.org/ws/2004/08/addressing"
_WSD = "http://schemas.xmlsoap.org/ws/2005/04/discovery"
_DN = "http://www.onvif.org/ver10/network/wsdl"
_TDS = "http://www.onvif.org/ver10/device/wsdl"
_OUR_TYPES = {(_DN, "NetworkVideoTransmitter"), (_TDS, "Device")}
_ANONYMOUS = "http://schemas.xmlsoap.org/ws/2004/08/addressing/role/anonymous"
_TO_ALL = "urn:schemas-xmlsoap-org:ws:2005:04:discovery"


def _local(tag: str) -> str:
    return tag.rpartition("}")[2]


def _find(root: ET.Element, *path: str) -> ET.Element | None:
    """Walk by local names, so SOAP 1.1 and 1.2 envelopes both work."""
    el = root
    for name in path:
        el = next((c for c in el if _local(c.tag) == name), None)
        if el is None:
            return None
    return el


class Discovery:
    def __init__(self, endpoint: str, scopes: list[str], xaddr: Callable[[str], str],
                 local_for: Callable[[str], str] = local_address_for, log=print):
        self.endpoint = endpoint          # urn:uuid:...
        self.scopes = scopes
        self.xaddr = xaddr                # our address -> device service URL
        self._local_for = local_for
        self._log = log
        self._instance = 1 + int(uuid.uuid4().int % 2**31)
        self._number = 0
        self._sock: socket.socket | None = None
        self._joined: set[str] = set()

    # ---- messages (pure, tested) -------------------------------------------------

    def handle(self, data: bytes, peer: str) -> bytes | None:
        """The reply to a datagram, or None if it isn't for us."""
        if b"<!DOCTYPE" in data or b"<!ENTITY" in data:
            return None
        try:
            prefixes = {p: u for _, (p, u) in ET.iterparse(io.BytesIO(data), events=("start-ns",))}
            root = ET.fromstring(data)
        except ET.ParseError:
            return None
        body = _find(root, "Body")
        msg_id = _find(root, "Header", "MessageID")
        relates = msg_id.text.strip() if msg_id is not None and msg_id.text else ""
        probe = _find(body, "Probe") if body is not None else None
        if probe is not None:
            types = _find(probe, "Types")
            scopes = _find(probe, "Scopes")
            if not self._types_match(types.text if types is not None else None, prefixes):
                return None
            if not self._scopes_match(scopes.text if scopes is not None else None):
                return None
            return self._message("ProbeMatches", relates, _ANONYMOUS,
                                 f"<d:ProbeMatches><d:ProbeMatch>{self._endpoint_xml(peer)}</d:ProbeMatch></d:ProbeMatches>")
        resolve = _find(body, "Resolve") if body is not None else None
        if resolve is not None:
            address = _find(resolve, "EndpointReference", "Address")
            if address is None or (address.text or "").strip() != self.endpoint:
                return None
            return self._message("ResolveMatches", relates, _ANONYMOUS,
                                 f"<d:ResolveMatches><d:ResolveMatch>{self._endpoint_xml(peer)}</d:ResolveMatch></d:ResolveMatches>")
        return None

    def hello(self, local: str) -> bytes:
        return self._message("Hello", None, _TO_ALL, f"<d:Hello>{self._endpoint_xml(None, local)}</d:Hello>")

    def bye(self) -> bytes:
        return self._message("Bye", None, _TO_ALL,
                             f"<d:Bye><a:EndpointReference><a:Address>{self.endpoint}</a:Address></a:EndpointReference></d:Bye>")

    @staticmethod
    def _types_match(text: str | None, prefixes: dict[str, str]) -> bool:
        if not text or not text.strip():
            return True   # an empty Probe asks for everything
        for qname in text.split():
            prefix, _, local = qname.rpartition(":")
            ns = prefixes.get(prefix)
            if (ns, local) in _OUR_TYPES or (ns is None and local == "NetworkVideoTransmitter"):
                return True
        return False

    def _scopes_match(self, text: str | None) -> bool:
        # Default MatchBy (RFC 3986): each scope asked for is a prefix of one of ours
        if not text or not text.strip():
            return True
        ours = [s.lower() for s in self.scopes]
        return all(any(o == q or o.startswith(q.rstrip("/") + "/") for o in ours) for q in text.lower().split())

    def _endpoint_xml(self, peer: str | None, local: str | None = None) -> str:
        local = local or self._local_for(peer)
        return (f"<a:EndpointReference><a:Address>{self.endpoint}</a:Address></a:EndpointReference>"
                "<d:Types>dn:NetworkVideoTransmitter tds:Device</d:Types>"
                f"<d:Scopes>{escape(' '.join(self.scopes))}</d:Scopes>"
                f"<d:XAddrs>{escape(self.xaddr(local))}</d:XAddrs><d:MetadataVersion>1</d:MetadataVersion>")

    def _message(self, action: str, relates_to: str | None, to: str, body: str) -> bytes:
        self._number += 1
        relates = f"<a:RelatesTo>{escape(relates_to)}</a:RelatesTo>" if relates_to else ""
        return (f'<?xml version="1.0" encoding="UTF-8"?>\n'
                f'<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" xmlns:a="{_WSA}" xmlns:d="{_WSD}" '
                f'xmlns:dn="{_DN}" xmlns:tds="{_TDS}"><s:Header>'
                f"<a:MessageID>urn:uuid:{uuid.uuid4()}</a:MessageID>{relates}<a:To>{to}</a:To>"
                f"<a:Action>{_WSD}/{action}</a:Action>"
                f'<d:AppSequence InstanceId="{self._instance}" MessageNumber="{self._number}"/>'
                f"</s:Header><s:Body>{body}</s:Body></s:Envelope>").encode()

    # ---- socket ------------------------------------------------------------------

    def serve(self, stop: threading.Event) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
        sock.bind(("", PORT))
        sock.settimeout(1.0)
        self._sock = sock
        rejoin = 0.0
        while not stop.is_set():
            rejoin -= 1.0
            if rejoin <= 0:
                self._join_all()   # Wi-Fi reconnects and new interfaces
                rejoin = 30.0
            try:
                data, (addr, port) = sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError as e:
                self._log(f"discovery: {e}", file=sys.stderr)
                continue
            reply = self.handle(data, addr)
            if reply:
                try:
                    sock.sendto(reply, (addr, port))
                except OSError as e:
                    self._log(f"discovery: reply to {addr}: {e}", file=sys.stderr)
        self._announce(self.bye())
        sock.close()

    def _join_all(self) -> None:
        current = {i.address for i in interfaces()}
        for addr in current - self._joined:
            try:
                self._sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                                      struct.pack("4s4s", socket.inet_aton(GROUP), socket.inet_aton(addr)))
            except OSError:
                pass   # already a member, or the interface went away again
            self._send_from(addr, self.hello(addr))
        self._joined = current

    def _announce(self, msg: bytes) -> None:
        for addr in self._joined:
            self._send_from(addr, msg)

    def _send_from(self, local: str, msg: bytes) -> None:
        try:
            self._sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(local))
            self._sock.sendto(msg, (GROUP, PORT))
        except OSError as e:
            self._log(f"discovery: announce on {local}: {e}", file=sys.stderr)
