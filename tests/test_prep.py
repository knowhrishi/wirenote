# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hrishikesh Jadhav
"""Which past calls belong to an upcoming event, and the brief built from them
(docs/dev/PLAN.md › Defaults › Meeting prep)."""

from __future__ import annotations

import datetime as dt
import json

from wirenote.calendar.ics import Event
from wirenote.capture.recorder import read_manifest, write_manifest
from wirenote.meetings.catalog import load_calls
from wirenote.meetings.related import event_key, prep_brief, related_calls

from test_people import rec

START = dt.datetime(2026, 11, 3, 10, 0, tzinfo=dt.timezone.utc)


def event(title="Weekly sync", uid="", people=(("Anna", "anna@x.test"), ("Ben", "ben@x.test"))):
    return Event(title=title, start=START, end=START + dt.timedelta(hours=1), uid=uid,
                 attendees=tuple(n for n, _ in people), people=tuple(people))


def make(root, n, *, uid="", title="Weekly sync", people=(("Anna", "anna@x.test"),), actions=None, summary=None,
         old=False):
    """old: written like 0.1 to 0.3, names and addresses as two lists, no people."""
    d = rec(root, n, attendees=[p[0] for p in people], people=None if old else [list(p) for p in people],
            emails=[p[1] for p in people] if old else None, title=title, actions=actions)
    m = read_manifest(d)
    m["calendar"]["uid"] = uid
    write_manifest(d, m)
    if summary is not None:
        s = json.loads((d / "summary.json").read_text())
        s.update(summary)
        (d / "summary.json").write_text(json.dumps(s))
    return d.name


def test_related_by_uid_then_title_and_people_then_people(tmp_path):
    a = make(tmp_path, 1, uid="u1", title="Totally different", people=(("Zed", ""),))
    b = make(tmp_path, 2, title="weekly SYNC #14", people=(("Ben", "ben@x.test"),))
    c = make(tmp_path, 3, title="Budget", people=(("Anna", "anna@x.test"), ("Ben", "ben@x.test")))
    make(tmp_path, 4, title="Budget", people=(("Anna", "anna@x.test"),))     # one shared person only
    got = [r.id for r in related_calls(event(uid="u1"), load_calls(tmp_path))]
    assert got == [a, b, c]


def test_unrelated_titles_with_no_shared_people_do_not_match(tmp_path):
    make(tmp_path, 1, title="Weekly sync", people=(("Zed", "zed@x.test"),))
    make(tmp_path, 2, title="Lunch", people=(("Anna", "anna@x.test"),))
    assert related_calls(event(), load_calls(tmp_path)) == []
    assert related_calls(event(people=()), load_calls(tmp_path)) == []


def test_people_match_by_name_when_an_email_is_missing(tmp_path):
    a = make(tmp_path, 1, title="Other", people=(("anna", ""), ("BEN", "")))
    got = related_calls(event(), load_calls(tmp_path))
    assert [r.id for r in got] == [a]


def test_later_calls_and_the_limit_are_respected(tmp_path):
    for n in range(1, 8):
        make(tmp_path, n, uid="u1")
    late = make(tmp_path, 9, uid="u1")
    m = read_manifest(tmp_path / late)
    m["started"] = "2026-11-03T10:30:00+00:00"          # after the event starts
    write_manifest(tmp_path / late, m)
    got = related_calls(event(uid="u1"), load_calls(tmp_path))
    assert len(got) == 5 and late not in [r.id for r in got]


def test_prep_brief_collects_open_actions_across_the_series(tmp_path):
    older = make(tmp_path, 1, uid="u1", actions=[
        {"task": "Send deck", "owner": "Anna", "due": "", "done": False},
        {"task": "Book room", "owner": "Ben", "due": "", "done": True}])
    newer = make(tmp_path, 2, uid="u1", actions=[{"task": "Fix bug", "owner": "Ben", "due": "Fri", "done": False}],
                 summary={"summary": ["We shipped it.", "Second point."], "decisions": ["Ship on Friday"]})
    brief = prep_brief(event(uid="u1"), load_calls(tmp_path))
    assert brief["event"]["title"] == "Weekly sync" and brief["event"]["key"] == event_key(event(uid="u1"))
    assert brief["last"]["title"] and brief["last"]["summary"] == ["We shipped it.", "Second point."]
    assert brief["last"]["decisions"] == ["Ship on Friday"] and brief["last"]["flags"] == 0
    assert [(a["call"], a["task"]) for a in brief["open_actions"]] == [(newer, "Fix bug"), (older, "Send deck")]
    assert [p["name"] for p in brief["people"]] == ["Anna", "Ben"]
    assert prep_brief(event(uid="nope", people=()), [])["last"] is None


