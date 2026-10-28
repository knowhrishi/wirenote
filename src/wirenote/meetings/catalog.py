# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hrishikesh Jadhav
"""One reader for all calls (docs/dev/PLAN.md › Defaults › People).

Reads each recording's manifest, transcript, summary and flags into a CallRecord.
People pages and meeting prep share it, so the rules for old recordings live here:
a field a version did not write is read as "none", never as an error.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from ..capture.recorder import read_flags, read_manifest
from ..process.note import title_for
from ..process.speakers import display_name
from ..process.summarize import Summary

SUMMARY, TRANSCRIPT = "summary.json", "transcript.json"


@dataclass(frozen=True)
class CallRecord:
    id: str
    title: str
    started: str
    calendar_uid: str
    calendar_title: str
    attendees: tuple[tuple[str, str], ...]     # (name, email); email "" when unknown
    speakers: tuple[tuple[str, float], ...]    # named speakers only, seconds of speech
    summary: dict | None
    flags: tuple[float, ...]


def load_calls(root: Path) -> list[CallRecord]:
    """Every readable call under root, newest first. A folder that cannot be read is skipped."""
    try:
        dirs = sorted((d for d in root.iterdir() if (d / "manifest.json").is_file()), reverse=True)
    except OSError:
        return []
    out = []
    for d in dirs:
        try:
            out.append(_load(d))
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            continue
    return out


def _json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _load(d: Path) -> CallRecord:
    m = read_manifest(d)
    summary = _json(d / SUMMARY) or None
    cal = m.get("calendar") or {}
    return CallRecord(
        id=str(m["id"]),
        title=title_for(m, Summary(title=str((summary or {}).get("title") or ""))),
        started=str(m["started"]),
        calendar_uid=str(cal.get("uid") or ""),
        calendar_title=str(cal.get("title") or ""),
        attendees=_attendees(cal),
        speakers=_speakers(_json(d / TRANSCRIPT)),
        summary=summary,
        flags=tuple(read_flags(d)))


def _attendees(cal: dict) -> tuple[tuple[str, str], ...]:
    """(name, email) pairs. Calendars saved before 1.0 have no people, only attendees and
    emails: two lists that line up by index when they are as long (every invitee then had
    an address); otherwise names only. An invitee who has no display name is listed by
    address, so that address is the email too."""
    pairs = cal.get("people")
    if not isinstance(pairs, list):
        names, emails = cal.get("attendees") or [], cal.get("emails") or []
        if not (isinstance(emails, list) and isinstance(names, list) and len(emails) == len(names)):
            emails = [""] * len(names)
        pairs = [[n, e] for n, e in zip(names, emails)]
    out = []
    for p in pairs:
        name, email = (list(p) + ["", ""])[:2] if isinstance(p, (list, tuple)) else (p, "")
        name, email = str(name).strip(), str(email or "").strip()
        if "@" in name and not email:
            email = name
        if name:
            out.append((name, email))
    return tuple(out)


def _speakers(transcript: dict) -> tuple[tuple[str, float], ...]:
    """Seconds of speech per speaker who has a name; Me, Others and unnamed ones are left out."""
    speakers = transcript.get("speakers") or {}
    seconds: dict[str, float] = {}
    for line in transcript.get("lines") or []:
        label = line["speaker"]
        if not (speakers.get(label) or {}).get("name"):
            continue
        name = display_name(label, speakers)
        seconds[name] = seconds.get(name, 0.0) + max(0.0, line["end"] - line["start"])
    return tuple(seconds.items())
