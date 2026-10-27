# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hrishikesh Jadhav
"""End to end with the real speech models: Whisper `tiny.en`, Parakeet, and speaker separation.

The everyday end-to-end test fakes the model, so it cannot notice when a new
faster-whisper, CTranslate2, onnxruntime or sherpa-onnx (Dependabot bumps them
weekly) breaks transcription. This one can. It downloads about 100 MB of models,
and Parakeet another 670 MB, so it runs only with `pytest -m models`: nightly, and
before every release.
Speech comes from espeak-ng, so no recording of a real person is needed.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import wave
from pathlib import Path

import numpy as np
import pytest

from harness import World
from test_call_to_note import note_done

pytestmark = [pytest.mark.e2e, pytest.mark.models,
              pytest.mark.skipif(not sys.platform.startswith("linux"),
                                 reason="wirenote runs on Linux only"),
              pytest.mark.skipif(shutil.which("espeak-ng") is None, reason="needs espeak-ng")]


def speech(text: str, path: Path, voice: str) -> float:
    """Speak text into a 16 kHz mono s16 WAV, as pw-record would record it; returns seconds."""
    raw = path.with_suffix(".raw.wav")
    subprocess.run(["espeak-ng", "-v", voice, "-s", "140", "-w", str(raw), text], check=True)
    with wave.open(str(raw), "rb") as w:
        rate = w.getframerate()
        samples = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float64)
    n = int(len(samples) * 16000 / rate)
    resampled = np.interp(np.linspace(0, len(samples) - 1, n), np.arange(len(samples)), samples)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(resampled.astype("<i2").tobytes())
    return n / 16000


def talk_and_note(tmp_path: Path, settings: dict | None = None) -> tuple[str, str]:
    """One short spoken call; returns what the note says Me and the others said."""
    mic = tmp_path / "mic.wav"
    system = tmp_path / "system.wav"
    seconds = max(speech("I will send the changelog by Thursday.", mic, "en-us"),
                  speech("Great. Then we ship the release on Friday.", system, "en-gb"))
    world = World(tmp_path / "world", models=True, mic_wav=mic, system_wav=system,
                  settings=settings)
    try:
        world.start_daemon()
        world.wait_for(lambda: "prompt" in world.event_names(), 30, "the Record prompt")
        assert world.cli("record").returncode == 0
        need = 44 + int((seconds + 2) * 16000 * 2)
        world.wait_for(lambda: min(world.wav_bytes("mic"), world.wav_bytes("system")) > need,
                       60, "the speech on both tracks")
        world.hangup()
        note_done(world, timeout=900)    # includes the model download on a cold cache
        (_, note), = world.jobs()
        lines = Path(note).read_text().splitlines()
        me = " ".join(line for line in lines if line.startswith("**Me**")).lower()
        others = " ".join(line for line in lines
                          if line.startswith("**") and not line.startswith("**Me**")).lower()
        assert world.stop_daemon() == 0
        return me, others
    finally:
        world.close()


def test_real_models_turn_speech_into_a_note(tmp_path):
    me, others = talk_and_note(tmp_path)
    # tiny.en mishears espeak's "changelog"; the weekdays come through reliably.
    assert "thursday" in me and "friday" not in me, me
    assert "friday" in others and "thursday" not in others, others


def test_parakeet_turns_speech_into_a_note(tmp_path):
    me, others = talk_and_note(tmp_path, {"asr_engine": "parakeet"})
    assert "thursday" in me and "friday" not in me, me
    assert "friday" in others and "thursday" not in others, others
