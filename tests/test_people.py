# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hrishikesh Jadhav
"""One reader for all calls, and a page per person (docs/dev/PLAN.md › People)."""

from __future__ import annotations

import json
import shutil
import time
from dataclasses import replace
from pathlib import Path

import pytest

from wirenote.capture.recorder import write_flags, write_manifest
from wirenote.meetings.catalog import CallRecord, load_calls
from wirenote.meetings.people import build_people, person_key
from wirenote.ui.data import Invalid, NotFound, Store

from test_process import no_voices, settings_for
import test_ui
from test_ui import request

COMPAT_CALLS = Path(__file__).parent / "compat_calls"
store = test_ui.store      # the HTTP fixtures of test_ui, which test_people_api_... uses
server = test_ui.server


def rec(root: Path, n: int, *, speakers: dict | None = None, lines: list | None = None,
        attendees: list | None = None, people: list | None = None, actions: list | None = None,
        title: str = "Call") -> Path:
    d = root / f"20261027-1{n:05d}-chrome"
    d.mkdir(parents=True)
    cal = None
    if attendees is not None:
        cal = {"title": title, "attendees": attendees, "emails": []}
        if people is not None:
            cal["people"] = people
    write_manifest(d, {"version": 1, "id": d.name, "app": "chrome", "name": "Chrome", "service": None,
                       "room": None, "calendar": cal, "started": f"2026-10-27T10:{n % 60:02d}:00+00:00",
                       "ended": "x", "segments": []})
    (d / "transcript.json").write_text(json.dumps({"lines": lines or [], "speakers": speakers or {}}))
    (d / "summary.json").write_text(json.dumps({"title": title, "summary": [], "decisions": [],
                                                "action_items": actions or [], "open_questions": []}))
    return d


def talk(speaker: str, start: float, end: float) -> dict:
    return {"speaker": speaker, "start": start, "end": end, "text": "hello"}


def anna_world(root: Path) -> None:
    # Call A: Anna spoke for 10 s. Call B: Anna was only invited, with an email.
    rec(root, 1, speakers={"S1": {"name": "Anna"}}, lines=[talk("S1", 0, 6), talk("Me", 6, 8), talk("S1", 8, 12)],
        actions=[{"owner": "Anna", "task": "Send the plan", "due": "Friday"},
                 {"owner": "Me", "task": "Review it", "due": ""}], title="Kickoff")
    rec(root, 2, attendees=["Anna", "Bo"], people=[["Anna", "anna@example.org"], ["Bo", "bo@example.org"]],
        title="Follow-up")


def test_catalog_reads_0_3_recordings(tmp_path):
    shutil.copytree(COMPAT_CALLS / "0.3.1" / "recording", tmp_path / "20261027-192142-firefox")
    # The released snapshot has no calendar or named speaker; it must load the same way.
    plain = tmp_path / "20261027-192143-firefox"
    shutil.copytree(Path(__file__).parent / "compat" / "0.3.1" / "recording", plain)
    manifest = json.loads((plain / "manifest.json").read_text())
    write_manifest(plain, {**manifest, "id": plain.name})
    calls = load_calls(tmp_path)
    assert [c.id for c in calls] == [plain.name, "20261027-192142-firefox"]
    plain, cal = calls
    assert plain.calendar_uid == "" and plain.flags == () and plain.attendees == ()
    assert plain.speakers == () and plain.summary["title"] == "Release planning"
    assert cal.calendar_uid == "" and cal.flags == () and cal.calendar_title == "Release planning"
    # Before 1.0 only names were stored; emails are not guessed.
    assert cal.attendees == (("Anna Berg", ""), ("bo@example.org", "bo@example.org"))
    assert cal.speakers == (("Anna Berg", 2.5),)
    assert isinstance(cal, CallRecord)


def test_catalog_skips_unreadable_folders_and_reads_flags(tmp_path):
    d = rec(tmp_path, 1, attendees=["Ana"], title="Planning")
    write_flags(d, [3.0, 9.5])
    broken = tmp_path / "20261027-100002-chrome"
    broken.mkdir()
    (broken / "manifest.json").write_text("{not json")
    (call,) = load_calls(tmp_path)
    assert call.id == d.name and call.flags == (3.0, 9.5) and call.title == "Planning"
    assert load_calls(tmp_path / "missing") == []


def test_people_join_voices_attendees_and_actions(tmp_path):
    anna_world(tmp_path)
    people = build_people(load_calls(tmp_path), {})
    assert sorted(people) == ["anna", "bo"]
    anna = people["anna"]
    assert anna.name == "Anna" and anna.emails == {"anna@example.org"}
    assert [(c["title"], c["role"], c["seconds"]) for c in anna.calls] == [
        ("Follow-up", "invited", 0.0), ("Kickoff", "spoke", 10.0)]
    assert anna.seconds == 10.0
    assert anna.last_met.startswith("2026-10-27T10:02")
    assert [(a["call"], a["index"], a["task"], a["due"], a["done"]) for a in anna.actions] == [
        ("20261027-100001-chrome", 0, "Send the plan", "Friday", False)]
    assert people["bo"].emails == {"bo@example.org"} and people["bo"].seconds == 0.0


def test_me_others_and_unnamed_speakers_are_not_people(tmp_path):
    rec(tmp_path, 1, speakers={"S1": {"name": None}, "S2": {"name": "Cy"}, "S3": {"name": "Speaker 7"}},
        lines=[talk("Me", 0, 5), talk("Others", 5, 9), talk("S1", 9, 12), talk("S2", 12, 15), talk("S3", 15, 16)],
        attendees=["Me", "Others", "Speaker 2", "Dee"], actions=[{"owner": "Me", "task": "x", "due": ""}])
    assert sorted(build_people(load_calls(tmp_path), {})) == ["cy", "dee"]


