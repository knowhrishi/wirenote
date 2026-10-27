# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hrishikesh Jadhav
"""Transcribe each recorded segment (docs/dev/PLAN.md › Processing, step 1).

The speech engine (process/engines.py: by default Parakeet or faster-whisper, whichever
fits; or whisper.cpp) turns the audio into timed words; everything after that is the same.
Samples are decoded here (16 kHz mono s16 from pw-record) because faster-whisper's
own decoder breaks with the installed PyAV.

On the system track, speakers are separated (process/speakers.py) and lines are
split at speaker changes.
"""

from __future__ import annotations

import sys
from pathlib import Path

from ..capture import wav
from ..config.settings import Settings
from .engines import make_engine
from .merge import Line
from .speakers import Diarizer, SpeakerBook, assign_sentence, sentences

MIN_SEGMENT_S = 0.5


def log(msg: str) -> None:
    print(f"worker: {msg}", file=sys.stderr, flush=True)


class Transcriber:
    def __init__(self, settings: Settings, vocabulary: list[str] | None = None) -> None:
        self.settings = settings
        self.engine = make_engine(settings, vocabulary or [])
        self.diarizer = None
        if settings.diarize:
            try:
                self.diarizer = Diarizer(settings)
            except Exception as e:  # noqa: BLE001 - fall back to a single "Others"
                log(f"speaker separation unavailable, using 'Others': {e!r}")

    def segment(self, path: Path, offset_s: float, track: str,
                book: SpeakerBook) -> tuple[list[Line], dict]:
        """Lines of one WAV, shifted by its offset in the recording, plus run info."""
        if wav.duration_s(path) < MIN_SEGMENT_S:
            return [], {"file": path.name, "skipped": "too short"}
        audio = wav.load_float(path)
        heard = self.engine.hear(audio)
        run = {"file": path.name, "engine": self.engine.name, "language": heard.language,
               "language_probability": heard.language_probability,
               "audio_s": round(len(audio) / 16000, 1), "speech_s": heard.speech_s}
        words = heard.words
        text = lambda ws: "".join(w.text for w in ws).strip()  # noqa: E731
        line = lambda who, ws: Line(who, round(offset_s + ws[0].start, 2),  # noqa: E731
                                    round(offset_s + ws[-1].end, 2), text(ws))

        if track == "mic":
            return [line("Me", ws) for ws in sentences(words) if text(ws)], run
        if self.diarizer is None:
            return [line("Others", ws) for ws in sentences(words) if text(ws)], run

        turns, prints = self.diarizer.run(audio)
        labels = {local: book.label_for(v) for local, v in prints.items()}
        for t in turns:
            book.add_time(labels[t.label], t.end - t.start)
        lines = []
        for sent in sentences(words):
            for local, ws in assign_sentence(sent, turns):
                if text(ws):
                    lines.append(line(labels.get(local, "Others"), ws))
        run["speakers"] = len(prints)
        return lines, run

    def recording(self, rec_dir: Path, manifest: dict) -> tuple[list[Line], list[dict], SpeakerBook]:
        book = SpeakerBook(self.settings.voice_match)
        lines, runs = [], []
        for seg in manifest.get("segments", []):
            path = rec_dir / seg["file"]
            if not path.exists():
                runs.append({"file": seg["file"], "skipped": "missing (audio deleted?)"})
                continue
            got, run = self.segment(path, seg["start_offset_s"], seg["track"], book)
            lines += got
            runs.append(run)
        return lines, runs, book
