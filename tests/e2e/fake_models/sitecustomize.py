# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hrishikesh Jadhav
"""Loaded by every Python process the end-to-end test starts (it is on PYTHONPATH).

Swaps faster-whisper's model for a fake, so the real worker runs without a 2 GB
download. The fake knows the two tones the fake pw-record plays: the quiet one is
the mic, the loud one the system audio. Everything around the model (decoding the
WAVs, sentences, merging, the note) is the real code.
"""

from __future__ import annotations

from types import SimpleNamespace

LINES = {"mic": "I will send the changelog by Thursday.",
         "system": "Great, then we ship the release on Friday."}


class FakeWhisperModel:
    def __init__(self, path, device="cpu", compute_type="int8", **_kw) -> None:
        self.path = path

    def transcribe(self, audio, language=None, vad_filter=True, word_timestamps=True,
                   hotwords=None, **_kw):
        duration = len(audio) / 16000
        rms = float((audio.astype("float64") ** 2).mean() ** 0.5) if len(audio) else 0.0
        text = LINES["system"] if rms > 0.12 else LINES["mic"]
        words = text.split()
        span = min(2.5, duration) / len(words)
        ws = [SimpleNamespace(start=0.2 + i * span, end=0.2 + (i + 1) * span, word=f" {w}")
              for i, w in enumerate(words)]
        seg = SimpleNamespace(start=ws[0].start, end=ws[-1].end, text=" " + text, words=ws)
        info = SimpleNamespace(language="en", language_probability=0.99, duration=duration,
                               duration_after_vad=min(2.5, duration))
        return iter([seg]), info


def _install() -> None:
    try:
        import faster_whisper
    except ImportError:
        return
    faster_whisper.WhisperModel = FakeWhisperModel


_install()