def test_alias_merges_two_names(tmp_path):
    anna_world(tmp_path)
    rec(tmp_path, 3, speakers={"S1": {"name": "Anna B."}}, lines=[talk("S1", 0, 5)],
        actions=[{"owner": "anna b.", "task": "Book room", "due": ""}], title="Third")
    calls = load_calls(tmp_path)
    assert sorted(build_people(calls, {})) == ["anna", "anna b.", "bo"]
    people = build_people(calls, {"anna b.": "Anna"})
    assert sorted(people) == ["anna", "bo"]
    anna = people["anna"]
    assert len(anna.calls) == 3 and anna.seconds == 15.0
    assert sorted(a["task"] for a in anna.actions) == ["Book room", "Send the plan"]
    # A loop of aliases (hand-edited file) must end, not hang.
    assert build_people(calls, {"anna b.": "Anna", "anna": "anna b."})


def test_person_key_is_stable_and_case_blind():
    assert person_key("Anna") == person_key("ANNA") == person_key("anna")
    assert len(person_key("Anna")) == 12 and person_key("Anna") != person_key("Bo")


@pytest.fixture
def people_store(tmp_path):
    s = replace(settings_for(tmp_path), recordings_dir=str(tmp_path / "recordings"))
    anna_world(tmp_path / "recordings")
    return Store(s, tmp_path / "state", voices=no_voices(tmp_path))


def test_store_people_alias_persist(people_store, tmp_path):
    store = people_store
    listed = {p["name"]: p for p in store.people()}
    assert set(listed) == {"Anna", "Bo"} and listed["Anna"]["key"] == person_key("Anna")
    assert listed["Anna"]["calls"] == 2 and listed["Anna"]["open_actions"] == 1
    rec(tmp_path / "recordings", 3, speakers={"S1": {"name": "Anna B."}}, lines=[talk("S1", 0, 5)])
    assert len(store.people()) == 3
    key = store.alias("Anna B.", "Anna")
    assert key == person_key("Anna") and len(store.people()) == 2
    assert json.loads((tmp_path / "state" / "people.json").read_text()) == {"aliases": {"anna b.": "Anna"}}
    assert len(store.person(key)["calls"]) == 3
    with pytest.raises(Invalid):
        store.alias("", "Anna")
    with pytest.raises(Invalid):
        store.alias("anna", "ANNA")
    with pytest.raises(NotFound):
        store.person("0" * 12)
    with pytest.raises(NotFound):
        store.person("Anna")


def test_people_api_and_unknown_person_404(server):
    app, port, rec_id = server
    anna_world(app.store.root)
    status, body = request(port, "GET", "/api/people")
    people = json.loads(body)["people"]
    assert status == 200 and {p["name"] for p in people} == {"Anna", "Bo"}
    key = next(p["key"] for p in people if p["name"] == "Anna")
    status, body = request(port, "GET", f"/api/people/{key}")
    one = json.loads(body)
    assert status == 200 and one["name"] == "Anna" and len(one["calls"]) == 2 and one["actions"][0]["task"]
    assert request(port, "GET", "/api/people/" + "0" * 12)[0] == 404
    assert request(port, "GET", "/api/people/Anna")[0] == 404
    hdr = {"X-Wirenote-Token": app.token}
    body = {"name": "Bo", "same_as": "Anna"}
    assert request(port, "POST", "/api/people/alias", body=body)[0] == 403         # no token
    assert request(port, "POST", "/api/people/alias", body=body, session=False,
                   headers=hdr)[0] == 401                                           # no session
    status, out = request(port, "POST", "/api/people/alias", headers=hdr, body=body)
    assert status == 200 and json.loads(out) == {"key": key}
    assert request(port, "POST", "/api/people/alias", headers=hdr, body={"name": "x"})[0] == 400
    assert len(json.loads(request(port, "GET", "/api/people")[1])["people"]) == 1


def test_people_scale_500_calls(tmp_path):
    names = [f"Person {i}" for i in range(60)]
    for n in range(500):
        who = names[n % 60]
        rec(tmp_path, n, speakers={"S1": {"name": who}}, lines=[talk("S1", 0, 30), talk("Me", 30, 60)] * 20,
            attendees=[who, names[(n + 1) % 60]],
            people=[[who, f"p{n % 60}@example.org"], [names[(n + 1) % 60], ""]],
            actions=[{"owner": who, "task": "t", "due": ""}] * 3)
    t0 = time.perf_counter()
    people = build_people(load_calls(tmp_path), {})
    elapsed = time.perf_counter() - t0
    assert len(people) == 60 and sum(len(p.calls) for p in people.values()) >= 500
    assert elapsed < 2.0, f"{elapsed:.2f}s"


def test_a_person_with_your_address_is_not_listed(tmp_path):
    rec(tmp_path, 1, attendees=["Me Myself", "Anna"], people=[["Me Myself", "me@x.test"], ["Anna", "a@x.test"]])
    rec(tmp_path, 2, attendees=["Me Myself", "Bo"], people=[["Me Myself", "me@x.test"], ["Bo", ""]])
    rec(tmp_path, 3, attendees=["Me Myself", "Cy"], people=[["Me Myself", "me@x.test"], ["Cy", ""]],
        speakers={"S1": {"name": "Me Myself"}}, lines=[talk("S1", 0, 5)])
    calls = load_calls(tmp_path)
    assert "me myself" in build_people(calls, {})
    assert sorted(build_people(calls, {}, {"me@x.test"})) == ["anna", "bo", "cy"]
