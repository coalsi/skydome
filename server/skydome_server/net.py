"""The Jetson's IPv4 interfaces, for ONVIF discovery and GetNetworkInterfaces."""

from __future__ import annotations

import json
import socket
import subprocess
from dataclasses import dataclass


@dataclass(frozen=True)
class Interface:
    name: str
    mac: str
    mtu: int
    address: str
    prefix: int
    dhcp: bool


def parse_ip_json(text: str) -> list[Interface]:
    """`ip -j addr show up` -> interfaces that are up, have a link and an IPv4 address."""
    out = []
    for link in json.loads(text or "[]"):
        flags = link.get("flags", [])
        if "LOOPBACK" in flags or "LOWER_UP" not in flags:
            continue
        for a in link.get("addr_info", []):
            if a.get("family") == "inet":
                out.append(Interface(link["ifname"], link.get("address", ""), int(link.get("mtu", 1500)),
                                     a["local"], int(a["prefixlen"]), bool(a.get("dynamic"))))
    return out


def interfaces() -> list[Interface]:
    try:
        text = subprocess.run(["ip", "-j", "addr", "show", "up"], capture_output=True, text=True,
                              timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    return parse_ip_json(text)


def local_address_for(peer: str) -> str:
    """Our address on the route to `peer` (no packet is sent)."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect((peer, 3702))
            return s.getsockname()[0]
        except OSError:
            return "127.0.0.1"
