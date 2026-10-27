# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hrishikesh Jadhav
"""Which addresses are yours (docs/dev/PLAN.md › Defaults › Own address).

You are on almost every invitation, so prep and people must leave you out: with you
counted, "Standup" of another team shares a person with every standup you attend.
The setting my_emails wins; without it, an address that is in every recorded call that
has attendee addresses (at least INFER_MIN_CALLS such calls) is taken to be yours.
"""

from __future__ import annotations

from ..config.settings import Settings
from .catalog import CallRecord

INFER_MIN_CALLS = 3


def infer_addresses(calls: list[CallRecord]) -> set[str]:
    """Addresses in every call that lists any. Calls saved before 1.0 have names only and
    are not counted, so they neither support nor block the guess."""
    sets = [{e.casefold() for _, e in c.attendees if e} for c in calls]
    sets = [s for s in sets if s]
    if len(sets) < INFER_MIN_CALLS:
        return set()
    return set.intersection(*sets)


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
