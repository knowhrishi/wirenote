# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hrishikesh Jadhav
"""ICS calendar: window, recurrence, all-day events, tie-break."""

from __future__ import annotations

import datetime as dt
import json

from wirenote.calendar.ics import has_call_link, in_window, parse, pick

TZ = dt.timezone(dt.timedelta(hours=1))
ICS = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//test//EN
BEGIN:VEVENT
UID:weekly@test
SUMMARY:Weekly sync
DTSTART:20261020T100000Z
DTEND:20261020T103000Z
RRULE:FREQ=WEEKLY
LOCATION:https://meet.jit.si/SomeRoom
ATTENDEE;CN=Ana:mailto:ana@example.org
ATTENDEE:mailto:bo@example.org
END:VEVENT
BEGIN:VEVENT
UID:lunch@test
SUMMARY:Lunch
DTSTART:20261027T100000Z
DTEND:20261027T110000Z
END:VEVENT
BEGIN:VEVENT
UID:holiday@test
SUMMARY:Holiday
DTSTART;VALUE=DATE:20261027
DTEND;VALUE=DATE:20261028
END:VEVENT
END:VCALENDAR
"""


def at(h, m=0):
    return dt.datetime(2026, 10, 27, h, m, tzinfo=dt.timezone.utc)


def events_at(now):
    return parse(ICS, now)


def test_recurrence_expanded_and_all_day_skipped():
    evs = events_at(at(10))
    assert sorted(e.title for e in evs) == ["Lunch", "Weekly sync"]
    sync = next(e for e in evs if e.title == "Weekly sync")
    assert sync.start == at(10) and sync.links == ("https://meet.jit.si/SomeRoom",)
    assert sync.attendees == ("Ana", "bo@example.org")
    assert sync.emails == ("ana@example.org", "bo@example.org")


def test_event_people_pairs_names_with_emails():
    ics = ("BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:-//t//EN\nBEGIN:VEVENT\nUID:a@t\nSUMMARY:S\n"
           "DTSTART:20261027T100000Z\nDTEND:20261027T110000Z\n"
           "ATTENDEE;CN=Ana:mailto:ana@example.org\nATTENDEE:mailto:bo@example.org\n"
           "ATTENDEE;CN=Room 4:resource-4\nEND:VEVENT\nEND:VCALENDAR\n")
    (ev,) = parse(ics, at(10))
    assert ev.people == (("Ana", "ana@example.org"), ("bo@example.org", "bo@example.org"),
                         ("Room 4", ""))
    assert ev.as_meta()["people"] == [["Ana", "ana@example.org"], ["bo@example.org", "bo@example.org"],
                                      ["Room 4", ""]]
    json.dumps(ev.as_meta())


def test_window_edges():
    evs = [e for e in events_at(at(9)) if e.title == "Weekly sync"]
    win = lambda now: in_window(evs, now, 600, 300)  # noqa: E731
    assert win(at(9, 49)) == []          # 11 min before
    assert win(at(9, 50)) == evs         # 10 min before
    assert win(at(10, 20)) == evs        # in progress
    assert win(at(10, 30)) == []         # ended


def test_pick_prefers_the_event_with_a_link_for_the_service():
    evs = in_window(events_at(at(10, 5)), at(10, 5), 600, 300)
    assert len(evs) == 2
    assert pick(evs, "Jitsi").title == "Weekly sync"
    assert pick(evs, None).title == "Weekly sync"       # any call link
    assert pick(evs, "Zoom") is None                    # two events, no Zoom link
    assert pick([e for e in evs if e.title == "Lunch"], "Zoom").title == "Lunch"
    assert has_call_link(evs) and not has_call_link([e for e in evs if e.title == "Lunch"])


# Limits for calendar feeds -------------------------------------------

import io
import time
import urllib.request

import pytest

from wirenote.calendar import ics
from wirenote.calendar.ics import CalendarError

SPAM = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//test//EN
BEGIN:VEVENT
UID:spam@test
SUMMARY:Spam
DTSTART:20261027T000000Z
DTEND:20261027T000001Z
RRULE:FREQ=SECONDLY
END:VEVENT
BEGIN:VEVENT
UID:real@test
SUMMARY:{title}
DTSTART:20261027T120000Z
DTEND:20261027T130000Z
END:VEVENT
END:VCALENDAR
"""


class FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def test_http_links_refused():
    with pytest.raises(CalendarError, match="https"):
        ics.fetch("http://cal.example/feed.ics")


