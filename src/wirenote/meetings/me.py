# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hrishikesh Jadhav
"""Which addresses are yours (docs/dev/PLAN.md › Defaults › Own address).

You are on almost every invitation, so prep and people must leave you out: with you
counted, "Standup" of another team shares a person with every standup you attend.
The setting my_emails wins; without it, an address that is in at least INFER_PERCENT % of
the recorded calls that have attendee addresses (and at least INFER_MIN_CALLS such calls)
is taken to be yours, if only one is. Not every one: an invitation forwarded to you does not list you.
"""

from __future__ import annotations

from collections import Counter

from ..config.settings import Settings
from .catalog import CallRecord

INFER_MIN_CALLS = 3       # docs/dev/PLAN.md › Defaults › Own address
INFER_PERCENT = 90        # › Own address


def infer_addresses(calls: list[CallRecord]) -> set[str]:
    """The one address in at least INFER_PERCENT % of the calls that list any, as a set; no
    guess (empty) when none or more than one qualifies. Two qualify when you only meet one
    person one to one: their address is as common as yours, and a guess would hide them.
    A call whose invitees have no addresses (an older one where some invitee had none) is
    not counted: it neither supports nor blocks the guess."""
    sets = [{e.casefold() for _, e in c.attendees if e} for c in calls]
    sets = [s for s in sets if s]
    if len(sets) < INFER_MIN_CALLS:
        return set()
    seen = Counter(e for s in sets for e in s)
    found = {e for e, n in seen.items() if 100 * n >= INFER_PERCENT * len(sets)}
    return found if len(found) == 1 else set()


def my_addresses(calls: list[CallRecord], settings: Settings) -> set[str]:
    """Lower-case addresses that are yours: my_emails if set, else the inferred ones."""
    own = {e.strip().casefold() for e in settings.my_emails if e.strip()}
    return own or infer_addresses(calls)


def without_me(pairs, mine: set[str], my_names: set[str]) -> list[tuple[str, str]]:
    """The (name, email) pairs that are not you: not your address, and, when there is no
    address, not a name your address goes by in another call."""
    return [(n, e) for n, e in pairs
            if not ((e and e.casefold() in mine) or (not e and n.strip().casefold() in my_names))]


def my_names(pair_lists, mine: set[str]) -> set[str]:
    """The names your addresses appear under in these invitee lists."""
    return {n.strip().casefold() for pairs in pair_lists for n, e in pairs if e and e.casefold() in mine and n}
