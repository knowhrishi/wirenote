# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hrishikesh Jadhav
"""vocabulary.txt: names and terms Whisper should spell right (one per line, # comments)."""

from __future__ import annotations

from pathlib import Path

from .settings import config_dir

MAX_TERMS = 200


def vocabulary_path() -> Path:
    return config_dir() / "vocabulary.txt"


def load_vocabulary() -> list[str]:
    try:
        lines = vocabulary_path().read_text().splitlines()
    except FileNotFoundError:
        return []
    terms = [l.strip() for l in lines if l.strip() and not l.lstrip().startswith("#")]
    return list(dict.fromkeys(terms))[:MAX_TERMS]


def save_vocabulary(terms: list[str]) -> list[str]:
    clean = list(dict.fromkeys(t.strip() for t in terms if t.strip()))[:MAX_TERMS]
    path = vocabulary_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_text("# Names and terms for the transcript, one per line.\n" + "\n".join(clean) + "\n")
    return clean