def test_webcal_maps_to_https(monkeypatch):
    seen = []
    monkeypatch.setattr(ics, "_open", lambda req, timeout_s: seen.append(req.full_url) or FakeResp(b"x"))
    assert ics.fetch("webcal://cal.example/feed.ics") == b"x"
    assert seen == ["https://cal.example/feed.ics"]


def test_size_cap(monkeypatch):
    monkeypatch.setattr(ics, "_open", lambda req, timeout_s: FakeResp(b"x" * (ics.MAX_BYTES + 1)))
    with pytest.raises(CalendarError, match="too large"):
        ics.fetch("https://cal.example/feed.ics")


def test_rrule_secondly_is_capped_and_other_events_survive():
    t0 = time.monotonic()
    evs = parse(SPAM.format(title="Real meeting"), at(10))
    assert time.monotonic() - t0 < 2.0
    assert sum(e.title == "Spam" for e in evs) == ics.MAX_PER_EVENT
    assert any(e.title == "Real meeting" for e in evs)


def test_long_title_cut():
    (real,) = [e for e in parse(SPAM.format(title="A" * 500), at(10)) if e.title != "Spam"]
    assert len(real.title) == ics.MAX_TITLE


def test_errors_never_contain_the_link(monkeypatch):
    def fail(req, timeout_s):
        raise urllib.error.URLError("refused for https://cal.example/private/SECRET123.ics")
    monkeypatch.setattr(ics, "_open", fail)
    _, errors = ics.load(("/secret/token.ics", "https://cal.example/private/SECRET123.ics?k=9"), at(10))
    joined = " ".join(errors)
    assert "/secret/" not in joined and "SECRET123" not in joined and "k=9" not in joined
    assert "token.ics" in joined and "cal.example" in joined


def test_redirect_to_http_refused():
    handler = ics._HttpsOnlyRedirect()
    req = urllib.request.Request("https://cal.example/a.ics")
    with pytest.raises(CalendarError, match="https"):
        handler.redirect_request(req, None, 302, "Found", {}, "http://cal.example/a.ics")
    assert handler.redirect_request(req, None, 302, "Found", {}, "https://cdn.example/a.ics") is not None


def _series(uid, rule, start="20200101T000000Z", title=None):
    return (f"BEGIN:VEVENT\nUID:{uid}\nSUMMARY:{title or uid}\nDTSTART:{start}\n"
            f"DTEND:{start[:-2]}1Z\nRRULE:{rule}\nEND:VEVENT\n")


def _feed(*events):
    return ("BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:-//test//EN\n" + "".join(events) +
            "BEGIN:VEVENT\nUID:real@test\nSUMMARY:Real meeting\nDTSTART:20261027T120000Z\n"
            "DTEND:20261027T130000Z\nEND:VEVENT\nEND:VCALENDAR\n")


def test_series_from_the_distant_past_cannot_burn_cpu():
    """An invite that repeats every second since years ago (review: Important 1)."""
    feed = _feed(_series("s", "FREQ=SECONDLY", "20250101T000000Z"),
                 _series("m", "FREQ=MINUTELY"),
                 _series("b", "FREQ=YEARLY;BYMONTH=1,2,3,4,5,6,7,8,9,10,11,12;BYHOUR=0,1,2,3,4,5,6,7,"
                              "8,9,10,11,12,13,14,15,16,17,18,19,20,21,22,23;BYMINUTE=0,10,20,30,40,50"),
                 *[_series(f"h{i}", "FREQ=HOURLY", "20180101T000000Z") for i in range(40)])
    t0 = time.monotonic()
    evs = parse(feed, at(10))
    assert time.monotonic() - t0 < 3.0
    titles = {e.title for e in evs}
    assert "Real meeting" in titles and not titles & {"s", "m", "b"}


def test_ordinary_long_running_series_still_expand():
    feed = _feed(_series("weekly", "FREQ=WEEKLY;BYDAY=TU", "20150106T120000Z", "Team sync"),
                 _series("daily", "FREQ=DAILY;BYDAY=MO,TU,WE,TH,FR", "20100104T093000Z", "Standup"))
    titles = [e.title for e in parse(feed, at(10))]
    assert "Team sync" in titles and "Standup" in titles


def test_event_uid_is_kept_in_meta():
    events = {e.title: e for e in events_at(at(10, 5))}
    assert events["Weekly sync"].uid == "weekly@test"
    assert events["Weekly sync"].as_meta()["uid"] == "weekly@test"
    assert events["Lunch"].uid == "lunch@test"
