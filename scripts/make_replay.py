#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hrishikesh Jadhav
"""Redaction pass: turn an M0 fixture into a compact replay trace for tests.

    python3 scripts/make_replay.py tests/fixtures/jitsi-chrome tests/replay/jitsi-chrome.json

Keeps only what the detector reads: stream, source and sink nodes, the links
between them, and the clients that own streams, with a whitelist of
properties. Room names in media.name become "Room" (the "| Jitsi Meet"
suffix stays); device names become source-N / sink-N; hostnames, user names,
command lines and cgroup paths are dropped. Process evidence comes from
streams.json, reduced to the fields identity.ProcReader returns.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

NODE_PROPS = ("media.class", "node.name", "media.name", "client.id", "object.serial",
              "application.process.id", "application.process.binary", "application.name")
CLIENT_PROPS = ("client.api", "object.serial", "pipewire.sec.pid", "application.process.id",
                "application.process.binary", "application.name",
                "pipewire.access.portal.app_id")
PROC_FIELDS = ("exe_basename", "comm", "ppid")


def scrub_media_name(name: str | None) -> str | None:
    if name and " | " in name:
        return "Room | " + name.rsplit(" | ", 1)[1]
    return name


def keep_node(props: dict) -> bool:
    mc = props.get("media.class", "")
    return mc.startswith(("Stream/", "Audio/Source", "Audio/Sink"))


def device_name(props: dict, oid: int) -> str:
    mc = props.get("media.class", "")
    return f"{'source' if mc.startswith('Audio/Source') else 'sink'}-{oid}"


def scrub_node(obj: dict) -> dict:
    info = obj["info"]
    p = info.get("props") or {}
    out = {k: p[k] for k in NODE_PROPS if k in p}
    if not p.get("media.class", "").startswith("Stream/"):
        out["node.name"] = device_name(p, obj["id"])
    if "media.name" in out:
        out["media.name"] = scrub_media_name(out["media.name"])
    return {"id": obj["id"], "type": obj["type"], "info": {"state": info.get("state"), "props": out}}


def scrub_client(obj: dict) -> dict:
    p = obj["info"].get("props") or {}
    return {"id": obj["id"], "type": obj["type"],
            "info": {"props": {k: p[k] for k in CLIENT_PROPS if k in p}}}


def scrub_link(obj: dict) -> dict:
    i = obj["info"]
    return {"id": obj["id"], "type": obj["type"],
            "info": {k: i.get(k) for k in ("output-node-id", "input-node-id", "state")}}


def build(fixture: Path) -> dict:
    records = [json.loads(line) for line in (fixture / "monitor.jsonl").open()]
    records = [r for r in records if "batch" in r]
    # A client can appear batches before its stream node, so collect every
    # client id that ever owns a stream first.
    stream_clients: set[int] = set()
    for rec in records:
        for obj in rec["batch"]:
            p = ((obj.get("info") or {}).get("props") or {})
            if obj.get("type", "").endswith(":Node") and p.get("media.class", "").startswith("Stream/"):
                if p.get("client.id") is not None:
                    stream_clients.add(p["client.id"])

    kept: dict[int, str] = {}       # id -> type, for objects currently in the trace
    batches = []
    for rec in records:
        out = []
        for obj in rec["batch"]:
            oid = obj["id"]
            typ = obj.get("type", "")
            if obj.get("info") is None and "type" not in obj:
                if oid in kept:
                    out.append({"id": oid, "info": None})
                    del kept[oid]
                continue
            p = ((obj.get("info") or {}).get("props") or {})
            if typ.endswith(":Node") and keep_node(p):
                out.append(scrub_node(obj))
            elif typ.endswith(":Client") and oid in stream_clients:
                out.append(scrub_client(obj))
            elif typ.endswith(":Link"):
                i = obj["info"]
                if i.get("output-node-id") in kept or i.get("input-node-id") in kept:
                    out.append(scrub_link(obj))
                else:
                    continue
            else:
                kept.pop(oid, None)  # id reused by an object we drop
                continue
            kept[oid] = typ
        if out:
            batches.append({"rel_s": rec["rel_s"], "batch": out})

    procs: dict[str, dict] = {}
    for ev in json.loads((fixture / "streams.json").read_text()).get("events", []):
        for pid, p in (ev.get("analysis") or {}).get("processes", {}).items():
            if not p.get("alive") or pid in procs:
                continue
            procs[pid] = proc_entry(p)
            parents = p.get("parents", [])
            for parent in parents:
                procs.setdefault(str(parent["pid"]), proc_entry(parent))
            # streams.json stores parents as a list; link them by ppid so the
            # fake reader can walk the chain like /proc.
            for child, parent in zip([p] + parents, parents):
                procs[str(child["pid"])]["ppid"] = parent["pid"]
    return {"source": fixture.name, "processes": procs, "batches": batches}


def proc_entry(p: dict) -> dict:
    cg = p.get("cgroup") or {}
    cmd = p.get("cmdline") or []
    first = cmd[0].split(" ", 1)[0] if len(cmd) == 1 else (cmd[0] if cmd else None)
    return {
        **{k: p.get(k) for k in PROC_FIELDS},
        "cmdline0": Path(first).name if first else None,
        "flatpak_id": cg.get("flatpak_id"),
        "snap_name": cg.get("snap_name"),
        "unit": cg.get("unit"),
    }


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__, file=sys.stderr)
        return 2
    trace = build(Path(argv[1]))
    Path(argv[2]).parent.mkdir(parents=True, exist_ok=True)
    Path(argv[2]).write_text(json.dumps(trace, indent=1) + "\n")
    n = sum(len(b["batch"]) for b in trace["batches"])
    print(f"{argv[2]}: {len(trace['batches'])} batches, {n} objects, "
          f"{len(trace['processes'])} processes")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
