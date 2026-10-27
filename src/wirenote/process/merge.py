# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hrishikesh Jadhav
"""Merge both tracks into one transcript (docs/dev/PLAN.md › Processing, step 2).

Mic lines are "Me"; system-audio lines carry a speaker label (S1, S2, ...,
process/speakers.py) or "Others" when speakers were not separated. Echo dedupe (docs/dev/m2-m4-design.md › M3): a Me
line that overlaps an Others line and says nearly the same thing is the far
side leaking into the mic, so it is dropped.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from difflib import SequenceMatcher

_WORD = re.compile(r"\w+", re.UNICODE)


@dataclass(frozen=True)
class Line:
    speaker: str
    start: float      # seconds from the start of the recording
    end: float
    text: str

    def as_dict(self) -> dict:
        return asdict(self)


def _norm(text: str) -> str:
    return " ".join(_WORD.findall(text.lower()))


def dedupe_echo(lines: list[Line], min_overlap: float, min_similarity: float
                ) -> tuple[list[Line], list[Line]]:
    """Drop Me lines that echo an overlapping Others line. Returns (kept, dropped)."""
    others = [l for l in lines if l.speaker != "Me"]
    kept, dropped = [], []
    for line in lines:
        if line.speaker == "Me" and _echoes(line, others, min_overlap, min_similarity):
            dropped.append(line)
        else:
            kept.append(line)
    return kept, dropped


def _echoes(me: Line, others: list[Line], min_overlap: float, min_similarity: float) -> bool:
    length = max(me.end - me.start, 1e-6)
    mine = _norm(me.text)
    if not mine:
        return False
    for o in others:
        overlap = min(me.end, o.end) - max(me.start, o.start)
        if overlap / length < min_overlap:
            continue
        if SequenceMatcher(None, mine, _norm(o.text)).ratio() >= min_similarity:
            return True
    return False


def paragraphs(lines: list[Line], max_gap: float) -> list[Line]:
    """Sort by time and join consecutive lines of one speaker closer than max_gap."""
    out: list[Line] = []
    for line in sorted(lines, key=lambda l: (l.start, l.speaker)):
        text = line.text.strip()
        if not text:
            continue
        prev = out[-1] if out else None
        if prev and prev.speaker == line.speaker and line.start - prev.end < max_gap:
            out[-1] = Line(prev.speaker, prev.start, max(prev.end, line.end),
                           f"{prev.text} {text}")
        else:
            out.append(Line(line.speaker, line.start, line.end, text))
    return out


def flagged_lines(lines: list[Line], flags: list[float], before_s: float, after_s: float
                  ) -> list[tuple[float, Line]]:
    """The lines that overlap [t - before_s, t + after_s] for each flag t, as (t, line) in
    time order. A line several flags reach is listed once, with the first of them.
    People flag just after hearing something, so the window reaches further back."""
    picked: dict[int, tuple[float, Line]] = {}
    for t in sorted(flags):
        for i, line in enumerate(lines):
            if i not in picked and line.end >= t - before_s and line.start <= t + after_s:
                picked[i] = (t, line)
    return sorted(picked.values(), key=lambda p: (p[1].start, p[1].speaker))


def nearest_line(lines: list[Line], t: float) -> Line | None:
    """The line being said at t, else the one closest in time (the earlier on a tie)."""
    def gap(l: Line) -> float:
        return 0.0 if l.start <= t <= l.end else min(abs(t - l.start), abs(t - l.end))
    return min(lines, key=lambda l: (gap(l), l.start), default=None)


def stamp(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def as_text(lines: list[Line], names: dict[str, str] | None = None) -> str:
    """Plain transcript for the summary backend; names maps labels to display names."""
    names = names or {}
    return "\n".join(f"[{stamp(l.start)}] {names.get(l.speaker, l.speaker)}: {l.text}"
                     for l in lines)
