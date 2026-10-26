#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hrishikesh Jadhav
"""M0 stream probe: dump PipeWire capture streams and who owns them.

Throwaway script for milestone M0 of docs/dev/PLAN.md. It answers one question: can
each meeting app's microphone stream be told apart by the lookup order in
docs/dev/PLAN.md's "Identifying the app" table?

Modes:
  snapshot (default)  run `pw-dump` once, save it, print every capture stream.
  --monitor           run `pw-dump --monitor`, log every update batch with a
                      timestamp, and print capture-stream events as they happen.
                      Stop with Ctrl-C (or --duration).

Files written to --out:
  pw-dump.json                    snapshot mode: raw `pw-dump` output
  streams.json                    analysis of every Stream/Input/Audio node
  monitor.jsonl                   --monitor: one line per update batch
  mirror-running-serial<N>.json   --monitor: full graph the moment a capture
                                  stream (by object.serial) first reached "running"
  mirror-final.json               --monitor: graph mirror at exit

Process data (/proc) is read while the stream is live, because the process
may be gone by the time the capture ends.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

CAPTURE_CLASS = "Stream/Input/Audio"
PARENT_DEPTH = 5

# Fixtures get committed, so drop anything that looks like a secret from
# command lines (Zoom join links carry the meeting password, for example).
_SECRET_ARG = re.compile(r"(token|pwd|password|passwd|secret|key|auth|sig)", re.I)
_URL_QUERY = re.compile(r"(\w+://[^\s?#]*)[?#]\S*")


def now_iso() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="milliseconds")


# --------------------------------------------------------------------------
# /proc helpers
# --------------------------------------------------------------------------

def redact_arg(arg: str) -> str:
    arg = _URL_QUERY.sub(r"\1?<redacted>", arg)
    if "=" in arg:
        name, _, _value = arg.partition("=")
        if _SECRET_ARG.search(name):
            return f"{name}=<redacted>"
    return arg


def _read(path: str) -> str | None:
    try:
        with open(path, "rb") as f:
            return f.read().decode("utf-8", "replace")
    except OSError:
        return None


def parse_cgroup(text: str | None) -> dict:
    """Pull Flatpak ID, Snap name and systemd unit out of /proc/<pid>/cgroup."""
    out = {"raw": None, "unit": None, "flatpak_id": None, "snap_name": None}
    if not text:
        return out
    # cgroup v2 has a single "0::/path" line; v1 has several, take name=systemd
    # or the unified line.
    path = None
    for line in text.splitlines():
        parts = line.split(":", 2)
        if len(parts) == 3 and (parts[0] == "0" or parts[1] == "name=systemd"):
            path = parts[2]
            if parts[0] == "0":
                break
    out["raw"] = path
    if not path:
        return out
    leaf_units = [seg for seg in path.split("/") if seg.endswith((".scope", ".service"))]
    unit = leaf_units[-1] if leaf_units else None
    out["unit"] = unit
    for seg in reversed(path.split("/")):
        # app-flatpak-<app-id>-<number>.scope
        m = re.match(r"app-flatpak-(.+)-\d+\.scope$", seg)
        if m:
            out["flatpak_id"] = m.group(1)
            break
        # snap.<name>.<app>-<uuid>.scope  /  snap.<name>.<app>.service
        m = re.match(r"snap\.([^.]+)\.", seg)
        if m:
            out["snap_name"] = m.group(1)
            break
    return out


def proc_info(pid: int) -> dict:
    """What /proc says about one PID. Missing fields mean the read failed."""
    base = f"/proc/{pid}"
    info: dict = {"pid": pid, "alive": os.path.isdir(base)}
    if not info["alive"]:
        return info
    try:
        info["exe"] = os.readlink(f"{base}/exe")
    except OSError as e:
        info["exe"] = None
        info["exe_error"] = e.strerror
    if info.get("exe"):
        # A replaced binary reads as "/path/to/exe (deleted)".
        info["exe_basename"] = os.path.basename(info["exe"].removesuffix(" (deleted)"))
    comm = _read(f"{base}/comm")
    info["comm"] = comm.strip() if comm else None
    cmdline = _read(f"{base}/cmdline")
    if cmdline is not None:
        argv = [a for a in cmdline.split("\0") if a]
        info["cmdline"] = [redact_arg(a) for a in argv]
    info["cgroup"] = parse_cgroup(_read(f"{base}/cgroup"))
    stat = _read(f"{base}/stat")
    if stat:
        # Field 4 (ppid) comes after the parenthesised comm, which may hold spaces.
        rest = stat.rsplit(")", 1)[-1].split()
        info["ppid"] = int(rest[1]) if len(rest) > 1 else None
    return info


def parent_chain(pid: int, depth: int = PARENT_DEPTH) -> list[dict]:
    chain = []
    ppid = proc_info(pid).get("ppid")
    while ppid and ppid > 1 and len(chain) < depth:
        p = proc_info(ppid)
        chain.append({k: p.get(k) for k in
                      ("pid", "exe", "exe_basename", "comm", "cmdline", "cgroup")})
        ppid = p.get("ppid")
    return chain


# --------------------------------------------------------------------------
# Graph mirror and analysis
# --------------------------------------------------------------------------

class Graph:
    """In-memory mirror of the PipeWire object graph, keyed by object id."""

    def __init__(self) -> None:
        self.objects: dict[int, dict] = {}

    def apply(self, batch: list[dict]) -> list[tuple[str, dict, dict | None]]:
        """Apply one pw-dump batch. Returns (kind, new_or_removed, previous)."""
        changes = []
        for obj in batch:
            oid = obj["id"]
            prev = self.objects.get(oid)
            if obj.get("info") is None and "type" not in obj:
                # Removal: pw-dump prints {"id": N, "info": null}.
                if prev is not None:
                    del self.objects[oid]
                changes.append(("removed", prev or obj, prev))
            else:
                # Updates carry the whole object, so replace rather than merge.
                self.objects[oid] = obj
                changes.append(("added" if prev is None else "changed", obj, prev))
        return changes

    def props(self, oid: int | None) -> dict:
        obj = self.objects.get(oid) if oid is not None else None
        return ((obj or {}).get("info") or {}).get("props") or {}

    def capture_nodes(self) -> list[dict]:
        return [o for o in self.objects.values() if is_capture_node(o)]

    def links_into(self, node_id: int) -> list[dict]:
        out = []
        for o in self.objects.values():
            if o.get("type", "").endswith(":Link"):
                info = o.get("info") or {}
                if info.get("input-node-id") == node_id:
                    out.append(o)
        return out


def is_capture_node(obj: dict) -> bool:
    if not obj.get("type", "").endswith(":Node"):
        return False
    props = (obj.get("info") or {}).get("props") or {}
    return props.get("media.class") == CAPTURE_CLASS


def serial_of(obj: dict) -> int | None:
    return ((obj.get("info") or {}).get("props") or {}).get("object.serial")


def node_state(obj: dict | None) -> str | None:
    return ((obj or {}).get("info") or {}).get("state")


def classify_source(media_class: str | None, port_props: dict) -> str:
    if port_props.get("port.monitor") or (media_class or "").startswith("Audio/Sink"):
        return "sink-monitor"
    if (media_class or "").startswith("Audio/Source"):
        return "capture-device"
    if (media_class or "").startswith("Stream/Output/Audio"):
        return "app-stream"
    return "other"


def analyze(graph: Graph, node: dict) -> dict:
    info = node.get("info") or {}
    props = info.get("props") or {}
    nid = node["id"]
    client_id = props.get("client.id")
    cprops = graph.props(client_id)

    links = []
    for link in graph.links_into(nid):
        li = link.get("info") or {}
        src_id = li.get("output-node-id")
        sprops = graph.props(src_id)
        pprops = graph.props(li.get("output-port-id"))
        links.append({
            "link_id": link["id"],
            "link_state": li.get("state"),
            "source_node_id": src_id,
            "source_node_name": sprops.get("node.name"),
            "source_description": sprops.get("node.description"),
            "source_media_class": sprops.get("media.class"),
            "source_port_name": pprops.get("port.name"),
            "source_kind": classify_source(sprops.get("media.class"), pprops),
        })

    # Several places claim a PID. pipewire.sec.pid comes from the socket's
    # peer credentials, but for pulse-protocol clients that peer is the
    # pipewire-pulse server itself. application.process.id is self-reported
    # and may be a sandbox-namespace PID inside Flatpak.
    pid_claims = {
        "client.pipewire.sec.pid": cprops.get("pipewire.sec.pid"),
        "client.application.process.id": cprops.get("application.process.id"),
        "node.application.process.id": props.get("application.process.id"),
    }
    pids = []
    for v in pid_claims.values():
        try:
            pid = int(v)
        except (TypeError, ValueError):
            continue
        if pid not in pids:
            pids.append(pid)

    processes = {}
    for pid in pids:
        p = proc_info(pid)
        p["parents"] = parent_chain(pid) if p.get("alive") else []
        processes[str(pid)] = p

    app_props = lambda d: {k: v for k, v in d.items()
                           if k.startswith(("application.", "pipewire.", "media.", "window."))}

    result = {
        "node_id": nid,
        "node_serial": props.get("object.serial"),
        "client_api": cprops.get("client.api"),
        "state": info.get("state"),
        "node_name": props.get("node.name"),
        "media_name": props.get("media.name"),
        "node_app_props": app_props(props),
        "client_id": client_id,
        "client_app_props": app_props(cprops),
        "links_in": links,
        "pid_claims": pid_claims,
        "processes": processes,
    }
    result["identity"] = resolve_identity(result)
    return result


def cmdline0(argv: list[str] | None) -> str | None:
    if not argv:
        return None
    # Chrome and Electron rewrite argv into one space-joined string, so the
    # first NUL-separated field can hold the whole command line.
    first = argv[0].split(" ", 1)[0] if len(argv) == 1 else argv[0]
    return os.path.basename(first)


def resolve_identity(a: dict) -> dict:
    """Walk docs/dev/PLAN.md's lookup order and report what each step yields.

    M0 has no apps.toml yet, so instead of stopping at the first rule match
    this records every step's answer plus the first step that gave anything.
    """
    claims = a["pid_claims"]
    if a.get("client_api") == "pipewire-pulse":
        # sec.pid is the pulse server here; only the self-reported PID is left.
        pid_source = "client.application.process.id"
    else:
        pid_source = "client.pipewire.sec.pid"
    pid = claims.get(pid_source)
    if pid is None:
        pid_source, pid = next(((k, v) for k, v in claims.items() if v is not None),
                               (None, None))
    primary = a["processes"].get(str(pid), {}) if pid is not None else {}
    cg = primary.get("cgroup") or {}
    cprops = a["client_app_props"]
    nprops = a["node_app_props"]

    steps = {
        "1_cgroup": (f"flatpak:{cg['flatpak_id']}" if cg.get("flatpak_id")
                     else f"snap:{cg['snap_name']}" if cg.get("snap_name") else None),
        "2_portal_app_id": cprops.get("pipewire.access.portal.app_id"),
        "3_exe": primary.get("exe_basename"),
        "3_cmdline0": cmdline0(primary.get("cmdline")),
        "4_parents": [p.get("exe_basename") or p.get("comm") for p in primary.get("parents", [])],
        "5_binary": cprops.get("application.process.binary") or nprops.get("application.process.binary"),
        "5_app_name": cprops.get("application.name") or nprops.get("application.name"),
        "systemd_unit": cg.get("unit"),
    }
    first = None
    for key in ("1_cgroup", "2_portal_app_id", "3_exe", "3_cmdline0", "5_binary", "5_app_name"):
        if steps[key]:
            first = (key, steps[key])
            break
    # Does the chosen PID look like the stream owner? A mismatch between the
    # self-reported binary and the real exe/comm means the PID is suspect
    # (pulse server PID, sandbox-namespace PID, or a lie).
    claimed = steps["5_binary"]
    pid_check = {
        "pid": pid,
        "pid_source": pid_source,
        "claimed_binary": claimed,
        "exe_basename": primary.get("exe_basename"),
        "comm": primary.get("comm"),
        "matches_claim": bool(claimed) and claimed in (primary.get("exe_basename"),
                                                       primary.get("comm")),
    }
    return {"steps": steps, "first_hit": first, "pid_check": pid_check}


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------

def format_analysis(a: dict) -> str:
    ident = a["identity"]
    s = ident["steps"]
    lines = [
        f"  node {a['node_id']} (serial {a['node_serial']})  state={a['state']}"
        f"  node.name={a['node_name']!r}  media.name={a['media_name']!r}",
        f"    client {a['client_id']} (api={a['client_api']!r}): app.name={s['5_app_name']!r}"
        f"  binary={s['5_binary']!r}",
        f"    pid claims: {a['pid_claims']}",
        f"    pid check: {ident['pid_check']}",
    ]
    for link in a["links_in"] or [{}]:
        if not link:
            lines.append("    linked from: (no links)")
            continue
        lines.append(
            f"    linked from: node {link['source_node_id']} {link['source_node_name']!r}"
            f" [{link['source_media_class']}] port={link['source_port_name']}"
            f" -> {link['source_kind']} (link {link['link_state']})")
    for pid, p in a["processes"].items():
        if not p.get("alive"):
            lines.append(f"    pid {pid}: not visible in /proc")
            continue
        cmd = " ".join(p.get("cmdline") or [])
        lines.append(f"    pid {pid}: exe={p.get('exe')}  comm={p.get('comm')}")
        lines.append(f"      cmdline: {cmd[:200]}{'…' if len(cmd) > 200 else ''}")
        lines.append(f"      cgroup:  {(p.get('cgroup') or {}).get('raw')}")
        for depth, par in enumerate(p.get("parents", []), 1):
            lines.append(f"      parent {depth}: pid {par['pid']} exe={par.get('exe')}"
                         f" comm={par.get('comm')}")
    lines.append(f"    identity steps: {json.dumps(s)}")
    lines.append(f"    first hit: {ident['first_hit']}")
    return "\n".join(lines)


def write_json(path: Path, data) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=False))
    tmp.replace(path)


# --------------------------------------------------------------------------
# Modes
# --------------------------------------------------------------------------

def run_snapshot(out: Path | None) -> int:
    raw = subprocess.run(["pw-dump", "--no-colors"], check=True,
                         capture_output=True, text=True).stdout
    graph = Graph()
    graph.apply(json.loads(raw))
    analyses = [analyze(graph, n) for n in sorted(graph.capture_nodes(), key=lambda n: n["id"])]

    print(f"{now_iso()}  {len(analyses)} {CAPTURE_CLASS} node(s)")
    for a in analyses:
        print(format_analysis(a))
    if out:
        out.mkdir(parents=True, exist_ok=True)
        (out / "pw-dump.json").write_text(raw)
        write_json(out / "streams.json", {"captured_at": now_iso(), "mode": "snapshot",
                                          "streams": analyses})
        print(f"\nwrote {out}/pw-dump.json and {out}/streams.json")
    return 0


def iter_batches(stream):
    """Yield (wall_iso, monotonic, batch) for each top-level JSON array.

    pw-dump --monitor prints pretty-printed arrays back to back; each one ends
    with "]" alone at column 0, which is where we try to decode.
    """
    buf: list[str] = []
    for line in stream:
        buf.append(line)
        if line.rstrip("\n") != "]":
            continue
        text = "".join(buf)
        try:
            batch = json.loads(text)
        except json.JSONDecodeError:
            continue  # a nested "]" at column 0; keep reading
        buf.clear()
        yield now_iso(), time.monotonic(), batch


def describe_change(kind: str, obj: dict, prev: dict | None, graph: Graph) -> str | None:
    """One console line for changes that matter to detection, else None."""
    typ = (obj.get("type") or (prev or {}).get("type") or "")
    if typ.endswith(":Node") and is_capture_node(obj if kind != "removed" else (prev or {})):
        ref = obj if kind != "removed" else prev
        props = ((ref or {}).get("info") or {}).get("props") or {}
        label = f"capture node {obj['id']} {props.get('node.name')!r} ({props.get('application.name')!r})"
        if kind == "added":
            return f"ADD     {label} state={node_state(obj)}"
        if kind == "removed":
            return f"REMOVE  {label} (last state {node_state(prev)})"
        if node_state(obj) != node_state(prev):
            return f"STATE   {label} {node_state(prev)} -> {node_state(obj)}"
        return None
    if typ.endswith(":Link"):
        li = ((obj if kind != "removed" else prev) or {}).get("info") or {}
        dst = li.get("input-node-id")
        if dst is None or not is_capture_node(graph.objects.get(dst, {})):
            return None
        src = li.get("output-node-id")
        sname = graph.props(src).get("node.name")
        if kind == "added":
            return f"LINK+   {src} {sname!r} -> capture node {dst} ({li.get('state')})"
        if kind == "removed":
            return f"LINK-   {src} {sname!r} -> capture node {dst}"
        if li.get("state") != ((prev or {}).get("info") or {}).get("state"):
            return f"LINK~   {src} {sname!r} -> capture node {dst} now {li.get('state')}"
    return None


def run_monitor(out: Path | None, duration: float | None) -> int:
    if out:
        out.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(["pw-dump", "--monitor", "--no-colors"],
                            stdout=subprocess.PIPE, text=True, bufsize=1)
    graph = Graph()
    log = open(out / "monitor.jsonl", "w") if out else None
    analyses: list[dict] = []
    seen_running: set[int] = set()  # object.serial values
    start = time.monotonic()
    stop = {"flag": False}
    lock = threading.Lock()  # monitor.jsonl and analyses are shared with read_markers

    def on_signal(signum, _frame):
        stop["flag"] = True
        proc.send_signal(signal.SIGINT)

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)
    if duration:
        signal.signal(signal.SIGALRM, on_signal)
        signal.setitimer(signal.ITIMER_REAL, duration)

    def record(reason: str, node: dict, t: str, mono: float, verbose: bool) -> None:
        a = analyze(graph, node)
        with lock:
            analyses.append({"t": t, "rel_s": round(mono - start, 3), "reason": reason,
                             "analysis": a})
        if verbose:
            print(format_analysis(a), flush=True)

    def read_markers() -> None:
        # Each line typed into the terminal becomes a timestamped marker, so
        # "join", "mute", "leave" line up with the stream events.
        for line in sys.stdin:
            text = line.strip() or "mark"
            t, mono = now_iso(), time.monotonic()
            rel = round(mono - start, 3)
            with lock:
                if log:
                    log.write(json.dumps({"t": t, "rel_s": rel, "marker": text}) + "\n")
                    log.flush()
                analyses.append({"t": t, "rel_s": rel, "reason": "marker", "marker": text})
            print(f"[{rel:8.3f}] MARK    {text}", flush=True)

    if sys.stdin.isatty():
        threading.Thread(target=read_markers, daemon=True).start()
    print(f"{now_iso()}  monitoring PipeWire; Ctrl-C to stop."
          " Type a word + Enter to log a marker (join, mute, unmute, leave).", flush=True)
    try:
        for n, (t, mono, batch) in enumerate(iter_batches(proc.stdout)):
            rel = mono - start
            if log:
                with lock:
                    log.write(json.dumps({"t": t, "rel_s": round(rel, 3), "batch": batch}) + "\n")
                    log.flush()
            changes = graph.apply(batch)
            if n == 0:
                nodes = graph.capture_nodes()
                print(f"[{rel:8.3f}] initial state: {len(graph.objects)} objects,"
                      f" {len(nodes)} capture node(s)", flush=True)
                for node in nodes:
                    record("initial", node, t, mono, verbose=True)
                    if node_state(node) == "running":
                        seen_running.add(serial_of(node))
                continue

            touched: dict[int, str] = {}
            for kind, obj, prev in changes:
                line = describe_change(kind, obj, prev, graph)
                if line:
                    print(f"[{rel:8.3f}] {line}", flush=True)
                if kind == "removed" and prev is not None and is_capture_node(prev):
                    with lock:
                        analyses.append({"t": t, "rel_s": round(rel, 3), "reason": "removed",
                                         "node_id": obj["id"],
                                         "node_serial": prev["info"]["props"].get("object.serial")})
                if kind != "removed" and is_capture_node(obj):
                    if kind == "added" or node_state(obj) != node_state(prev):
                        touched[obj["id"]] = f"{kind}:{node_state(obj)}"
                if line and line.startswith("LINK") and kind != "removed":
                    dst = (obj.get("info") or {}).get("input-node-id")
                    touched.setdefault(dst, "link")

            for nid, reason in touched.items():
                node = graph.objects.get(nid)
                if not node:
                    continue
                # PipeWire reuses node ids; the serial is what identifies a stream.
                first_run = node_state(node) == "running" and serial_of(node) not in seen_running
                record(reason, node, t, mono, verbose=first_run)
                if first_run:
                    seen_running.add(serial_of(node))
                    if out:
                        write_json(out / f"mirror-running-serial{serial_of(node)}.json",
                                   list(graph.objects.values()))
            if stop["flag"]:
                break
    finally:
        if proc.poll() is None:
            proc.send_signal(signal.SIGINT)
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
        if log:
            log.close()
        if out:
            write_json(out / "mirror-final.json", list(graph.objects.values()))
            write_json(out / "streams.json", {"captured_at": now_iso(), "mode": "monitor",
                                              "events": analyses})
            print(f"\nwrote monitor.jsonl, streams.json, mirror-*.json to {out}", flush=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--monitor", action="store_true", help="follow pw-dump --monitor")
    ap.add_argument("--out", type=Path, help="directory to save fixtures into")
    ap.add_argument("--duration", type=float, help="--monitor: stop after N seconds")
    args = ap.parse_args(argv)
    if args.monitor:
        return run_monitor(args.out, args.duration)
    return run_snapshot(args.out)


if __name__ == "__main__":
    sys.exit(main())
