# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hrishikesh Jadhav
"""Live transcript while a call records (docs/dev/live-design.md).

`wirenote live REC_DIR` runs beside the recording when the setting live_transcript is
on; the daemon starts it at low priority and stops it with the recording. It reads the
growing WAV files, cuts them at pauses and appends what it hears to live.jsonl, which
the call page shows. It is a preview: the note's transcript is still made from the
whole recording afterwards, and live.jsonl is deleted once that is written.

It never downloads a model during a call. It uses Parakeet or Whisper `small` if one is
already downloaded; otherwise the worker downloads one after the next note (`prefetch`).
"""

from __future__ import annotations

import json
import signal
import sys
import time
from dataclasses import dataclass, field, replace
from pathlib import Path

import numpy as np

from ..capture import wav
from ..config.settings import Settings
from .engines import (PARAKEET_LANGUAGES, ParakeetEngine, WhisperEngine, speech_spans,
                      system_language)
from .merge import Line, dedupe_echo
from .speakers import sentences

LIVE = "live.jsonl"
SR = wav.RATE
# docs/dev/PLAN.md › Defaults › Live transcript
STEP_S = 1.5            # how often new audio is read
MIN_PIECE_S = 2.0       # shorter pieces wait for more audio
MAX_PIECE_S = 15.0      # longer ones are cut even without a pause
PAUSE_S = 0.5           # silence after speech that ends a piece
HOLD_ME_S = 10.0        # a mic line waits at most this long for the system track (echo check)
THREADS = 2
LIVE_WHISPER = "small"


def log(msg: str) -> None:
    print(f"live: {msg}", file=sys.stderr, flush=True)


def _uses_parakeet(settings: Settings) -> bool:
    if settings.asr_engine == "parakeet":
        return True
    language = (system_language() if settings.whisper_language == "auto"
                else settings.whisper_language.lower())
    return settings.asr_engine != "whisper" and language in PARAKEET_LANGUAGES


def live_engine(settings: Settings, vocabulary: list[str]):
    """Parakeet where it fits the language, else Whisper `small`; downloaded models only."""
    tries = []
    if _uses_parakeet(settings):
        tries.append(lambda: ParakeetEngine(settings, vocabulary, threads=THREADS,
                                            local_only=True))
    tries.append(lambda: WhisperEngine(replace(settings, whisper_model=LIVE_WHISPER), vocabulary,
                                       threads=THREADS, local_only=True))
    for make in tries:
        try:
            return make()
        except Exception:  # noqa: BLE001 - not downloaded yet; try the next
            continue
    return None


def prefetch(settings: Settings) -> None:
    """Download the live model after a note, so the next call can show a live transcript."""
    from .engines import PARAKEET_FILES, PARAKEET_REPO
    from .models import hub_file, whisper_path

    if _uses_parakeet(settings):
        for name in PARAKEET_FILES:
            hub_file(PARAKEET_REPO, name)
    else:
        whisper_path(LIVE_WHISPER)