def test_event_key_is_twelve_hex_and_ignores_the_title_when_there_is_a_uid():
    k = event_key(event(uid="abc"))
    assert len(k) == 12 and int(k, 16) >= 0
    assert k == event_key(event(title="Renamed", uid="abc"))
    assert k != event_key(event(title="Renamed"))
    later = Event(title="x", start=START + dt.timedelta(days=7), end=START, uid="abc")
    assert event_key(later) != k


ME = ("Me Myself", "me@x.test")


def _team(root):
    """The user is on every invitation: another team's standup, a 1:1 with Anna, filler."""
    other = make(root, 1, title="Standup", people=(ME, ("Zed", "zed@x.test")))
    one_one = make(root, 2, title="1:1", people=(ME, ("Anna", "anna@x.test")))
    make(root, 3, title="Filler", people=(ME, ("Quinn", "quinn@x.test")))
    return other, one_one


def test_you_on_every_invitation_do_not_make_other_meetings_match(tmp_path):
    from dataclasses import replace

    from wirenote.config.settings import Settings
    from wirenote.meetings.me import my_addresses

    _team(tmp_path)
    calls = load_calls(tmp_path)
    e = event(title="Standup", people=(ME, ("Anna", "anna@x.test"), ("Ben", "ben@x.test")))
    assert len(related_calls(e, calls)) == 2                 # counting you, both false matches
    mine = my_addresses(calls, Settings())
    assert mine == {"me@x.test"}
    assert related_calls(e, calls, mine=mine) == []
    assert prep_brief(e, calls, mine)["people"] and "Me Myself" not in [
        p["name"] for p in prep_brief(e, calls, mine)["people"]]
    assert my_addresses(calls, replace(Settings(), my_emails=("Other@x.test",))) == {"other@x.test"}


def test_you_are_inferred_from_calls_saved_before_1_0(tmp_path):
    from wirenote.config.settings import Settings
    from wirenote.meetings.me import my_addresses

    make(tmp_path, 1, title="All hands", people=(ME, ("Zed", "zed@x.test")), old=True)
    make(tmp_path, 2, title="1:1", people=(ME, ("Anna", "anna@x.test")), old=True)
    make(tmp_path, 3, title="Filler", people=(ME, ("Quinn", "quinn@x.test")), old=True)
    calls = load_calls(tmp_path)
    mine = my_addresses(calls, Settings())
    assert mine == {"me@x.test"}
    e = event(title="All hands", people=(ME, ("Anna", "anna@x.test"), ("Ben", "ben@x.test")))
    assert related_calls(e, calls, mine=mine) == []          # not the other all-hands, not the 1:1


def test_your_address_is_inferred_only_from_three_calls(tmp_path):
    from wirenote.config.settings import Settings
    from wirenote.meetings.me import infer_addresses, my_addresses

    make(tmp_path, 1, people=(ME, ("Zed", "zed@x.test")))
    make(tmp_path, 2, people=(ME, ("Anna", "anna@x.test")))
    calls = load_calls(tmp_path)
    assert infer_addresses(calls) == set() and my_addresses(calls, Settings()) == set()
    make(tmp_path, 3, people=(ME, ("Quinn", "quinn@x.test")))
    assert infer_addresses(load_calls(tmp_path)) == {"me@x.test"}
    make(tmp_path, 4, people=(("Someone", "s@x.test"),))      # not on this one: not you
    assert infer_addresses(load_calls(tmp_path)) == set()


def test_my_emails_is_a_list_setting():
    from wirenote.config.settings import parse_settings

    assert parse_settings('my_emails = ["a@x.test", "b@x.test"]').my_emails == ("a@x.test", "b@x.test")
    assert parse_settings("").my_emails == ()
    assert parse_settings('my_emails = "me@x.test"').my_emails == ("me@x.test",)       # one address, no list


def test_my_emails_rejects_items_that_are_not_text():
    import pytest

    from wirenote.config.settings import parse_settings

    with pytest.raises(ValueError, match="my_emails"):
        parse_settings('my_emails = ["a@x.test", 3]')
    with pytest.raises(ValueError, match="my_emails"):
        parse_settings("my_emails = 3")


def test_your_address_is_inferred_despite_a_few_calls_without_you(tmp_path):
    from wirenote.meetings.me import infer_addresses

    def calls(n, missing, root):
        for i in range(1, n + 1):
            people = (("Other", f"o{i}@x.test"),) if i <= missing else (ME, ("Other", f"o{i}@x.test"))
            make(root, i, people=people)
        return load_calls(root)

    assert infer_addresses(calls(4, 0, tmp_path / "a")) == {"me@x.test"}         # 4 of 4
    assert infer_addresses(calls(10, 1, tmp_path / "b")) == {"me@x.test"}        # 9 of 10: a forwarded invite
    assert infer_addresses(calls(3, 1, tmp_path / "c")) == set()                 # 2 of 3 is not enough
