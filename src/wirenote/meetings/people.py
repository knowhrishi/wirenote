# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hrishikesh Jadhav
"""People across calls (docs/dev/PLAN.md › Defaults › People).

A person is a name that spoke in a call (a named speaker) or was invited to one,
joined by case-folded name. Me, Others and unnamed "Speaker N" are not people. There
is no own-address setting, so everyone invited counts. Aliases say that one name is
another (stored in state_dir/people.json); they never touch the recordings.
Names are untrusted text: pages and URLs use person_key, never the name.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from .catalog import CallRecord
from .me import my_names

PEOPLE_FILE = "people.json"
NOT_PEOPLE = re.compile(r"^(me|others|s\d+|speaker \d+)$")
MAX_NAME = 100


@dataclass
class Person:
    name: str
    emails: set[str] = field(default_factory=set)
    calls: list[dict] = field(default_factory=list)    # id, title, started, role, seconds
    actions: list[dict] = field(default_factory=list)  # call, index, task, due, done
    last_met: str = ""
    seconds: float = 0.0


def person_key(name: str) -> str:
    """The id used in URLs: the first 12 hex digits of sha256 of the case-folded name."""
    return hashlib.sha256(name.strip().casefold().encode()).hexdigest()[:12]


def is_person(name: str) -> bool:
    return bool(name.strip()) and not NOT_PEOPLE.match(name.strip().casefold())


def resolve(name: str, aliases: dict[str, str]) -> str:
    """The case-folded name that name stands for; follows chains, ends at a loop."""
    seen: set[str] = set()
    key = name.strip().casefold()
    while key in aliases and key not in seen:
        seen.add(key)
        key = aliases[key].strip().casefold()
    return key


def build_people(calls: list[CallRecord], aliases: dict[str, str],
                 mine: set[str] = frozenset()) -> dict[str, Person]:
    """calls newest first. Keyed by the case-folded canonical name. mine are your addresses
    (meetings/me.py): an invitee with one is you, not a person, and neither is a speaker
    named like one of your address's names."""
    people: dict[str, Person] = {}
    mine_names = my_names([c.attendees for c in calls], mine) if mine else set()

    def person(name: str) -> Person:
        key = resolve(name, aliases)
        if key not in people:
            people[key] = Person(name=_spelling(key, name, aliases))
        return people[key]

    for call in calls:
        here: dict[str, dict] = {}      # one entry per person per call
        for name, email in call.attendees:
            if is_person(name) and not (email.casefold() in mine if email else name.strip().casefold() in mine_names):
                p = person(name)
                if email:
                    p.emails.add(email)
                here.setdefault(resolve(name, aliases), {"p": p, "role": "invited", "seconds": 0.0})
        for name, secs in call.speakers:
            if is_person(name) and name.strip().casefold() not in mine_names:
                e = here.setdefault(resolve(name, aliases), {"p": person(name), "role": "", "seconds": 0.0})
                e["role"], e["seconds"] = "spoke", e["seconds"] + secs
        for e in here.values():
            p = e["p"]
            p.calls.append({"id": call.id, "title": call.title, "started": call.started,
                            "role": e["role"], "seconds": round(e["seconds"], 1)})
            p.seconds += e["seconds"]
            p.last_met = max(p.last_met, call.started)
    for call in calls:      # after all calls: an owner may first appear in an older one
        for i, a in enumerate((call.summary or {}).get("action_items") or []):
            owner = str(a.get("owner") or "")
            key = resolve(owner, aliases)
            if is_person(owner) and key in people:
                people[key].actions.append({"call": call.id, "index": i, "task": a.get("task", ""),
                                            "due": a.get("due", ""), "done": bool(a.get("done"))})
    return people


def _spelling(key: str, first_seen: str, aliases: dict[str, str]) -> str:
    """How to write the name: the spelling an alias gives its target, else as first seen."""
    for target in aliases.values():
        if target.strip().casefold() == key:
            return target.strip()
    return first_seen.strip()


def load_aliases(state_dir: Path) -> dict[str, str]:
    try:
        data = json.loads((state_dir / PEOPLE_FILE).read_text())
        items = data["aliases"].items()
        return {str(k): str(v) for k, v in items}
    except (OSError, ValueError, KeyError, AttributeError, TypeError):
        return {}


def save_aliases(state_dir: Path, aliases: dict[str, str]) -> None:
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = state_dir / PEOPLE_FILE
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"aliases": aliases}, indent=2, ensure_ascii=False))
    tmp.replace(path)
