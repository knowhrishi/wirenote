# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hrishikesh Jadhav
"""ICS calendar matching (docs/dev/PLAN.md › Defaults › Calendar window and refresh).

Sources are ICS links (https, webcal) or file paths, refreshed every
calendar_refresh_s. An event is in the window when it starts within
calendar_before_s, started at most calendar_after_start_s ago, or is in
progress. Tie-break (docs/dev/m2-m4-design.md › M4): prefer an event with a link
for the detected service, else the only event in the window, else none.

A calendar feed is outside input: anyone can send an invite. So links must be
https, a feed is capped in size, each event series expands to at most
MAX_PER_EVENT occurrences, a series that would take too many recurrence steps to
reach the window is skipped, and errors never repeat the (secret) link.
"""

from __future__ import annotations

import datetime as dt
import math
import re
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

LINK = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
# Hosts that mean "this event is an online call", by service name.
SERVICE_HOSTS = {
    "Jitsi": ("jitsi", "meet.jit.si"),
    "Zoom": ("zoom.us",),
    "Teams": ("teams.microsoft.com", "teams.live.com"),
    "Meet": ("meet.google.com",),
    "Rocket.Chat": ("rocket",),
    "Discord": ("discord.com", "discord.gg"),
    "Whereby": ("whereby.com",),
}
LOOKAHEAD = dt.timedelta(hours=24)
LOOKBEHIND = dt.timedelta(hours=12)
MAX_BYTES = 10_000_000
MAX_PER_EVENT = 50
MAX_EVENTS = 2000
MAX_TITLE = 200
# Expanding a series walks every occurrence from its DTSTART, also those before
# the window. Estimate those steps and skip series (and feeds) that cost too much:
# 500,000 steps take about a second.
FREQ_S = {"SECONDLY": 1, "MINUTELY": 60, "HOURLY": 3600, "DAILY": 86400,
          "WEEKLY": 7 * 86400, "MONTHLY": 28 * 86400, "YEARLY": 365 * 86400}
BY_PARTS = ("BYSECOND", "BYMINUTE", "BYHOUR", "BYDAY", "BYMONTHDAY", "BYYEARDAY",
            "BYWEEKNO", "BYMONTH", "BYSETPOS")
MAX_SERIES_STEPS = 200_000
MAX_FEED_STEPS = 500_000
SCHEME = re.compile(r"^(https?|webcal)://", re.IGNORECASE)


class CalendarError(Exception):
    pass


@dataclass(frozen=True)
class Event:
    title: str
    start: dt.datetime
    end: dt.datetime
    links: tuple[str, ...] = ()
    attendees: tuple[str, ...] = field(default=())
    emails: tuple[str, ...] = field(default=())
    uid: str = ""      # the same for every occurrence of a repeating event
    people: tuple[tuple[str, str], ...] = ()   # (name, email) per invitee; email "" if none

    def as_meta(self) -> dict:
        d = asdict(self)
        d["start"], d["end"] = self.start.isoformat(), self.end.isoformat()
        d["attendees"], d["links"] = list(self.attendees), list(self.links)
        d["emails"] = list(self.emails)
        d["people"] = [list(p) for p in self.people]
        return d

    def service_links(self, service: str | None) -> list[str]:
        hosts = SERVICE_HOSTS.get(service, ()) if service else \
            tuple(h for hs in SERVICE_HOSTS.values() for h in hs)
        return [l for l in self.links if any(h in l.lower() for h in hosts)]


def parse(text: str | bytes, now: dt.datetime) -> list[Event]:
    """Timed events (all-day events are skipped) around now, recurrences expanded.

    Each series (one UID) is expanded on its own and lazily, so one event that
    repeats every second costs MAX_PER_EVENT occurrences, not the whole window."""
    import icalendar
    import recurring_ical_events

    cal = icalendar.Calendar.from_ical(text)
    start, end = now - LOOKBEHIND, now + LOOKAHEAD
    zones = list(cal.walk("VTIMEZONE"))
    series: dict[str, list] = {}
    for i, ev in enumerate(cal.walk("VEVENT")):
        series.setdefault(str(ev.get("UID") or f"#{i}"), []).append(ev)
    out: list[Event] = []
    budget = MAX_FEED_STEPS
    for comps in series.values():
        cost = _steps_before(comps, start)
        if cost > MAX_SERIES_STEPS or cost > budget:
            continue
        budget -= cost
        sub = icalendar.Calendar()
        for c in zones + comps:
            sub.add_component(c)
        kept = 0
        for seen, ev in enumerate(recurring_ical_events.of(sub).after(start)):
            if seen >= 4 * MAX_PER_EVENT or kept >= MAX_PER_EVENT:
                break
            first = ev.decoded("DTSTART", None)
            if not isinstance(first, dt.datetime):
                if isinstance(first, dt.date) and first > end.date():
                    break
                continue
            if _aware(first) >= end:
                break
            out.append(_event(ev))
            kept += 1
        if len(out) >= MAX_EVENTS:
            return out[:MAX_EVENTS]
    return out


