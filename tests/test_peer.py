# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hrishikesh Jadhav
"""Who owns a loopback TCP socket."""

from __future__ import annotations

import os
import socket

from wirenote.net import peer
from wirenote.net.peer import listener_uid, tcp_owner


def _listener():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen()
    return s


def test_listener_uid_is_me():
    s = _listener()
    try:
        assert listener_uid(s.getsockname()[1]) == os.getuid()
    finally:
        s.close()


def test_tcp_owner_of_real_client():
    s = _listener()
    c = socket.create_connection(s.getsockname())
    conn, addr = s.accept()
    try:
        assert tcp_owner(s.getsockname()[1], addr[1]) == os.getuid()
    finally:
        conn.close()
        c.close()
        s.close()


def test_unknown_is_none():
    s = _listener()
    port = s.getsockname()[1]
    s.close()
    assert listener_uid(port) is None
    assert tcp_owner(port, 1) is None


def test_parses_fixture_with_other_uid(tmp_path, monkeypatch):
    # 127.0.0.1:8765 listening, owned by uid 1001; a client on 127.0.0.1:40000 -> :8765, uid 1002
    fixture = tmp_path / "tcp"
    fixture.write_text(
        "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"
        "   0: 0100007F:223D 00000000:0000 0A 00000000:00000000 00:00000000 00000000  1001        0 1 1 0 100 0 0 10 0\n"
        "   1: 0100007F:9C40 0100007F:223D 01 00000000:00000000 00:00000000 00000000  1002        0 2 1 0 20 4 30 10 -1\n")
    monkeypatch.setattr(peer, "PROC_TCP", fixture)
    assert listener_uid(8765) == 1001
    assert tcp_owner(8765, 40000) == 1002
    assert tcp_owner(8765, 40001) is None


HEADER = "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"
V6_ANY = "00000000000000000000000000000000"
V6_LOOP = "00000000000000000000000001000000"


def _tables(tmp_path, monkeypatch, v4_rows=(), v6_rows=()):
    """Rows: (local_hex_ip, port, uid) of listening sockets."""
    for name, rows, attr in (("tcp", v4_rows, "PROC_TCP"), ("tcp6", v6_rows, "PROC_TCP6")):
        f = tmp_path / name
        f.write_text(HEADER + "".join(
            f"   {i}: {ip}:{port:04X} {'0' * len(ip)}:0000 0A 00000000:00000000 00:00000000 "
            f"00000000  {uid}        0 {i} 1 0 100 0 0 10 0\n" for i, (ip, port, uid) in enumerate(rows)))
        monkeypatch.setattr(peer, attr, f)


def test_dual_stack_wildcard_counts_for_ipv4(tmp_path, monkeypatch):
    """[::]:port also takes 127.0.0.1 connections; it must not look like "nobody listens"."""
    _tables(tmp_path, monkeypatch, v6_rows=[(V6_ANY, 11434, 1002)])
    assert listener_uid(11434) == 1002


def test_most_specific_listener_wins(tmp_path, monkeypatch):
    _tables(tmp_path, monkeypatch, v4_rows=[("0100007F", 11434, 1001)],
            v6_rows=[(V6_ANY, 11434, 1002), (V6_LOOP, 11434, 1003)])
    assert listener_uid(11434) == 1001
    assert listener_uid(11434, "127.0.0.1") == 1001
    assert listener_uid(11434, "::1") == 1003


def test_trusted_owner(monkeypatch):
    monkeypatch.setattr(peer, "uid_min", lambda: 1000)
    me = os.getuid()
    assert peer.trusted_owner(me) and peer.trusted_owner(0) and peer.trusted_owner(990)
    other = 1000 if me != 1000 else 1001
    assert not peer.trusted_owner(other) and not peer.trusted_owner(None)
