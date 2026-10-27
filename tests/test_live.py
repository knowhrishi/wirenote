# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hrishikesh Jadhav
"""Live transcript: reading growing WAVs, cutting at pauses, echo, the daemon and the app."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

from wirenote.capture.recorder import write_manifest
from wirenote.config.settings import Settings
from wirenote.process import live as live_mod
from wirenote.process.engines import Heard
from wirenote.process.live import LIVE, Live, Tail
from wirenote.process.speakers import Word

from test_capture import pw_header
from test_daemon import _record_and_stop, make

SR = 16000


def loud_spans(audio):
    """Stands in for the Silero VAD: speech is where the signal is loud."""
    on = np.abs(audio) > 0.1
    spans, start = [], None
    for i in range(0, len(audio), 160):               # 10 ms steps
        if on[i:i + 160].any():
            start = i if start is None else start
        elif start is not None:
            spans.append((start, i))
            start = None
    if start is not None:
        spans.append((start, len(audio)))
    return spans


@pytest.fixture(autouse=True)
def fake_vad(monkeypatch):
    monkeypatch.setattr(live_mod, "speech_spans", loud_spans)


def tone(seconds: float, level: float = 0.5) -> np.ndarray:
    t = np.arange(int(seconds * SR)) / SR
    return (level * np.sin(2 * np.pi * 220 * t)).astype(np.float32)


def silence(seconds: float) -> np.ndarray:
    return np.zeros(int(seconds * SR), np.float32)


def append(path: Path, audio: np.ndarray) -> None:
    if not path.exists():
        path.write_bytes(pw_header())
    with path.open("ab") as f:
        f.write((audio * 32767).astype("<i2").tobytes())


class SaysWhatItHears:
    """A fake engine: one word per 0.5 s of loud audio, from the tone's level."""
    label = "fake"

    def __init__(self, words_by_level: dict[float, list[str]]) -> None:
        self.words_by_level = words_by_level
        self.pieces: list[float] = []

    def hear(self, audio):
        self.pieces.append(round(len(audio) / SR, 2))
        level = round(float(np.abs(audio).max()), 1)
        spans = loud_spans(audio)
        if not spans:
            return Heard([], None, None, 0.0)
        s0 = spans[0][0] / SR
        words = self.words_by_level.get(level, ["something."])
        return Heard([Word(s0 + i * 0.5, s0 + i * 0.5 + 0.4, " " + w) for i, w in enumerate(words)],
                     None, None, 0.0)


def make_rec(tmp_path: Path, segments: list[dict]) -> Path:
    rec = tmp_path / "20261027-100000-chrome"
    rec.mkdir()
    write_manifest(rec, {"version": 1, "id": rec.name, "segments": segments})
    return rec


def seg(track, file, start=0.0, end=None):
    return {"track": track, "file": file, "start_offset_s": start, "end_offset_s": end}


def lines(rec: Path) -> list[dict]:
    return [json.loads(l) for l in (rec / LIVE).read_text().splitlines()]


def test_a_piece_ends_at_a_pause_not_while_someone_talks(tmp_path):
    rec = make_rec(tmp_path, [seg("system", "system-000.wav")])
    wav = rec / "system-000.wav"
    tail = Tail("system")
    append(wav, tone(1.5))
    tail.read(rec, [seg("system", "system-000.wav")])
    assert tail.piece(loud_spans) is None                 # too short, still talking
    append(wav, tone(1.0))
    tail.read(rec, [seg("system", "system-000.wav")])
    assert tail.piece(loud_spans) is None                 # 2.5 s, but no pause yet
    append(wav, silence(0.8))
    tail.read(rec, [seg("system", "system-000.wav")])
    start, piece = tail.piece(loud_spans)
    assert start == 0.0 and abs(len(piece) / SR - 2.5) < 0.02
    assert tail.done_until_s == pytest.approx(len(piece) / SR)


