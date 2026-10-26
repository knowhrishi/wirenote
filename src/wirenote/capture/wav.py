# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hrishikesh Jadhav
"""WAV helpers for pw-record output: header repair and sample loading.

pw-record writes a 44-byte PCM header and fixes the sizes on SIGINT. After a
kill -9 the header still says RIFF size 8 and data size 0 while the samples
are intact (checked 2026-10-26, PipeWire 1.6.8), so repair rewrites the two
size fields from the file length.
"""

from __future__ import annotations

import struct
import wave
from pathlib import Path

HEADER = 44
RATE = 16000   # docs/dev/PLAN.md › Defaults › Audio format: 16 kHz mono 16-bit


def repair(path: Path) -> bool:
    """Fix the RIFF and data sizes of a truncated WAV. Returns True if changed."""
    size = path.stat().st_size
    if size < HEADER:
        return False
    with path.open("r+b") as f:
        head = f.read(HEADER)
        if head[:4] != b"RIFF" or head[8:12] != b"WAVE" or head[36:40] != b"data":
            return False
        riff, data = struct.unpack_from("<I", head, 4)[0], struct.unpack_from("<I", head, 40)[0]
        want_data = (size - HEADER) // 2 * 2
        if riff == want_data + 36 and data == want_data:
            return False
        f.seek(4)
        f.write(struct.pack("<I", want_data + 36))
        f.seek(40)
        f.write(struct.pack("<I", want_data))
    return True


def duration_s(path: Path) -> float:
    try:
        with wave.open(str(path)) as w:
            return w.getnframes() / w.getframerate()
    except (OSError, wave.Error, EOFError):
        return 0.0


def load_float(path: Path):
    """Samples as float32 in [-1, 1], for faster-whisper (16 kHz mono expected)."""
    import numpy as np

    with wave.open(str(path)) as w:
        if (w.getframerate(), w.getnchannels(), w.getsampwidth()) != (RATE, 1, 2):
            raise ValueError(f"{path.name}: expected 16 kHz mono s16, got "
                             f"{w.getframerate()} Hz, {w.getnchannels()} ch, {w.getsampwidth() * 8} bit")
        raw = w.readframes(w.getnframes())
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
