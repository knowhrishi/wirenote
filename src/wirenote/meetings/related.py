# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hrishikesh Jadhav
"""Which past calls belong to an upcoming event (docs/dev/PLAN.md › Defaults › Meeting prep).

A call is related by the calendar series (same uid), by the same title and a shared
invitee, or by enough shared invitees. Titles and names are untrusted text: they are
only compared here, and the key used in links is a hash, never the title.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import re

from ..calendar.ics import Event
from .catalog import CallRecord
from .me import my_names, without_me
from .people import build_people, is_person, link

MAX_RELATED = 5
SERIES_ACTIONS = 20      # related calls whose open action items the brief collects

_DATES = re.compile(r"\d{1,4}[-/.]\d{1,2}[-/.]\d{1,4}|#\s*\d+")
_NOT_WORD = re.compile(r"[\W_]+")


def event_key(e: Event) -> str:
    """The id used in prep links: 12 hex digits of sha256 of uid (else title) and start."""
    return hashlib.sha256(f"{e.uid or e.title}|{e.start.isoformat()}".encode()).hexdigest()[:12]


def normalize_title(title: str) -> str:
    """Case-folded, without dates, #n and digits: "Standup #14" and "standup" are one series."""
    t = _DATES.sub(" ", title.casefold())
    return _NOT_WORD.sub(" ", re.sub(r"\d+", " ", t)).strip()


def _invitees(e: Event) -> tuple[tuple[str, str], ...]:
    """(name, email) of the invitees. There is no own-address setting, so all of them count."""
    if e.people:
        return e.people
    if e.attendees:
        return tuple((a, e.emails[i] if i < len(e.emails) else "") for i, a in enumerate(e.attendees))
    return tuple(("", m) for m in e.emails)


def _others(pairs, mine: set[str], names: set[str]) -> tuple[tuple[str, str], ...]:
    return tuple(without_me(pairs, mine, names)) if mine else tuple(pairs)


def _same(a: tuple[str, str], b: tuple[str, str]) -> bool:
    """By email when both have one, else by case-folded name."""
    (an, ae), (bn, be) = a, b
    if ae and be:
        return ae.casefold() == be.casefold()
    return bool(an.strip()) and an.strip().casefold() == bn.strip().casefold()


def _shared(invitees: tuple[tuple[str, str], ...], call: CallRecord, mine: set[str] = frozenset(),
            names: set[str] = frozenset()) -> int:
    theirs = _others(call.attendees, mine, names)
    return sum(1 for i in invitees if any(_same(i, a) for a in theirs))


def _before(call: CallRecord, e: Event) -> bool:
    """Only calls that started before the event: the call being recorded for it is not history."""
    try:
        started = dt.datetime.fromisoformat(call.started)
    except ValueError:
        return True
    if started.tzinfo is None:
        started = started.astimezone()
    return started < e.start


def related_calls(e: Event, calls: list[CallRecord], limit: int = MAX_RELATED,
                  mine: set[str] = frozenset()) -> list[CallRecord]:
    """calls newest first. Series matches come first, then title and people, then people alone.
    mine are your addresses (meetings/me.py): you are left out on both sides."""
    names = my_names([_invitees(e), *(c.attendees for c in calls)], mine) if mine else set()
    invitees = _others(_invitees(e), mine, names)
    title = normalize_title(e.title)
    need = min(2, len(invitees))
    tiers: list[list[CallRecord]] = [[], [], []]
    for c in calls:
        if not _before(c, e):
            continue
        if e.uid and c.calendar_uid == e.uid:
            tiers[0].append(c)
            continue
        shared = _shared(invitees, c, mine, names)
        if title and shared and normalize_title(c.calendar_title or c.title) == title:
            tiers[1].append(c)
        elif need and shared >= need:
            tiers[2].append(c)
    return [c for tier in tiers for c in tier][:limit]


def prep_brief(e: Event, calls: list[CallRecord], mine: set[str] = frozenset(),
               aliases: dict[str, str] | None = None) -> dict:
    """What to read before the event: the last related call, open action items across all
    related calls, and who is invited. last is None when nothing is related. An invitee's
    key is that of their page (through aliases), or None when they have none."""
    related = related_calls(e, calls, SERIES_ACTIONS, mine)
    names = my_names([_invitees(e), *(c.attendees for c in related)], mine) if mine else set()
    last = related[0] if related else None
    actions = []
    for c in related:
        for i, a in enumerate((c.summary or {}).get("action_items") or []):
            if isinstance(a, dict) and not a.get("done"):
                actions.append({"call": c.id, "index": i, "title": c.title, "started": c.started,
                                "task": str(a.get("task") or ""), "owner": str(a.get("owner") or ""),
                                "due": str(a.get("due") or "")})
    aliases = aliases or {}
    known = build_people(calls, aliases, mine)
    people = []
    for name, email in _others(_invitees(e), mine, names):
        if is_person(name):
            people.append({"name": name, "key": link(name, aliases, known),
                           "met": sum(1 for c in related if _shared(((name, email),), c, mine, names))})
    return {"event": {"key": event_key(e), "title": e.title, "start": e.start.isoformat(),
                      "end": e.end.isoformat()},
            "last": None if last is None else {
                "id": last.id, "title": last.title, "started": last.started,
                "summary": _texts((last.summary or {}).get("summary")),
                "decisions": _texts((last.summary or {}).get("decisions")),
                "flags": len(last.flags)},
            "related": len(related),
            "open_actions": actions, "people": people}


def _texts(items) -> list[str]:
    return [str(x) for x in items] if isinstance(items, list) else []
