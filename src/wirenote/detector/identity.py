# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hrishikesh Jadhav
"""Which app owns a mic stream (docs/dev/PLAN.md › Identifying the app).

PID rule (docs/dev/m0-findings.md §3): for pulse-protocol clients
(client.api == "pipewire-pulse") pipewire.sec.pid is the pipewire-pulse
server, so the self-reported application.process.id is used; for native
clients pipewire.sec.pid comes from the socket and is used. The chosen PID
must pass a check: its exe basename or comm equals the reported
application.process.binary. If it fails (say a Flatpak sandbox PID that
names an unrelated host process), the PID-derived steps are skipped.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Protocol

from ..config.apps import AppRule, AppRules

PARENT_DEPTH = 5
COMM_LEN = 15  # the kernel truncates /proc/<pid>/comm to 15 characters


class ProcReader(Protocol):
    def read(self, pid: int) -> dict | None:
        """exe_basename, comm, cmdline0, flatpak_id, snap_name, unit, ppid; None if gone."""


@dataclass(frozen=True)
class Evidence:
    pid: int | None = None
    pid_source: str | None = None
    pid_ok: bool = False
    flatpak_id: str | None = None
    snap_name: str | None = None
    unit: str | None = None
    portal_app_id: str | None = None
    exe: str | None = None
    cmdline0: str | None = None
    parents: tuple[str, ...] = ()
    binary: str | None = None
    app_name: str | None = None

    def as_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v not in (None, ())}


@dataclass(frozen=True)
class Identity:
    rule: AppRule | None
    matched_by: str | None
    evidence: Evidence = field(default_factory=Evidence)

    @property
    def key(self) -> str | None:
        return self.rule.key if self.rule else None


def gather_evidence(client_props: dict, node_props: dict, proc: ProcReader) -> Evidence:
    def prop(name):
        return client_props.get(name) or node_props.get(name)

    binary = prop("application.process.binary")
    if client_props.get("client.api") == "pipewire-pulse":
        pid_source = "application.process.id"
        pid = _as_int(prop("application.process.id"))
    else:
        pid_source = "pipewire.sec.pid"
        pid = _as_int(client_props.get("pipewire.sec.pid"))
        if pid is None:
            pid_source = "application.process.id"
            pid = _as_int(prop("application.process.id"))

    base = dict(
        pid=pid,
        pid_source=pid_source if pid is not None else None,
        portal_app_id=client_props.get("pipewire.access.portal.app_id"),
        binary=binary,
        app_name=prop("application.name"),
    )
    p = proc.read(pid) if pid is not None else None
    if not p:
        return Evidence(**base)

    names = {p.get("exe_basename"), p.get("comm")}
    pid_ok = binary is None or binary in names or binary[:COMM_LEN] in names
    if not pid_ok:
        return Evidence(**base)

    parents = []
    ppid = p.get("ppid")
    while ppid and ppid > 1 and len(parents) < PARENT_DEPTH:
        pp = proc.read(ppid)
        if not pp:
            break
        parents.append(pp.get("exe_basename") or pp.get("comm") or "?")
        ppid = pp.get("ppid")

    return Evidence(
        **base,
        pid_ok=True,
        flatpak_id=p.get("flatpak_id"),
        snap_name=p.get("snap_name"),
        unit=p.get("unit"),
        exe=p.get("exe_basename"),
        cmdline0=p.get("cmdline0"),
        parents=tuple(parents),
    )


def resolve(ev: Evidence, rules: AppRules) -> Identity:
    """Walk the lookup order and stop at the first exact rule match."""
    steps: list[tuple[str, str, str | None]] = []
    if ev.pid_ok:
        steps += [("cgroup.flatpak", "flatpak", ev.flatpak_id),
                  ("cgroup.snap", "snap", ev.snap_name)]
    steps.append(("portal_app_id", "flatpak", ev.portal_app_id))
    if ev.pid_ok:
        steps += [("exe", "exe", ev.exe), ("cmdline0", "exe", ev.cmdline0)]
        steps += [(f"parent{i + 1}", "exe", name) for i, name in enumerate(ev.parents)]
    steps += [("binary", "binary", ev.binary), ("binary", "exe", ev.binary),
              ("app_name", "app_name", ev.app_name)]
    for label, fld, value in steps:
        rule = rules.lookup(fld, value)
        if rule:
            return Identity(rule, label, ev)
    return Identity(None, None, ev)


class LiveProc:
    """ProcReader over /proc."""

    def read(self, pid: int) -> dict | None:
        base = f"/proc/{pid}"
        try:
            exe = os.readlink(f"{base}/exe").removesuffix(" (deleted)")
        except OSError:
            exe = None
        comm = _read(f"{base}/comm")
        if comm is None and exe is None:
            return None
        cmdline = _read(f"{base}/cmdline") or ""
        argv = [a for a in cmdline.split("\0") if a]
        stat = _read(f"{base}/stat") or ""
        rest = stat.rsplit(")", 1)[-1].split()
        out = {
            "exe_basename": os.path.basename(exe) if exe else None,
            "comm": comm.strip() if comm else None,
            "cmdline0": cmdline0(argv),
            "ppid": int(rest[1]) if len(rest) > 1 else None,
        }
        out.update(parse_cgroup(_read(f"{base}/cgroup")))
        return out


def cmdline0(argv: list[str]) -> str | None:
    if not argv:
        return None
    # Chrome and Electron rewrite argv into one space-joined string.
    first = argv[0].split(" ", 1)[0] if len(argv) == 1 else argv[0]
    return os.path.basename(first)


def parse_cgroup(text: str | None) -> dict:
    out = {"flatpak_id": None, "snap_name": None, "unit": None}
    path = None
    for line in (text or "").splitlines():
        parts = line.split(":", 2)
        if len(parts) == 3 and (parts[0] == "0" or parts[1] == "name=systemd"):
            path = parts[2]
            if parts[0] == "0":
                break
    if not path:
        return out
    segs = path.split("/")
    units = [s for s in segs if s.endswith((".scope", ".service"))]
    out["unit"] = units[-1] if units else None
    for seg in reversed(segs):
        m = re.match(r"app-flatpak-(.+)-\d+\.scope$", seg)
        if m:
            out["flatpak_id"] = m.group(1)
            break
        m = re.match(r"snap\.([^.]+)\.", seg)
        if m:
            out["snap_name"] = m.group(1)
            break
    return out


def _read(path: str) -> str | None:
    try:
        with open(path, "rb") as f:
            return f.read().decode("utf-8", "replace")
    except OSError:
        return None


def _as_int(v) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None
