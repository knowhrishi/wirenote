# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hrishikesh Jadhav
"""Recorder: segments, heartbeat, restarts, stop, and crash recovery, with fake pw-record."""

from __future__ import annotations

import asyncio
import json
import signal
import struct
import wave
from pathlib import Path

from wirenote.capture import wav
from wirenote.capture.recorder import Recording, RecordingMeta, read_flags, read_manifest, recover


def pw_header(data_size: int = 0) -> bytes:
    """What pw-record writes: 16 kHz mono s16; sizes stay 8/0 until SIGINT."""
    riff = 8 if data_size == 0 else data_size + 36
    return (b"RIFF" + struct.pack("<I", riff) + b"WAVEfmt " +
            struct.pack("<IHHIIHH", 16, 1, 1, 16000, 32000, 2, 16) +
            b"data" + struct.pack("<I", data_size))


class FakeChild:
    """A pw-record child: writes the header on start, fixes it on SIGINT."""
    next_pid = 1000

    def __init__(self, argv: list[str]) -> None:
        FakeChild.next_pid += 1
        self.pid = FakeChild.next_pid
        self.argv = argv
        self.path = Path(argv[-1])
        self.path.write_bytes(pw_header())
        self.returncode = None
        self.signals = []

    def grow(self, n: int = 3200) -> None:
        with self.path.open("ab") as f:
            f.write(b"\x01\x00" * (n // 2))

    def send_signal(self, sig) -> None:
        self.signals.append(sig)
        if sig == signal.SIGINT:
            size = self.path.stat().st_size - 44
            data = self.path.read_bytes()
            self.path.write_bytes(pw_header(size) + data[44:])
            self.returncode = 1          # pw-record exits 1 on SIGINT

    def kill(self) -> None:
        self.returncode = -9

    async def wait(self) -> int:
        return self.returncode


class Harness:
    def __init__(self, tmp_path: Path) -> None:
        self.now = 100.0
        self.children: list[FakeChild] = []
        self.rec = Recording(tmp_path, RecordingMeta("chrome", "Chrome", "Jitsi", "Room"),
                             spawn=self.spawn, clock=lambda: self.now)

    async def spawn(self, argv):
        child = FakeChild(argv)
        self.children.append(child)
        return child

    def child(self, track: str) -> FakeChild:
        return [c for c in self.children if f"/{track}-" in str(c.path)][-1]


def run(coro):
    return asyncio.run(coro)


def test_start_runs_two_tagged_tracks(tmp_path):
    h = Harness(tmp_path)
    run(h.rec.start("alsa_input.headset"))
    mic, system = h.child("mic"), h.child("system")
    assert mic.argv[mic.argv.index("--target") + 1] == "alsa_input.headset"
    assert "--target" not in system.argv                       # follows the default sink
    assert "node.name=wirenote-mic" in " ".join(mic.argv)
    assert "stream.capture.sink=true" in " ".join(system.argv)
    assert ["--rate", "16000"] == mic.argv[1:3]
    m = read_manifest(h.rec.dir)
    assert [s["file"] for s in m["segments"]] == ["mic-000.wav", "system-000.wav"]
    assert m["ended"] is None and m["service"] == "Jitsi"


def test_heartbeat_reports_a_stalled_track_once_and_its_recovery(tmp_path):
    h = Harness(tmp_path)
    run(h.rec.start(None))
    for _ in range(4):                       # system keeps growing, mic does not
        h.now += 1.5
        h.child("system").grow()
        events = run(h.rec.heartbeat())
    assert [(e.track, e.kind) for e in events] == [("mic", "stalled")]
    h.now += 1
    assert run(h.rec.heartbeat()) == []      # reported once
    h.child("mic").grow()
    h.now += 1
    assert [(e.track, e.kind) for e in run(h.rec.heartbeat())] == [("mic", "recovered")]


def test_dead_child_restarts_as_new_segment(tmp_path):
    h = Harness(tmp_path)
    run(h.rec.start(None))
    h.child("system").grow()
    h.child("system").returncode = 1
    h.now += 3
    events = run(h.rec.heartbeat())
    assert [(e.track, e.kind) for e in events] == [("system", "restarted")]
    segs = read_manifest(h.rec.dir)["segments"]
    assert [s["file"] for s in segs] == ["mic-000.wav", "system-000.wav", "system-001.wav"]
    assert segs[1]["end_reason"] == "pw-record exited with 1" and segs[2]["start_offset_s"] == 3.0


def test_mic_retarget_starts_new_segment(tmp_path):
    h = Harness(tmp_path)
    run(h.rec.start(None))
    assert run(h.rec.retarget_mic(None)) is False
    h.now += 10
    assert run(h.rec.retarget_mic("alsa_input.usb")) is True
    assert signal.SIGINT in h.children[0].signals
    segs = read_manifest(h.rec.dir)["segments"]
    assert segs[-1]["file"] == "mic-001.wav" and segs[-1]["target"] == "alsa_input.usb"


def test_stop_closes_files_cleanly(tmp_path):
    h = Harness(tmp_path)
    run(h.rec.start(None))
    for c in h.children:
        c.grow(32000)
    h.now += 60
    run(h.rec.stop("app left the mic"))
    m = read_manifest(h.rec.dir)
    assert m["end_reason"] == "app left the mic" and m["ended"]
    assert all(s["end_offset_s"] == 60.0 for s in m["segments"])
    with wave.open(str(h.rec.dir / "mic-000.wav")) as w:
        assert w.getnframes() == 16000


def test_repair_fixes_kill9_header(tmp_path):
    p = tmp_path / "x.wav"
    p.write_bytes(pw_header() + b"\x00\x01" * 8000)
    assert wav.repair(p) is True
    with wave.open(str(p)) as w:
        assert (w.getframerate(), w.getnframes()) == (16000, 8000)
    assert wav.repair(p) is False


def test_recover_closes_a_recording_left_open(tmp_path):
    h = Harness(tmp_path)
    run(h.rec.start(None))
    for c in h.children:
        c.grow(16000)                # 0.5 s each; daemon dies here (kill -9)
    assert recover(tmp_path) == [h.rec.dir]
    m = read_manifest(h.rec.dir)
    assert m["end_reason"] == "crash"
    assert [s["end_offset_s"] for s in m["segments"]] == [0.5, 0.5]
    assert wav.duration_s(h.rec.dir / "mic-000.wav") == 0.5
    assert recover(tmp_path) == []   # already closed


def test_recover_ignores_unrelated_dirs(tmp_path):
    (tmp_path / "junk").mkdir()
    (tmp_path / "bad").mkdir()
    (tmp_path / "bad" / "manifest.json").write_text("{")
    done = tmp_path / "done"
    done.mkdir()
    (done / "manifest.json").write_text(json.dumps({"ended": "x", "segments": []}))
    assert recover(tmp_path) == []


def test_flag_offsets_use_the_transcript_clock_and_dedupe(tmp_path):
    h = Harness(tmp_path)
    run(h.rec.start(None))
    h.now = h.rec.t0 + 65
    assert h.rec.flag() == 65.0
    h.now += 1
    assert h.rec.flag() is None                     # within 2 s of the last one
    assert read_flags(h.rec.dir) == [65.0]
    h.now += 9
    assert h.rec.flag() == 75.0
    assert read_flags(h.rec.dir) == [65.0, 75.0]
    assert not list(h.rec.dir.glob("*.tmp"))        # written atomically


def test_read_flags_ignores_a_missing_or_broken_file(tmp_path):
    assert read_flags(tmp_path) == []
    (tmp_path / "flags.json").write_text("not json")
    assert read_flags(tmp_path) == []
    (tmp_path / "flags.json").write_text('[{"t": 3.5}, {"x": 1}, {"t": "no"}, {"t": -1}]')
    assert read_flags(tmp_path) == [3.5]
