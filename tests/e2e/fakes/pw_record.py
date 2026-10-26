# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hrishikesh Jadhav
"""Stand-in for `pw-record --rate 16000 --channels 1 --format s16 -P {...} FILE`.

Writes what pw-record writes: a 44-byte PCM header whose sizes are 0 until a clean
stop, then samples in real time, so the recorder's heartbeat sees the file grow.
SIGINT or SIGTERM fixes the header and exits 0, as pw-record does.

The track comes from node.name in -P. Each track plays a tone of its own loudness
(the fake Whisper model tells them apart by it), or, if WIRENOTE_E2E_MIC_WAV or
WIRENOTE_E2E_SYSTEM_WAV names a 16 kHz mono s16 WAV, that speech followed by quiet.
"""

from __future__ import annotations

import math
import os
import signal
import struct
import sys
import time
import wave

RATE = 16000
CHUNK = RATE // 10                       # 100 ms
AMPLITUDE = {"mic": 3000, "system": 9000}


def header(data_bytes: int) -> bytes:
    return (b"RIFF" + struct.pack("<I", 36 + data_bytes if data_bytes else 0) + b"WAVE"
            + b"fmt " + struct.pack("<IHHIIHH", 16, 1, 1, RATE, RATE * 2, 2, 16)
            + b"data" + struct.pack("<I", data_bytes))


def tone(amplitude: int, start: int, n: int) -> bytes:
    return struct.pack(f"<{n}h", *(int(amplitude * math.sin(2 * math.pi * 440 * (start + i) / RATE))
                                   for i in range(n)))


def main(argv: list[str]) -> None:
    path = argv[-1]
    props = argv[argv.index("-P") + 1] if "-P" in argv else ""
    track = "mic" if "wirenote-mic" in props else "system"
    speech = b""
    src = os.environ.get(f"WIRENOTE_E2E_{track.upper()}_WAV")
    if src:
        with wave.open(src, "rb") as w:
            speech = w.readframes(w.getnframes())

    f = open(path, "wb")
    f.write(header(0))
    f.flush()
    written = 0

    def finish(*_):
        f.seek(0)
        f.write(header(written))
        f.close()
        sys.exit(0)

    signal.signal(signal.SIGINT, finish)
    signal.signal(signal.SIGTERM, finish)
    parent = os.getppid()
    t0 = time.monotonic()
    n = 0
    while True:
        if speech and written < len(speech):
            chunk = speech[written:written + CHUNK * 2]
            chunk += tone(30, n, (CHUNK * 2 - len(chunk)) // 2)
        else:
            chunk = tone(30 if speech else AMPLITUDE[track], n, CHUNK)
        f.write(chunk)
        f.flush()
        written += len(chunk)
        n += CHUNK
        if os.getppid() != parent:       # the daemon died without signalling us
            finish()
        time.sleep(max(0.0, t0 + n / RATE - time.monotonic()))


if __name__ == "__main__":
    main(sys.argv[1:])