@dataclass
class Tail:
    """One track's growing WAV files, read as they are written."""
    track: str
    file: str | None = None
    pos: int = wav.HEADER
    file_start_s: float = 0.0
    buf: np.ndarray = field(default_factory=lambda: np.zeros(0, np.float32))
    buf_start_s: float = 0.0
    done_until_s: float = 0.0          # recording time up to which audio was heard
    flush: bool = False                # a file ended: hear what is left of it

    def read(self, rec_dir: Path, segments: list[dict]) -> None:
        mine = [s for s in segments if s.get("track") == self.track]
        names = [s["file"] for s in mine]
        if not mine:
            return
        if self.file not in names:
            self._open(mine[0])
        i = names.index(self.file)
        try:
            with (rec_dir / self.file).open("rb") as f:
                f.seek(self.pos)
                data = f.read()
        except OSError:
            data = b""
        data = data[:len(data) // 2 * 2]
        if data:
            if not len(self.buf):
                self.buf_start_s = self.file_start_s + (self.pos - wav.HEADER) / 2 / SR
            samples = np.frombuffer(data, dtype="<i2").astype(np.float32) / 32768.0
            self.buf = np.concatenate([self.buf, samples])
            self.pos += len(data)
        if mine[i].get("end_offset_s") is not None and i + 1 < len(mine):
            # Everything of a closed file was read; the next file may start after a gap,
            # so what is left is heard on its own.
            self.flush = True
            self._open(mine[i + 1])

    def _open(self, seg: dict) -> None:
        self.file, self.pos = seg["file"], wav.HEADER
        self.file_start_s = float(seg.get("start_offset_s") or 0.0)

    def piece(self, spans_of=None) -> tuple[float, np.ndarray] | None:
        """The next piece to hear, cut at a pause: (recording time, samples), or None."""
        n = len(self.buf)
        if not n or (n < MIN_PIECE_S * SR and not self.flush):
            return None
        spans = (spans_of or speech_spans)(self.buf)
        if not spans:
            keep = 0 if self.flush else int(PAUSE_S * SR)     # silence: drop it
            self._take(n - keep)
            self.flush = False
            return None
        last_start, last_end = spans[-1]
        if self.flush:
            cut = n
        elif (n - last_end) >= PAUSE_S * SR:
            cut = last_end
        elif n >= MAX_PIECE_S * SR:
            # Mid-sentence: cut before the last bit of speech unless that is all there is.
            cut = last_start if last_start >= MIN_PIECE_S * SR else n
        else:
            return None
        start = self.buf_start_s
        piece = self._take(cut)
        self.flush = False
        return start, piece

    def _take(self, n: int) -> np.ndarray:
        piece, self.buf = self.buf[:n], self.buf[n:]
        self.buf_start_s += n / SR
        self.done_until_s = self.buf_start_s
        return piece


class Live:
    def __init__(self, rec_dir: Path, settings: Settings, engine,
                 clock=time.monotonic) -> None:
        self.dir = rec_dir
        self.settings = settings
        self.engine = engine
        self.clock = clock
        self.tails = {"system": Tail("system"), "mic": Tail("mic")}
        self.held: list[tuple[float, Line]] = []      # mic lines waiting for the system track
        self.recent: list[Line] = []                  # system lines, for the echo check
        self.out = (rec_dir / LIVE).open("a", encoding="utf-8")

    def write(self, obj: dict) -> None:
        self.out.write(json.dumps(obj, ensure_ascii=False) + "\n")
        self.out.flush()

    def step(self) -> None:
        segments = json.loads((self.dir / "manifest.json").read_text()).get("segments", [])
        for name, tail in self.tails.items():       # system first: mic lines wait for it
            tail.read(self.dir, segments)
            while (got := tail.piece()) is not None:
                start, audio = got
                for line in self._hear(audio, start, "Me" if name == "mic" else "Others"):
                    if name == "mic":
                        self.held.append((self.clock(), line))
                    else:
                        self.recent.append(line)
                        self.write(line.as_dict())
        self._release()

    def _hear(self, audio: np.ndarray, start_s: float, speaker: str) -> list[Line]:
        words = self.engine.hear(audio).words
        out = []
        for ws in sentences(words):
            text = "".join(w.text for w in ws).strip()
            if text:
                out.append(Line(speaker, round(start_s + ws[0].start, 2),
                                round(start_s + ws[-1].end, 2), text))
        return out

    def _release(self) -> None:
        """Show a mic line once the system track was heard past it, unless it is the far
        side's voice coming back through the mic (merge.dedupe_echo)."""
        heard = self.tails["system"].done_until_s
        now = self.clock()
        self.recent = [l for l in self.recent if l.end > heard - 60]
        waiting = []
        for since, line in self.held:
            if line.end > heard and now - since < HOLD_ME_S:
                waiting.append((since, line))
                continue
            kept, _ = dedupe_echo([line, *self.recent], self.settings.echo_overlap,
                                  self.settings.echo_similarity)
            if line in kept:
                self.write(line.as_dict())
        self.held = waiting

    def close(self) -> None:
        self.out.close()


def run(rec_dir: Path, settings: Settings, vocabulary: list[str]) -> int:
    stopping = []
    signal.signal(signal.SIGTERM, lambda *_: stopping.append(1))
    out = (rec_dir / LIVE).open("a", encoding="utf-8")
    engine = live_engine(settings, vocabulary)
    if engine is None:
        out.write(json.dumps({"status": "unavailable",
                              "reason": "the live model downloads after your next note"}) + "\n")
        out.close()
        log("no live model downloaded yet")
        return 0
    out.write(json.dumps({"status": "listening", "engine": engine.label}) + "\n")
    out.close()
    live = Live(rec_dir, settings, engine)
    log(f"listening with {engine.label}")
    try:
        while not stopping:
            started = time.monotonic()
            live.step()
            if json.loads((rec_dir / "manifest.json").read_text()).get("ended"):
                break
            time.sleep(max(0.0, STEP_S - (time.monotonic() - started)))
    finally:
        live.close()
    return 0
