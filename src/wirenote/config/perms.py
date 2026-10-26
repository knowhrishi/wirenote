# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hrishikesh Jadhav
"""Private files: recordings, transcripts, notes and logs are for their
owner only. New files are private through umask 077; these find and fix older ones."""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Iterator
from pathlib import Path

from .settings import Settings, config_dir, data_dir, state_dir

def _walk(roots: list[Path]) -> Iterator[Path]:
    for root in roots:
        if not root.is_dir():
            continue
        yield root
        for dirpath, dirnames, filenames in os.walk(root):
            for name in dirnames + filenames:
                yield Path(dirpath) / name


def _loose(p: Path) -> bool:
    st = p.lstat()
    return not stat.S_ISLNK(st.st_mode) and bool(st.st_mode & 0o077)


def loose_files(roots: list[Path], files: list[Path] = ()) -> list[Path]:
    """Folders and files under roots, plus files, that group or others can access."""
    extra = [f for f in files if f.exists() and _loose(f)]
    return [p for p in _walk(roots) if _loose(p)] + extra


def fix_modes(roots: list[Path], files: list[Path] = ()) -> int:
    """Take group and other access away; returns how many changed. The owner's bits
    (an executable script stays executable) and symlinks are left alone."""
    changed = 0
    for p in loose_files(roots, files):
        os.chmod(p, stat.S_IMODE(p.lstat().st_mode) & 0o700)
        changed += 1
    return changed


def private_paths(settings: Settings) -> tuple[list[Path], list[Path]]:
    """(folders Wirenote owns, to check whole; the notes it wrote). The notes folder
    may be a vault or ~/Documents, so only the notes recordings point to are ours."""
    from ..process.note import own_note

    rec_root = settings.recordings_path()
    notes = []
    for manifest in sorted(rec_root.glob("*/manifest.json")) if rec_root.is_dir() else []:
        try:
            note = own_note(json.loads(manifest.read_text()), settings.notes_path())
        except (OSError, ValueError):
            continue
        if note:
            notes.append(note)
    return [config_dir(), state_dir(), data_dir(), rec_root], notes