def test_long_talk_is_cut_and_silence_is_dropped(tmp_path):
    rec = make_rec(tmp_path, [seg("mic", "mic-000.wav")])
    tail = Tail("mic")
    append(rec / "mic-000.wav", silence(5.0))
    tail.read(rec, [seg("mic", "mic-000.wav")])
    assert tail.piece(loud_spans) is None
    assert tail.done_until_s == pytest.approx(4.5)        # silence goes, a little is kept
    append(rec / "mic-000.wav", tone(16.0))
    tail.read(rec, [seg("mic", "mic-000.wav")])
    start, piece = tail.piece(loud_spans)
    assert start == pytest.approx(4.5) and len(piece) / SR == pytest.approx(16.5)


def test_a_new_file_flushes_the_old_one_and_keeps_recording_time(tmp_path):
    segs = [seg("mic", "mic-000.wav", 0.0, 3.0), seg("mic", "mic-001.wav", 4.0)]
    rec = make_rec(tmp_path, segs)
    append(rec / "mic-000.wav", tone(1.0))                 # short, and then the mic moved
    append(rec / "mic-001.wav", silence(0.5))
    append(rec / "mic-001.wav", tone(2.0))
    append(rec / "mic-001.wav", silence(1.0))
    tail = Tail("mic")
    tail.read(rec, segs)
    first = tail.piece(loud_spans)
    assert first[0] == 0.0 and len(first[1]) == SR        # heard although only 1 s
    tail.read(rec, segs)
    start, piece = tail.piece(loud_spans)
    assert start == pytest.approx(4.0)                     # the second file's own offset


def test_live_writes_lines_and_drops_the_echo_of_the_far_side(tmp_path):
    segs = [seg("mic", "mic-000.wav"), seg("system", "system-000.wav")]
    rec = make_rec(tmp_path, segs)
    # The far side says something; it leaks into the mic, quieter, at the same time.
    append(rec / "system-000.wav", np.concatenate([tone(2.5, 0.5), silence(1.0)]))
    append(rec / "mic-000.wav", np.concatenate([tone(2.5, 0.2), silence(1.0)]))
    engine = SaysWhatItHears({0.5: ["We", "ship", "Friday."], 0.2: ["We", "ship", "Friday."],
                              0.3: ["I", "send", "the", "changelog."]})
    live = Live(rec, Settings(), engine, clock=lambda: 0.0)
    live.step()
    # Then I speak on my own.
    append(rec / "mic-000.wav", np.concatenate([tone(2.5, 0.3), silence(1.0)]))
    append(rec / "system-000.wav", silence(3.5))
    live.step()
    live.close()
    assert [(l["speaker"], l["text"]) for l in lines(rec)] == [
        ("Others", "We ship Friday."), ("Me", "I send the changelog.")]
    assert lines(rec)[1]["start"] == pytest.approx(3.5)


def test_a_mic_line_waits_for_the_system_track_but_not_forever(tmp_path):
    segs = [seg("mic", "mic-000.wav"), seg("system", "system-000.wav")]
    rec = make_rec(tmp_path, segs)
    append(rec / "mic-000.wav", np.concatenate([tone(2.5, 0.3), silence(1.0)]))
    append(rec / "system-000.wav", silence(0.1))           # the system track lags behind
    now = [0.0]
    live = Live(rec, Settings(), SaysWhatItHears({0.3: ["Hello."]}), clock=lambda: now[0])
    live.step()
    assert lines(rec) == []
    now[0] = live_mod.HOLD_ME_S + 1
    live.step()
    live.close()
    assert [l["text"] for l in lines(rec)] == ["Hello."]


def test_without_a_downloaded_model_it_says_so_and_exits(tmp_path, monkeypatch):
    rec = make_rec(tmp_path, [])
    monkeypatch.setattr(live_mod, "live_engine", lambda s, v: None)
    assert live_mod.run(rec, Settings(), []) == 0
    assert lines(rec)[0]["status"] == "unavailable"


