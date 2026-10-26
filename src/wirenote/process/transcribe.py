# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hrishikesh Jadhav
"""Transcribe each recorded segment with faster-whisper (docs/dev/PLAN.md › Processing, step 1).

CPU, int8; voice-activity filter on, word timestamps on, language auto-detect
unless configured. Samples are decoded here (16 kHz mono s16 from pw-record)
because faster-whisper's own decoder breaks with the installed PyAV.

The vocabulary (names, product terms) goes in as hotwords, which faster-whisper
puts into every window's prompt. On the system track, speakers are separated
(process/speakers.py) and lines are split at speaker changes.
"""

from __future__ import annotations

import sys
from pathlib import Path

from ..capture import wav
from ..config.settings import Settings
from .merge import Line
from .speakers import Diarizer, SpeakerBook, Word, assign_sentence, sentences

MIN_SEGMENT_S = 0.5


def log(msg: str) -> None:
    print(f"worker: {msg}", file=sys.stderr, flush=True)


class Transcriber:
    def __init__(self, settings: Settings, vocabulary: list[str] | None = None) -> None:
        from faster_whisper import WhisperModel

        self.settings = settings
        from .models import whisper_path

        self.model = WhisperModel(whisper_path(settings.whisper_model), device="cpu",
                                  compute_type="int8")
        # Each term as its own sentence: Whisper copies the prompt's style, and a bare
        # comma list made it drop sentence punctuation (docs/dev/features-2-design.md).
        self.hotwords = " ".join(f"{t.rstrip('.')}." for t in vocabulary or [])[:600] or None
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
        lang = None if self.settings.whisper_language == "auto" else self.settings.whisper_language
        segments, info = self.model.transcribe(audio, language=lang, vad_filter=True,
                                               word_timestamps=True, hotwords=self.hotwords)
        segments = [s for s in segments if s.text.strip()]
        run = {"file": path.name, "language": info.language,
               "language_probability": round(info.language_probability, 2),
               "audio_s": round(info.duration, 1), "speech_s": round(info.duration_after_vad, 1)}

        words = [Word(w.start, w.end, w.word) for s in segments for w in (s.words or [])]
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
