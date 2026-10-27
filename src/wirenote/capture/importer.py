# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hrishikesh Jadhav
"""Import an audio or video file as a recording (docs/dev/import-export-design.md).

ffmpeg converts it to the format pw-record writes (16 kHz mono s16 WAV). The whole file
becomes the system track, so every voice in it is told apart as Speaker 1, 2, ... and
none is taken for "Me". The original file is never changed or kept.
"""

from __future__ import annotations

import datetime as dt
import shutil
import subprocess
from pathlib import Path

from . import wav
from .recorder import write_manifest

APP = "import"
# docs/dev/PLAN.md › Defaults › Import
MAX_IMPORT_BYTES = 4 * 1024 ** 3
MAX_IMPORT_S = 4 * 3600.0          # the recording cap
CONVERT_TIMEOUT_S = 1800


class ImportFailed(Exception):
    pass


def _free_id(root: Path, started: dt.datetime) -> str:
    base = f"{started:%Y%m%d-%H%M%S}-{APP}"
    for n in range(1, 100):
        rec_id = base if n == 1 else f"{base}-{n}"
        if not (root / rec_id).exists():
            return rec_id
    raise ImportFailed("too many imports with the same start time")


def import_audio(src: Path, root: Path, *, name: str | None = None, title: str | None = None,
                 started: dt.datetime | None = None) -> Path:
    """Convert src into a new recording folder under root; returns the folder.

    started defaults to the file's modification time, which for a recorder's file is
    about when the recording ended; it only orders the call among the others."""
    if not src.is_file():
        raise ImportFailed(f"{src.name}: no such file")
    if src.stat().st_size > MAX_IMPORT_BYTES:
        raise ImportFailed(f"{src.name}: larger than {MAX_IMPORT_BYTES // 1024 ** 3} GB")
    if shutil.which("ffmpeg") is None:
        raise ImportFailed("ffmpeg is needed to import audio: sudo apt install ffmpeg")
    started = (started or dt.datetime.fromtimestamp(src.stat().st_mtime)).astimezone()
    started = started.replace(microsecond=0)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    rec = root / _free_id(root, started)
    rec.mkdir(mode=0o700)
    out = rec / "system-000.wav"
    tmp = rec / "system-000.wav.part"
    try:
        r = subprocess.run(
            ["ffmpeg", "-nostdin", "-v", "error", "-i", str(src), "-vn", "-map_metadata", "-1",
             "-ac", "1", "-ar", str(wav.RATE), "-c:a", "pcm_s16le", "-t", str(MAX_IMPORT_S),
             "-bitexact", "-f", "wav", str(tmp)],
            capture_output=True, text=True, timeout=CONVERT_TIMEOUT_S, check=False)
        if r.returncode != 0:
            # ffmpeg's last line names the problem; it may carry the path, so not more.
            why = (r.stderr.strip().splitlines() or ["unknown error"])[-1]
            raise ImportFailed(f"{src.name}: not an audio or video file ffmpeg can read "
                               f"({why.replace(str(src), src.name)[:160]})")
        tmp.replace(out)
        seconds = round(wav.duration_s(out), 3)
        if seconds < 1.0:
            raise ImportFailed(f"{src.name}: no audio in it")
    except BaseException:
        shutil.rmtree(rec, ignore_errors=True)
        raise
    out.chmod(0o600)
    m = {"version": 1, "id": rec.name, "app": APP,
         "name": (name or src.stem).strip()[:80] or "Imported recording",
         "service": None, "room": None, "calendar": None,
         "started": started.isoformat(timespec="seconds"),
         "ended": (started + dt.timedelta(seconds=seconds)).isoformat(timespec="seconds"),
         "end_reason": "imported",
         "segments": [{"track": "system", "file": out.name, "target": None,
                       "start_offset_s": 0.0, "end_offset_s": seconds, "end_reason": "imported"}]}
    if title and title.strip():
        m["title_override"] = title.strip()[:120]
    write_manifest(rec, m)
    return rec
