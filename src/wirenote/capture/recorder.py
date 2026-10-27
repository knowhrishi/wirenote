# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hrishikesh Jadhav
"""Two-track recording with pw-record (docs/dev/PLAN.md › Capture, docs/dev/m2-m4-design.md › M2).

One directory per recording holds manifest.json plus one WAV per segment:
mic-000.wav, system-000.wav, ... A new segment starts when a track's child
is restarted or the mic target changes; processing stitches segments by
their start offsets.

- Mic: the Audio/Source the meeting app is linked to. With no target the
  child follows the default source, like the app does.
- System: the default sink's monitor (stream.capture.sink=true), no target.
  WirePlumber moves it when the default sink changes
  (linking.follow-default-target, checked 2026-10-26), so a sink switch does
  not need a new segment.
"""

from __future__ import annotations

import asyncio
import ctypes
import datetime as dt
import json
import os
import signal
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import wav

MANIFEST = "manifest.json"
FLAGS = "flags.json"
FLAG_DEDUPE_S = 2.0       # docs/dev/PLAN.md › Defaults › Flagged moments
STOP_WAIT_S = 3.0
MAX_RESTARTS = 3


def now_iso() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def _die_with_parent() -> None:
    """Child setup: get SIGINT (clean WAV close) if the daemon dies."""
    try:
        ctypes.CDLL("libc.so.6", use_errno=True).prctl(1, signal.SIGINT)  # PR_SET_PDEATHSIG
    except OSError:
        pass


async def spawn_pw_record(argv: list[str]):
    return await asyncio.create_subprocess_exec(
        *argv, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL, preexec_fn=_die_with_parent)


Spawner = Callable[[list[str]], Awaitable]


@dataclass
class Segment:
    track: str                    # "mic" or "system"
    file: str
    target: str | None            # node.name, or None for the default
    start_offset_s: float
    pid: int | None = None
    end_offset_s: float | None = None
    end_reason: str | None = None


@dataclass
class _Track:
    name: str
    target: str | None
    proc: object = None
    segment: Segment | None = None
    last_size: int = 0
    last_growth: float = 0.0
    stalled: bool = False
    restarts: int = 0


@dataclass(frozen=True)
class TrackEvent:
    track: str
    kind: str        # "stalled", "recovered", "restarted", "gave_up"
    detail: str = ""


@dataclass
class RecordingMeta:
    app: str
    name: str
    service: str | None = None
    room: str | None = None
    calendar: dict | None = None
    extra: dict = field(default_factory=dict)


class Recording:
    def __init__(self, root: Path, meta: RecordingMeta, node_prefix: str = "wirenote-",
                 heartbeat_s: float = 5.0, spawn: Spawner = spawn_pw_record,
                 clock: Callable[[], float] = time.monotonic, system_target: str = "") -> None:
        self.started_wall = dt.datetime.now().astimezone()
        self.id = f"{self.started_wall:%Y%m%d-%H%M%S}-{meta.app}"
        self.dir = root / self.id
        self.meta = meta
        self.prefix = node_prefix
        self.heartbeat_s = heartbeat_s
        self.spawn = spawn
        self.clock = clock
        self.system_target = system_target or None
        self.t0 = clock()
        self.segments: list[Segment] = []
        self.tracks: dict[str, _Track] = {}
        self.ended: str | None = None
        self.end_reason: str | None = None
        self.flags: list[float] = []

    # Lifecycle -------------------------------------------------------------

    async def start(self, mic_target: str | None) -> None:
        self.dir.mkdir(mode=0o700, parents=True, exist_ok=False)
        self.tracks = {"mic": _Track("mic", mic_target),
                       "system": _Track("system", self.system_target)}
        try:
            for t in self.tracks.values():
                await self._open_segment(t)
        except BaseException:
            await self.stop("capture failed to start")
            raise
        self._write_manifest()

    async def stop(self, reason: str) -> Path:
        for t in self.tracks.values():
            await self._close_segment(t, reason)
        self.ended, self.end_reason = now_iso(), reason
        self._write_manifest()
        return self.dir

    def flag(self) -> float | None:
        """Mark this moment as important. Returns the offset in seconds on the transcript
        time base (the same zero as Segment.start_offset_s), or None if it is too close to
        a flag already set."""
        t = round(self.clock() - self.t0, 1)
        added = add_flag(self.flags, t)
        if added is None:
            return None
        self.flags = added
        write_flags(self.dir, self.flags)
        return t

    async def retarget_mic(self, target: str | None) -> bool:
        t = self.tracks.get("mic")
        if t is None or t.target == target:
            return False
        await self._close_segment(t, f"mic moved to {target or 'default'}")
        t.target = target
        await self._open_segment(t)
        self._write_manifest()
        return True

    async def heartbeat(self) -> list[TrackEvent]:
        """Check each track; call about once a second (docs/dev/PLAN.md › Capture heartbeat)."""
        events: list[TrackEvent] = []
        now = self.clock()
        for t in self.tracks.values():
            if t.proc is None:
                continue
            rc = t.proc.returncode
            if rc is not None:
                await self._close_segment(t, f"pw-record exited with {rc}")
                if t.restarts >= MAX_RESTARTS:
                    t.proc = None
                    events.append(TrackEvent(t.name, "gave_up", f"exited {t.restarts + 1} times"))
                    continue
                t.restarts += 1
                await self._open_segment(t)
                events.append(TrackEvent(t.name, "restarted", f"pw-record exited with {rc}"))
                continue
            size = self._size(t)
            if size > t.last_size:
                t.last_size, t.last_growth = size, now
                if t.stalled:
                    t.stalled = False
                    events.append(TrackEvent(t.name, "recovered"))
            elif not t.stalled and now - t.last_growth >= self.heartbeat_s:
                t.stalled = True
                events.append(TrackEvent(t.name, "stalled",
                                         f"no audio for {self.heartbeat_s:g} s"))
        if events:
            self._write_manifest()
        return events

    # Internals -------------------------------------------------------------

    def _argv(self, t: _Track, path: Path) -> list[str]:
        props = f"node.name={self.prefix}{t.name} media.name={self.prefix}{t.name}"
        if t.name == "system":
            props += " stream.capture.sink=true"
        argv = ["pw-record", "--rate", str(wav.RATE), "--channels", "1", "--format", "s16",
                "-P", "{ " + props + " }"]
        if t.target:
            argv += ["--target", t.target]
        return argv + [str(path)]

    async def _open_segment(self, t: _Track) -> None:
        n = sum(1 for s in self.segments if s.track == t.name)
        name = f"{t.name}-{n:03d}.wav"
        seg = Segment(t.name, name, t.target, round(self.clock() - self.t0, 3))
        t.proc = await self.spawn(self._argv(t, self.dir / name))
        seg.pid = t.proc.pid
        # The header alone is not audio.
        t.segment, t.last_size, t.last_growth, t.stalled = seg, wav.HEADER, self.clock(), False
        self.segments.append(seg)

    async def _close_segment(self, t: _Track, reason: str) -> None:
        seg, proc = t.segment, t.proc
        if seg is None:
            return
        if proc is not None and proc.returncode is None:
            try:
                proc.send_signal(signal.SIGINT)
                await asyncio.wait_for(proc.wait(), STOP_WAIT_S)
            except ProcessLookupError:
                pass
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
        path = self.dir / seg.file
        if path.exists():
            wav.repair(path)
        seg.end_offset_s = round(self.clock() - self.t0, 3)
        seg.end_reason = reason
        t.segment = None

    def _size(self, t: _Track) -> int:
        try:
            return (self.dir / t.segment.file).stat().st_size if t.segment else 0
        except OSError:
            return 0

    def manifest(self) -> dict:
        m = self.meta
        return {"version": 1, "id": self.id, "app": m.app, "name": m.name,
                "service": m.service, "room": m.room, "calendar": m.calendar, **m.extra,
                "started": self.started_wall.isoformat(timespec="seconds"),
                "ended": self.ended, "end_reason": self.end_reason,
                "segments": [asdict(s) for s in self.segments]}

    def _write_manifest(self) -> None:
        write_manifest(self.dir, self.manifest())


