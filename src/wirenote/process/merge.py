# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hrishikesh Jadhav
"""Merge both tracks into one transcript (docs/dev/PLAN.md › Processing, step 2).

Mic lines are "Me"; system-audio lines carry a speaker label (S1, S2, ...,
process/speakers.py) or "Others" when speakers were not separated. Echo dedupe (docs/dev/m2-m4-design.md › M3): a Me
line that overlaps an Others line and says nearly the same thing is the far
side leaking into the mic, so it is dropped.
"""

from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass
from difflib import SequenceMatcher

_WORD = re.compile(r"\w+", re.UNICODE)
_TOKEN = re.compile(r"\S+")


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


def clip(line: Line, lo: float, hi: float) -> Line:
    """The part of line said between lo and hi. Speech is taken to run at an even pace, so
    time maps to characters in proportion. The cuts fall between words and are marked
    with "…", and the part is stamped with its own start. A word longer than the window,
    as in a script written without spaces, is cut between characters instead."""
    span, n = line.end - line.start, len(line.text)
    if (lo <= line.start and hi >= line.end) or span <= 0 or not n:
        return line
    i, j = (lo - line.start) / span * n, (hi - line.start) / span * n
    words = [m.span() for m in _TOKEN.finditer(line.text)]
    if not words:
        return line
    a = _cut(words, i, j - i, True)
    b = _cut(words, j, j - i, False)
    if a >= b:       # no whole word in the window: keep the nearest, or a character of it
        w = min(words, key=lambda w: abs((w[0] + w[1]) / 2 - (i + j) / 2))
        a, b = w
        if b - a > max(j - i, 1):
            a = min(max(int((i + j) / 2), w[0]), w[1] - 1)
            b = a + 1
    text = ("…" if a > words[0][0] else "") + line.text[a:b] + ("…" if b < words[-1][1] else "")
    return Line(line.speaker, line.start + a / n * span, line.start + b / n * span, text)


def _cut(words: list[tuple[int, int]], x: float, width: float, start: bool) -> int:
    """Where to cut at character offset x: inward to the nearest word boundary, or right
    at x when x falls inside a word longer than the window (width), which would otherwise
    leave nothing of the window or all of the word."""
    for s, e in words:
        if s < x < e and e - s > width:
            return math.ceil(x) if start else math.floor(x)
    if start:
        return min((s for s, _ in words if s >= x), default=words[-1][1])
    return max((e for _, e in words if e <= x), default=words[0][0])


def around(line: Line, t: float, before_s: float, after_s: float,
           max_chars: int | None = None) -> Line:
    """The part of line said around t: from before_s before it to after_s after, and at
    most about max_chars of text. A line that fits is kept whole. A paragraph can run for
    minutes (one speaker with short pauses), and a flag is about a moment of it."""
    span, n = line.end - line.start, len(line.text)
    width = max(before_s + after_s, 0.0)
    if max_chars is not None and n > max_chars and span > 0:
        width = min(width, span * max(max_chars - 2, 0) / n)     # 2: the "…" at each cut
    if span <= width or not n:
        return line
    back = before_s / (before_s + after_s) if before_s + after_s > 0 else 0.5
    lo = t - width * back
    if lo + width < line.start:      # the window misses the line: take its nearest end
        lo = line.start
    elif lo > line.end:
        lo = line.end - width
    return clip(line, lo, lo + width)


def flagged_lines(lines: list[Line], flags: list[float], before_s: float, after_s: float
                  ) -> list[tuple[float, Line]]:
    """The lines that overlap [t - before_s, t + after_s] for each flag t, as (t, part) in
    time order, each cut to that window when it is longer (around()). A part several flags
    reach is listed once, with the first of them. People flag just after hearing
    something, so the window reaches further back."""
    picked: dict[Line, float] = {}
    for t in sorted(flags):
        for line in lines:
            if line.end >= t - before_s and line.start <= t + after_s:
                picked.setdefault(around(line, t, before_s, after_s), t)
    return sorted(((t, l) for l, t in picked.items()), key=lambda p: (p[1].start, p[1].speaker))


def distance(line: Line, t: float) -> float:
    """Seconds from t to the nearest moment of line; 0 while it is being said."""
    return 0.0 if line.start <= t <= line.end else min(abs(t - line.start), abs(t - line.end))


def nearest_line(lines: list[Line], t: float) -> Line | None:
    """The line being said at t, else the one closest in time (the earlier on a tie)."""
    return min(lines, key=lambda l: (distance(l, t), l.start), default=None)


def flag_excerpt(lines: list[Line], t: float, before_s: float, after_s: float) -> Line | None:
    """What was said at flag t, for the note and the app: the line closest to it, cut to
    the flag window when it is longer, and stamped with the start of that part."""
    line = nearest_line(lines, t)
    return around(line, t, before_s, after_s) if line is not None else None


def stamp(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def as_text(lines: list[Line], names: dict[str, str] | None = None) -> str:
    """Plain transcript for the summary backend; names maps labels to display names."""
    names = names or {}
    return "\n".join(f"[{stamp(l.start)}] {names.get(l.speaker, l.speaker)}: {l.text}"
                     for l in lines)