def test_live_engine_prefers_parakeet_where_it_fits_and_never_downloads(monkeypatch):
    made = []

    def parakeet(settings, vocabulary, threads=None, local_only=False):
        made.append(("parakeet", threads, local_only))
        raise FileNotFoundError("not downloaded")

    def whisper(settings, vocabulary, threads=None, local_only=False):
        made.append(("whisper", settings.whisper_model, threads, local_only))
        return "whisper-engine"
    monkeypatch.setattr(live_mod, "ParakeetEngine", parakeet)
    monkeypatch.setattr(live_mod, "WhisperEngine", whisper)
    monkeypatch.setenv("LANG", "de_DE.UTF-8")
    monkeypatch.delenv("LC_ALL", raising=False)
    assert live_mod.live_engine(Settings(), []) == "whisper-engine"
    assert made == [("parakeet", 2, True), ("whisper", "small", 2, True)]
    made.clear()
    assert live_mod.live_engine(replace(Settings(), asr_engine="whisper"), []) == "whisper-engine"
    assert made[0][0] == "whisper"


def test_daemon_runs_the_live_transcript_only_when_turned_on(tmp_path):
    started = []

    class Proc:
        returncode = None

        def terminate(self):
            started.append("terminated")
            self.returncode = 0

        async def wait(self):
            return self.returncode

    async def spawn_live(rec_dir):
        started.append(rec_dir.name)
        return Proc()

    d = make(tmp_path)
    d.spawn_live = spawn_live
    _record_and_stop(d)
    assert started == []
    d = make(tmp_path / "on")
    d.settings = replace(d.settings, live_transcript=True)
    d.spawn_live = spawn_live
    rec_id = _record_and_stop(d)
    assert started == [rec_id, "terminated"]


def test_the_app_reads_only_new_whole_lines(tmp_path):
    from wirenote.ui.data import Invalid, Store

    from test_process import make_recording, no_voices, settings_for

    rec = make_recording(tmp_path)
    s = replace(settings_for(tmp_path), recordings_dir=str(tmp_path / "recordings"),
                live_transcript=True)
    st = Store(s, tmp_path / "state", voices=no_voices(tmp_path))
    assert st.live(rec.name) == {"enabled": True, "status": None, "lines": [], "after": 0}
    first = '{"status": "listening", "engine": "parakeet-tdt-0.6b-v3"}\n' \
            '{"speaker": "Others", "start": 1.0, "end": 2.0, "text": "Hallo."}\n'
    (rec / LIVE).write_text(first + '{"speaker": "Me", "sta')
    r = st.live(rec.name)
    assert r["status"]["engine"] == "parakeet-tdt-0.6b-v3"
    assert [l["text"] for l in r["lines"]] == ["Hallo."] and r["after"] == len(first)
    with (rec / LIVE).open("a") as f:
        f.write('rt": 2.5, "end": 3.0, "text": "Hi."}\n')
    r = st.live(rec.name, r["after"])
    assert [l["text"] for l in r["lines"]] == ["Hi."] and r["status"] is None
    assert st.live(rec.name, 10 ** 9)["lines"][0]["text"] == "Hallo."    # starts over
    st.update_settings({"live_transcript": False}, confirm_cloud=False)
    assert st.live(rec.name)["enabled"] is False
    with pytest.raises(Invalid, match="true or false"):
        st.update_settings({"live_transcript": "yes"}, confirm_cloud=False)


def test_worker_replaces_the_preview_and_fetches_the_live_model(tmp_path, monkeypatch):
    from wirenote.process import worker
    from wirenote.process.jobs import Jobs

    from test_process import FakeBackend, FakeTranscriber, make_recording, no_voices, settings_for

    fetched = []
    monkeypatch.setattr(worker, "prefetch", lambda s: fetched.append(s.live_transcript))
    rec = make_recording(tmp_path)
    (rec / LIVE).write_text('{"speaker": "Me", "start": 0, "end": 1, "text": "x"}\n')
    jobs = Jobs(tmp_path / "jobs.db")
    s = replace(settings_for(tmp_path), live_transcript=True)
    worker.run_job(jobs.add(rec), s, jobs, FakeTranscriber(), lambda _s: FakeBackend(),
                   voices=no_voices(tmp_path))
    assert not (rec / LIVE).exists() and fetched == [True]


def test_live_transcript_is_off_by_default():
    assert Settings().live_transcript is False