def _steps_before(comps: list, start: dt.datetime) -> float:
    """About how many recurrence steps expanding comps walks before reaching start.
    Overestimates (BY* lists may limit rather than expand); unknown counts as too many."""
    total = 0.0
    for ev in comps:
        rules = ev.get("RRULE")
        if rules is None:
            continue
        try:
            first = ev.decoded("DTSTART")
            if not isinstance(first, dt.datetime):
                first = dt.datetime.combine(first, dt.time(), start.tzinfo)
            span = max(0.0, (start - _aware(first)).total_seconds())
            for rule in rules if isinstance(rules, list) else [rules]:
                step = FREQ_S[str(rule["FREQ"][0]).upper()] * max(1, int(rule.get("INTERVAL", [1])[0]))
                factor = math.prod(max(1, len(rule.get(part) or [])) for part in BY_PARTS)
                total += span / step * factor
        except (KeyError, ValueError, TypeError, IndexError):
            return math.inf
    return total


def _event(ev) -> Event:
    start, end = ev.decoded("DTSTART"), ev.decoded("DTEND", None)
    if not isinstance(end, dt.datetime):
        end = start + (ev.decoded("DURATION", None) or dt.timedelta(hours=1))
    start, end = _aware(start), _aware(end)
    text_fields = " ".join(str(ev.get(k, "")) for k in ("LOCATION", "DESCRIPTION", "URL"))
    links = tuple(dict.fromkeys(m.rstrip(".,;)") for m in LINK.findall(text_fields)))
    att = ev.get("ATTENDEE", [])
    att = att if isinstance(att, list) else [att]
    emails = tuple(re.sub(r"^mailto:", "", str(a), flags=re.IGNORECASE) for a in att)
    names = tuple(str(a.params.get("CN") or e) for a, e in zip(att, emails))
    title = str(ev.get("SUMMARY", "")).strip()[:MAX_TITLE] or "(no title)"
    people = tuple((n, e if "@" in e else "") for n, e in zip(names, emails))
    return Event(title, start, end, links, names, tuple(e for e in emails if "@" in e),
                 str(ev.get("UID") or "")[:MAX_TITLE], people)


def _aware(t: dt.datetime) -> dt.datetime:
    return t if t.tzinfo else t.astimezone()


def in_window(events: list[Event], now: dt.datetime, before_s: float,
              after_start_s: float) -> list[Event]:
    before, after = dt.timedelta(seconds=before_s), dt.timedelta(seconds=after_start_s)
    return [e for e in events
            if e.start - before <= now and (now <= e.start + after or now < e.end)]


def pick(events: list[Event], service: str | None) -> Event | None:
    linked = [e for e in events if e.service_links(service)]
    if len(linked) == 1:
        return linked[0]
    if len(events) == 1:
        return events[0]
    return None


def has_call_link(events: list[Event]) -> bool:
    return any(e.service_links(None) for e in events)


class _HttpsOnlyRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urlsplit(newurl).scheme.lower() != "https":
            raise CalendarError("the calendar redirected away from https; refused")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open(req: urllib.request.Request, timeout_s: float):
    return urllib.request.build_opener(_HttpsOnlyRedirect).open(req, timeout=timeout_s)


def source_label(source: str) -> str:
    """What may be shown or logged for a source: the host of a link, the name of a file."""
    if SCHEME.match(source):
        return urlsplit(source).hostname or "calendar link"
    return Path(source).name or "calendar file"


def fetch(source: str, timeout_s: float = 30.0) -> bytes:
    if SCHEME.match(source):
        url = re.sub(r"^webcal://", "https://", source, flags=re.IGNORECASE)
        if urlsplit(url).scheme.lower() != "https":
            raise CalendarError("calendar links must use https")
        req = urllib.request.Request(url, headers={"User-Agent": "wirenote"})
        with _open(req, timeout_s) as resp:
            data = resp.read(MAX_BYTES + 1)
    else:
        with Path(source).expanduser().open("rb") as f:
            data = f.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise CalendarError(f"calendar too large (over {MAX_BYTES // 1_000_000} MB)")
    return data


def describe_error(source: str, e: Exception) -> str:
    """An error line without the link or path, which can be secret."""
    detail = f": {e}" if isinstance(e, CalendarError) else ""
    if isinstance(e, urllib.error.HTTPError):
        detail = f": HTTP {e.code}"
    return f"{source_label(source)}: {type(e).__name__}{detail}"


def load(sources: tuple[str, ...], now: dt.datetime) -> tuple[list[Event], list[str]]:
    """All events from all sources, plus one error string per failed source."""
    events, errors = [], []
    for i, src in enumerate(sources):
        try:
            events += parse(fetch(src), now)
        except Exception as e:  # noqa: BLE001 - one bad calendar must not hide the others
            errors.append(f"calendar {i + 1}: {describe_error(src, e)}")
    return events, errors