def read_manifest(rec_dir: Path) -> dict:
    return json.loads((rec_dir / MANIFEST).read_text())


def write_manifest(rec_dir: Path, data: dict) -> None:
    tmp = rec_dir / (MANIFEST + ".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(rec_dir / MANIFEST)


def read_flags(rec_dir: Path) -> list[float]:
    """The flagged offsets in seconds, sorted. A missing or damaged file means no flags:
    recordings from before 0.4 have none."""
    try:
        items = json.loads((rec_dir / FLAGS).read_text())
        out = [float(i["t"]) for i in items
               if isinstance(i, dict) and isinstance(i.get("t"), (int, float))
               and not isinstance(i.get("t"), bool) and i["t"] >= 0]
    except (OSError, ValueError, TypeError):
        return []
    return sorted(out)


def write_flags(rec_dir: Path, flags: list[float]) -> None:
    tmp = rec_dir / (FLAGS + ".tmp")
    tmp.write_text(json.dumps([{"t": t} for t in flags]))
    tmp.replace(rec_dir / FLAGS)


def add_flag(flags: list[float], t: float) -> list[float] | None:
    """flags plus t, sorted; None if t is within FLAG_DEDUPE_S of a flag already there."""
    if any(abs(t - f) < FLAG_DEDUPE_S for f in flags):
        return None
    return sorted([*flags, t])


def recover(root: Path) -> list[Path]:
    """Close recordings left open by a crash: stop orphaned children, fix headers.

    Returns the recordings that were recovered, so the caller can queue them.
    """
    out = []
    if not root.is_dir():
        return out
    for rec_dir in sorted(p for p in root.iterdir() if (p / MANIFEST).is_file()):
        try:
            m = read_manifest(rec_dir)
        except (OSError, ValueError):
            continue
        if m.get("ended"):
            continue
        for seg in m.get("segments", []):
            path = rec_dir / seg["file"]
            _stop_orphan(seg.get("pid"), path)
            if path.exists():
                wav.repair(path)
            if seg.get("end_offset_s") is None:
                seg["end_offset_s"] = round(seg["start_offset_s"] + wav.duration_s(path), 3)
                seg["end_reason"] = "crash"
        mtimes = [(rec_dir / s["file"]).stat().st_mtime for s in m.get("segments", [])
                  if (rec_dir / s["file"]).exists()]
        end = dt.datetime.fromtimestamp(max(mtimes) if mtimes else time.time()).astimezone()
        m["ended"], m["end_reason"] = end.isoformat(timespec="seconds"), "crash"
        write_manifest(rec_dir, m)
        out.append(rec_dir)
    return out


def _stop_orphan(pid: int | None, path: Path) -> None:
    """SIGINT a pw-record that outlived us, but only if it still writes this file."""
    if not pid:
        return
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
    except OSError:
        return
    if str(path).encode() not in cmdline:
        return
    try:
        os.kill(pid, signal.SIGINT)
    except OSError:
        return
    for _ in range(30):
        if not Path(f"/proc/{pid}").exists():
            return
        time.sleep(0.1)
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass
