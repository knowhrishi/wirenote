# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hrishikesh Jadhav
"""Who owns a loopback TCP socket, from /proc/net/tcp and tcp6.

The web UI and Ollama listen on loopback, which every account on the machine
can reach. These lookups let us refuse other users' connections, and refuse
to talk to a port that another user has taken. A socket on [::] also takes
IPv4 connections (dual stack), so IPv6 listeners count for 127.0.0.1 too.
"""

from __future__ import annotations

import ipaddress
import os
import re
from pathlib import Path

PROC_TCP = Path("/proc/net/tcp")
PROC_TCP6 = Path("/proc/net/tcp6")
LOGIN_DEFS = Path("/etc/login.defs")
LISTEN = "0A"
V4_ANY = ipaddress.ip_address("0.0.0.0")
V6_ANY = ipaddress.ip_address("::")

Address = ipaddress.IPv4Address | ipaddress.IPv6Address


def _ip(hex_ip: str) -> Address:
    """/proc/net/tcp* write addresses as 32-bit words in host (little-endian) order."""
    raw = bytes.fromhex(hex_ip)
    return ipaddress.ip_address(b"".join(raw[i:i + 4][::-1] for i in range(0, len(raw), 4)))


def _rows() -> list[tuple[Address, int, Address, int, str, int]]:
    """(local_ip, local_port, remote_ip, remote_port, state, uid) per socket."""
    rows = []
    for path in (PROC_TCP, PROC_TCP6):
        try:
            lines = path.read_text().splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            f = line.split()
            if len(f) < 8:
                continue
            try:
                (lip, lport), (rip, rport) = f[1].split(":"), f[2].split(":")
                rows.append((_ip(lip), int(lport, 16), _ip(rip), int(rport, 16), f[3], int(f[7])))
            except ValueError:
                continue
    return rows


def _same(a: Address, b: Address) -> bool:
    """Equal, counting ::ffff:127.0.0.1 (an IPv4 peer of a dual-stack socket) as 127.0.0.1."""
    a = getattr(a, "ipv4_mapped", None) or a
    b = getattr(b, "ipv4_mapped", None) or b
    return a == b


def tcp_owner(local_port: int, remote_port: int, host: str = "127.0.0.1") -> int | None:
    """UID of the client side of a connection to our host:local_port that comes
    from host:remote_port; None if there is no such socket."""
    ip = ipaddress.ip_address(host)
    for lip, lport, rip, rport, _state, uid in _rows():
        if lport == remote_port and rport == local_port and _same(lip, ip) and _same(rip, ip):
            return uid
    return None


def listener_uid(port: int, host: str = "127.0.0.1") -> int | None:
    """UID of the socket that a connection to host:port would reach (the exact
    address first, then a wildcard); None if nothing listens there."""
    ip = ipaddress.ip_address(host)
    if ip.version == 4:
        ranks = [lambda a: a == ip, lambda a: a == V4_ANY,
                 lambda a: a.version == 6 and a.ipv4_mapped == ip, lambda a: a == V6_ANY]
    else:
        ranks = [lambda a: a == ip, lambda a: a == V6_ANY]
    listening = [(lip, uid) for lip, lport, _r, _rp, state, uid in _rows()
                 if state == LISTEN and lport == port]
    for match in ranks:
        for lip, uid in listening:
            if match(lip):
                return uid
    return None


def uid_min() -> int:
    """The first UID for people (UID_MIN in /etc/login.defs); below it are system accounts."""
    try:
        m = re.search(r"^\s*UID_MIN\s+(\d+)", LOGIN_DEFS.read_text(), re.MULTILINE)
    except OSError:
        m = None
    return int(m.group(1)) if m else 1000


def trusted_owner(uid: int | None) -> bool:
    """A socket we may hand private data to: ours, root's, or a system account's
    (Ollama's installer runs it as the system user "ollama"). Never another person's."""
    return uid is not None and (uid == os.getuid() or uid == 0 or 0 < uid < uid_min())
