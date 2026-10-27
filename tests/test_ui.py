# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hrishikesh Jadhav
"""UI: the data layer and the HTTP server's safety checks."""

from __future__ import annotations

import http.client
import json
import subprocess
import threading
from dataclasses import replace
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from wirenote.capture.recorder import read_manifest, write_flags, write_manifest
from wirenote.process.merge import Line
from wirenote.ui import data as data_mod
from wirenote.ui.data import Invalid, NotFound, Store, _relevant
from wirenote.ui.server import App, make_handler

from test_process import FakeBackend, FakeTranscriber, make_recording, no_voices, settings_for


@pytest.fixture
def store(tmp_path):
    from wirenote.process.jobs import Jobs
    from wirenote.process.worker import run_job

    rec = make_recording(tmp_path)
    s = replace(settings_for(tmp_path), recordings_dir=str(tmp_path / "recordings"))
    jobs = Jobs(tmp_path / "state" / "jobs.db")
    run_job(jobs.add(rec), s, jobs, FakeTranscriber(), lambda _s: FakeBackend(),
            voices=no_voices(tmp_path))
    return Store(s, tmp_path / "state", voices=no_voices(tmp_path)), rec.name


def test_list_and_search(store):
    st, rec_id = store
    (call,) = st.calls()
    assert call["id"] == rec_id and call["title"] == "Release planning"
    assert call["state"] == "done" and call["open_actions"] == 1
    hit = st.calls("shall we ship")[0]["match"]
    assert hit["where"] == "transcript" and hit["speaker"] == "S1" and hit["start"] == 1.0
    assert st.calls("nothing like this") == []


def test_ids_cannot_escape_the_recordings_dir(store):
    st, _ = store
    for bad in ("../../etc", "20261027-100000-chrome/../x", "", "x"):
        with pytest.raises(NotFound):
            st.rec_dir(bad)


def test_ticking_an_action_updates_summary_and_note(store):
    st, rec_id = store
    st.set_action_done(rec_id, 0, True)
    assert st.call(rec_id)["summary"]["action_items"][0]["done"] is True
    note = Path(st.call(rec_id)["note"]).read_text()
    assert "- [x] **Me**: Send the changelog (due: Thursday)" in note
    st.set_action_done(rec_id, 0, False)
    assert "- [ ] **Me**: Send the changelog" in Path(st.call(rec_id)["note"]).read_text()
    with pytest.raises(Invalid):
        st.set_action_done(rec_id, 9, True)


def test_rename_and_requeue_rules(store):
    st, rec_id = store
    st.rename(rec_id, "  Friday ship call ")
    assert st.calls()[0]["title"] == "Friday ship call"
    (st.rec_dir(rec_id) / "mic-000.wav").unlink()
    with pytest.raises(Invalid, match="audio was deleted"):
        st.requeue(rec_id, summary_only=False)
    st.requeue(rec_id, summary_only=True)
    assert st.calls()[0]["state"] == "queued"


def test_requeue_with_template_and_series(store):
    from wirenote.capture.recorder import write_manifest
    from wirenote.process.templates import series_templates

    st, rec_id = store
    d = st.rec_dir(rec_id)
    with pytest.raises(Invalid, match="template"):
        st.requeue(rec_id, summary_only=True, template="no-such-template")
    with pytest.raises(Invalid, match="calendar"):
        st.requeue(rec_id, summary_only=True, template="standup", series=True)
    assert read_manifest(d).get("template") is None and series_templates(st.state_dir) == {}
    m = read_manifest(d)
    write_manifest(d, {**m, "calendar": {"title": "Sync", "uid": "weekly@test"}})
    st.requeue(rec_id, summary_only=True, template="standup", series=True)
    assert read_manifest(d)["template"] == "standup"
    assert series_templates(st.state_dir) == {"weekly@test": "standup"}
    assert st.calls()[0]["state"] == "queued"
    st.requeue(rec_id, summary_only=True, template="")            # back to automatic
    assert "template" not in read_manifest(d)
    st.requeue(rec_id, summary_only=True)                        # no template given: unchanged
    info = st.call(rec_id)
    assert [t["id"] for t in info["templates"]][:2] == ["general", "one-on-one"]
    assert info["template"] == "general" and info["section_titles"] == {}
    assert info["in_series"] is True
    assert "default_template" in st.settings_info()["editable"] and st.settings_info()["templates"]


def test_a_series_template_can_be_forgotten(store):
    from wirenote.process.templates import series_templates

    st, rec_id = store
    d = st.rec_dir(rec_id)
    write_manifest(d, {**read_manifest(d), "calendar": {"title": "Sync", "uid": "weekly@test"}})
    assert st.call(rec_id)["series_template"] is None
    st.requeue(rec_id, summary_only=True, template="standup", series=True)
    assert st.call(rec_id)["series_template"] == "standup"
    st.requeue(rec_id, summary_only=True, template="", series=True)     # Automatic, for the series
    assert series_templates(st.state_dir) == {} and "template" not in read_manifest(d)
    assert st.call(rec_id)["series_template"] is None


def test_resummarize_body_sets_template_and_series(server):
    from wirenote.process.templates import series_templates

    app, port, rec_id = server
    st, d = app.store, app.store.rec_dir(rec_id)
    write_manifest(d, {**read_manifest(d), "calendar": {"title": "Sync", "uid": "weekly@test"}})
    path, hdrs = f"/api/calls/{rec_id}/resummarize", {"X-Wirenote-Token": app.token}
    assert request(port, "POST", path, headers=hdrs, body={"template": "standup", "series": True})[0] == 200
    assert read_manifest(d)["template"] == "standup"
    assert series_templates(st.state_dir) == {"weekly@test": "standup"}
    assert request(port, "POST", path, headers=hdrs, body={})[0] == 200          # unchanged
    assert read_manifest(d)["template"] == "standup"
    assert request(port, "POST", path, headers=hdrs, body={"template": "", "series": True})[0] == 200
    assert "template" not in read_manifest(d) and series_templates(st.state_dir) == {}
    assert request(port, "POST", path, headers=hdrs, body={"series": True})[0] == 400


def test_default_template_is_a_setting_with_a_known_value(store, monkeypatch):
    st, _ = store
    monkeypatch.setattr(data_mod, "poke_daemon", lambda *_a: None)
    assert st.update_settings({"default_template": "lecture"}, False)["editable"]["default_template"] == "lecture"
    with pytest.raises(Invalid, match="default_template"):
        st.update_settings({"default_template": "nope"}, False)


def test_ask_cites_timestamps_and_keeps_history(store, monkeypatch):
    st, rec_id = store
    backend = FakeBackend([{"answer": "Friday.", "citations": ["00:00:01", "bogus"]}])
    monkeypatch.setattr(data_mod, "make_backend", lambda s: backend)
    entry = st.ask(rec_id, "When do we ship?")
    assert entry["answer"] == "Friday." and entry["citations"] == [1.0] and not entry["partial"]
    assert "Question: When do we ship?" in backend.calls[0][1]
    assert st.call(rec_id)["asks"][0]["question"] == "When do we ship?"
    with pytest.raises(Invalid):
        st.ask(rec_id, "  ")


def test_long_transcripts_are_cut_to_the_relevant_parts():
    lines = [Line("Others", i * 10.0, i * 10.0 + 5, f"filler sentence number {i} about nothing")
             for i in range(400)]
    lines[200] = Line("Me", 2000.0, 2005.0, "the budget for the migration is approved")
    text, partial = _relevant(lines, "Was the migration budget approved?", 2000)
    assert partial and "budget for the migration" in text and len(text) <= 2100
    assert _relevant(lines[:3], "x", 2000) == (_relevant(lines[:3], "x", None)[0], False)


def test_delete_call_removes_note_audio_and_job(store):
    st, rec_id = store
    note = Path(st.call(rec_id)["note"])
    st.delete_call(rec_id)
    assert not note.exists() and st.calls() == []
    with st._jobs_lock:
        assert st.jobs.recent() == []


# Server ------------------------------------------------------------------------

SESSIONS: dict[int, str] = {}   # port -> session cookie value, filled by the server fixture


@pytest.fixture
def server(store, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    st, rec_id = store
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), None)
    port = httpd.server_address[1]
    app = App(st.settings, st.state_dir, port)
    app.store = st
    httpd.RequestHandlerClass = make_handler(app)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    SESSIONS[port] = app.session_key
    yield app, port, rec_id
    SESSIONS.pop(port, None)
    httpd.shutdown()
    httpd.server_close()


def cookie(port) -> dict:
    return {"Cookie": f"wirenote_session={SESSIONS[port]}"}


def request(port, method, path, host=None, headers=None, body=None, session=True):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    hdrs = {"Host": host or f"127.0.0.1:{port}", **(cookie(port) if session else {}),
            **(headers or {})}
    conn.request(method, path, body=json.dumps(body) if body is not None else None, headers=hdrs)
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    return resp.status, data


def test_page_carries_the_token_and_api_lists_calls(server):
    app, port, rec_id = server
    status, page = request(port, "GET", "/")
    assert status == 200 and app.token.encode() in page
    status, body = request(port, "GET", "/api/calls")
    assert status == 200 and json.loads(body)["calls"][0]["id"] == rec_id


def test_foreign_host_and_missing_token_are_refused(server):
    app, port, rec_id = server
    assert request(port, "GET", "/api/calls", host="evil.example:80")[0] == 403
    assert request(port, "DELETE", f"/api/calls/{rec_id}")[0] == 403
    assert request(port, "DELETE", f"/api/calls/{rec_id}",
                   headers={"X-Wirenote-Token": "wrong"})[0] == 403
    assert request(port, "POST", f"/api/calls/{rec_id}/action/0", body={"done": True},
                   headers={"X-Wirenote-Token": app.token,
                            "Origin": "http://evil.example"})[0] == 403
    status, _ = request(port, "POST", f"/api/calls/{rec_id}/action/0", body={"done": True},
                        headers={"X-Wirenote-Token": app.token,
                                 "Origin": f"http://127.0.0.1:{port}"})
    assert status == 200


def test_static_files_stay_inside_static(server):
    _, port, _ = server
    assert request(port, "GET", "/static/app.css")[0] == 200
    assert request(port, "GET", "/static/../server.py")[0] == 404
    assert request(port, "GET", "/static/%2e%2e/server.py")[0] == 404
    assert request(port, "GET", "/api/calls/..%2F..%2Fetc")[0] == 404


def test_audio_is_served_with_ranges(server, tmp_path):
    app, port, rec_id = server
    mix = app.store.rec_dir(rec_id) / "mix.ogg"
    mix.write_bytes(bytes(range(256)) * 4)
    app.store.mixed_audio = lambda _id: mix          # no ffmpeg in this test
    status, body = request(port, "GET", f"/api/calls/{rec_id}/audio", headers={"Range": "bytes=10-19"})
    assert status == 206 and body == bytes(range(10, 20))
    status, body = request(port, "GET", f"/api/calls/{rec_id}/audio", headers={"Range": "bytes=-4"})
    assert status == 206 and body == bytes(range(252, 256))
    assert read_manifest(app.store.rec_dir(rec_id))["id"] == rec_id


def test_add_flag_after_the_call_and_refused_while_recording(store, tmp_path):
    st, rec_id = store
    assert st.add_flag(rec_id, 4.2) == [4.2]
    assert st.add_flag(rec_id, 5.0) == [4.2]                       # within 2 s: dropped
    assert st.add_flag(rec_id, 1.5) == [1.5, 4.2]
    for bad in (-1, float("nan"), float("inf"), 10 ** 9, "3", True):
        with pytest.raises(Invalid):
            st.add_flag(rec_id, bad)
    assert st.add_flag(rec_id, 2000.0) == [1.5, 4.2, 1800.0]          # the call is 30 min long
    write_flags(st.rec_dir(rec_id), [1.5, 4.2])
    call = st.call(rec_id)
    assert [(f["t"], f["start"], f["name"]) for f in call["flags"]] == [
        (1.5, 1.0, "Speaker 1"), (4.2, 4.0, "Me")]
    assert call["flags"][1]["text"] == "Yes, Friday works."
    assert "## Flagged moments\n\n- [00:00:01] **Speaker 1** Shall we ship" in Path(call["note"]).read_text()

    (tmp_path / "state" / "status.json").write_text(json.dumps({"recording_id": rec_id}))
    with pytest.raises(Invalid, match="still recording"):
        st.add_flag(rec_id, 9.0)
    assert st.call(rec_id)["state"] == "recording"                 # the file is the daemon's now
    with pytest.raises(NotFound):
        st.add_flag("../x", 1.0)

    # An imported call still waiting for its transcript can be flagged; the flag waits too.
    imp = make_recording(tmp_path / "import")
    imp = imp.rename(st.rec_dir(rec_id).parent / "20261027-120000-import")
    write_manifest(imp, {**read_manifest(imp), "id": imp.name, "app": "import"})
    assert st.add_flag(imp.name, 7.0) == [7.0] and st.call(imp.name)["flags"] == [
        {"t": 7.0, "start": 7.0, "speaker": None, "name": None, "text": ""}]


def test_flag_control_reaches_the_daemon_or_says_it_is_not_running(store, monkeypatch, tmp_path):
    st, _ = store
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    assert st.control("flag") == {"ok": False, "error": "daemon not running"}
    sent = []
    monkeypatch.setattr("wirenote.tools.setup.control", lambda c: sent.append(c) or {"ok": True, "t": 3.0})
    assert st.control("flag") == {"ok": True, "t": 3.0} and sent == ["flag"]


def test_flag_routes(server, monkeypatch):
    app, port, rec_id = server
    monkeypatch.setattr("wirenote.tools.setup.control", lambda c: {"ok": True, "app": "x", "t": 1.0})
    status, body = request(port, "POST", "/api/flag", headers={"X-Wirenote-Token": app.token}, body={})
    assert status == 200 and json.loads(body)["t"] == 1.0
    status, body = request(port, "POST", f"/api/calls/{rec_id}/flags",
                           headers={"X-Wirenote-Token": app.token}, body={"t": 4.2})
    assert status == 200 and json.loads(body) == {"flags": [4.2]}
    status, _ = request(port, "POST", f"/api/calls/{rec_id}/flags",
                        headers={"X-Wirenote-Token": app.token}, body={"t": "soon"})
    assert status == 400


# Features 2: notes, speakers, action items, ask across calls, follow-up -------

def test_my_notes_are_saved_and_rewritten_into_the_note(store):
    st, rec_id = store
    st.save_my_notes(rec_id, "- ship Friday\n- ask legal")
    call = st.call(rec_id)
    assert call["my_notes"] == "- ship Friday\n- ask legal"
    assert "## My notes\n\n- ship Friday\n- ask legal" in Path(call["note"]).read_text()
    assert st.calls("ask legal")[0]["match"]["where"] == "my_notes"


def test_notes_from_a_stale_tab_do_not_overwrite_newer_ones(store):
    from wirenote.ui.data import Invalid

    st, rec_id = store
    st.save_my_notes(rec_id, "- ship Friday", base="")
    st.save_my_notes(rec_id, "- ship Friday\n- ask legal", base="- ship Friday")   # tab B
    with pytest.raises(Invalid, match="another tab"):
        st.save_my_notes(rec_id, "- ship Friday\n- typo", base="- ship Friday")    # tab A
    assert st.call(rec_id)["my_notes"] == "- ship Friday\n- ask legal"


def test_naming_a_speaker_updates_transcript_note_and_remembers_the_voice(store):
    st, rec_id = store
    call = st.call(rec_id)
    assert [p["name"] for p in call["people"]] == ["Me", "Speaker 1"]
    st.rename_speaker(rec_id, "S1", "Anna Schmidt", remember=True)
    call = st.call(rec_id)
    assert call["names"]["S1"] == "Anna Schmidt" and call["people"][1]["source"] == "you"
    assert "**Anna Schmidt** [00:00:01]" in Path(call["note"]).read_text()
    assert [v["name"] for v in st.voices.names()] == ["Anna Schmidt"]
    assert st.calls("anna")[0]["match"]["where"] == "people"
    with pytest.raises(Invalid):
        st.rename_speaker(rec_id, "../x", "Bob")


def test_merging_two_speakers(store):
    st, rec_id = store
    d = st.rec_dir(rec_id)
    tr = json.loads((d / "transcript.json").read_text())
    tr["lines"].append({"speaker": "S2", "start": 6.0, "end": 7.0, "text": "Same person, split."})
    tr["speakers"]["S2"] = {"name": None, "source": None, "seconds": 1.0}
    (d / "transcript.json").write_text(json.dumps(tr))
    st.rename_speaker(rec_id, "S2", merge_into="S1")
    call = st.call(rec_id)
    assert {l["speaker"] for l in call["lines"]} == {"Me", "S1"}
    assert "S2" not in json.loads((d / "transcript.json").read_text())["speakers"]


def test_action_items_across_calls(store):
    st, rec_id = store
    (item,) = st.action_items()
    assert item["owner"] == "Me" and item["call"] == rec_id and not item["done"]
    st.set_action_done(rec_id, item["index"], True)
    assert st.action_items()[0]["done"] is True


def test_ask_all_cites_call_and_moment(store, monkeypatch):
    st, rec_id = store
    backend = FakeBackend([{"answer": "Friday, in Release planning.", "citations": ["C1 00:00:01", "C9 00:00:01"]}])
    monkeypatch.setattr(data_mod, "make_backend", lambda s: backend)
    entry = st.ask_all("When do we ship?")
    assert entry["citations"] == [{"call": rec_id, "title": "Release planning", "t": 1.0}]
    prompt = backend.calls[0][1]
    assert "C1 = Release planning" in prompt and "[C1 00:00:01] Speaker 1: Shall we ship on Friday?" in prompt
    assert st.asks()[-1]["question"] == "When do we ship?"


def test_followup_is_written_from_the_summary(store, monkeypatch):
    st, rec_id = store
    backend = FakeBackend([{"subject": "Recap: release planning", "body": "Hi all, ..."}])
    monkeypatch.setattr(data_mod, "make_backend", lambda s: backend)
    entry = st.followup(rec_id)
    assert entry["subject"] == "Recap: release planning" and st.call(rec_id)["followup"]["body"] == "Hi all, ..."
    assert "Me: Send the changelog (due Thursday)" in backend.calls[0][1]


def test_vocabulary_round_trip(store, tmp_path, monkeypatch):
    st, _ = store
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    assert st.set_vocabulary(["Anna Schmidt", " ", "Suchindex", "Anna Schmidt"]) == ["Anna Schmidt", "Suchindex"]
    assert st.settings_info()["vocabulary"] == ["Anna Schmidt", "Suchindex"]


def test_keep_alive_survives_posts_whose_route_ignores_the_body(server):
    app, port, rec_id = server
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    hdrs = {"Host": f"127.0.0.1:{port}", "X-Wirenote-Token": app.token,
            "Content-Type": "application/json", **cookie(port)}
    for _ in range(2):                    # two in a row: the second must read its own body
        conn.request("POST", "/api/stop", body="{}", headers=hdrs)    # no daemon: ok=false
        conn.getresponse().read()
    conn.request("POST", "/api/stop", body="{}", headers={**hdrs, "X-Wirenote-Token": "x"})
    refused = conn.getresponse()
    assert refused.status == 403
    refused.read()
    conn.request("GET", "/api/calls", headers={"Host": f"127.0.0.1:{port}", **cookie(port)})
    resp = conn.getresponse()
    assert resp.status == 200 and json.loads(resp.read())["calls"][0]["id"] == rec_id
    conn.close()


# Login ---------------------------------------------------------------

def test_requests_without_cookie_are_refused(server):
    app, port, _ = server
    status, page = request(port, "GET", "/", session=False)
    assert status == 401 and b"wirenote open" in page and app.token.encode() not in page
    assert request(port, "GET", "/api/calls", session=False)[0] == 401
    assert request(port, "GET", "/api/calls", headers={"Cookie": "wirenote_session=nope"},
                   session=False)[0] == 401
    assert request(port, "GET", "/static/app.css", session=False)[0] == 200


def _login(port, code, nxt="%23%2F"):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", f"/login?code={code}&next={nxt}", headers={"Host": f"127.0.0.1:{port}"})
    resp = conn.getresponse()
    resp.read()
    conn.close()
    return resp


def test_login_code_sets_cookie_once(server):
    from wirenote.ui.launch import new_login_code

    app, port, _ = server
    code = new_login_code()
    resp = _login(port, code, "%23%2Fcall%2F20261027-100000-chrome")
    assert resp.status == 303 and resp.getheader("Location") == "/#/call/20261027-100000-chrome"
    set_cookie = resp.getheader("Set-Cookie")
    assert f"wirenote_session={app.session_key}" in set_cookie
    assert "HttpOnly" in set_cookie and "SameSite=Strict" in set_cookie
    assert _login(port, code).status == 403


def test_old_code_refused(server):
    import os
    import time

    from wirenote.ui.launch import login_dir, new_login_code

    _, port, _ = server
    code = new_login_code()
    old = time.time() - 61
    os.utime(login_dir() / code, (old, old))
    assert _login(port, code).status == 403
    assert _login(port, "../../ui.key").status == 403


def test_next_must_be_a_hash_route(server):
    from wirenote.ui.launch import new_login_code

    _, port, _ = server
    assert _login(port, new_login_code(), "https%3A%2F%2Fevil.example").getheader("Location") == "/#/"


def test_cookie_survives_server_restart(server):
    app, _, _ = server
    again = App(app.store.settings, app.store.state_dir, 1)
    assert again.session_key == app.session_key
    import stat
    assert stat.S_IMODE((app.store.state_dir / "ui.key").stat().st_mode) == 0o600


def test_other_uid_is_refused(server, monkeypatch):
    import os

    from wirenote.ui import server as server_mod

    _, port, _ = server
    monkeypatch.setattr(server_mod, "tcp_owner", lambda *_a: os.getuid() + 1)
    assert request(port, "GET", "/api/calls")[0] == 403
    monkeypatch.setattr(server_mod, "tcp_owner", lambda *_a: None)
    assert request(port, "GET", "/api/calls")[0] == 403


# Headers and cross-site -------------------------------------------

def _raw(port, method, path, headers):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request(method, path, headers={"Host": f"127.0.0.1:{port}", **cookie(port), **headers})
    resp = conn.getresponse()
    resp.read()
    conn.close()
    return resp


def test_security_headers(server):
    _, port, _ = server
    resp = _raw(port, "GET", "/", {})
    assert resp.getheader("Cross-Origin-Resource-Policy") == "same-origin"
    assert resp.getheader("X-Content-Type-Options") == "nosniff"
    assert resp.getheader("Referrer-Policy") == "no-referrer"
    csp = resp.getheader("Content-Security-Policy")
    assert "base-uri 'none'" in csp and "form-action 'self'" in csp
    assert _raw(port, "GET", "/static/app.js", {}).getheader("Cross-Origin-Resource-Policy") == "same-origin"


def test_cross_site_fetch_refused(server):
    _, port, rec_id = server
    assert _raw(port, "GET", f"/api/calls/{rec_id}/audio", {"Sec-Fetch-Site": "cross-site"}).status == 403
    assert _raw(port, "GET", "/api/calls", {"Sec-Fetch-Site": "same-origin"}).status == 200


def test_internal_error_is_generic(server, capsys):
    app, port, _ = server

    def boom(q=""):
        raise RuntimeError("/home/someone/secret")
    app.store.calls = boom
    status, body = request(port, "GET", "/api/calls?q=private+words")
    assert status == 500 and b"secret" not in body and b"/home" not in body
    assert "private" not in capsys.readouterr().err


def test_oversized_body_closes_the_connection(server):
    app, port, _ = server
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.putrequest("POST", "/api/stop", skip_host=True)
    for k, v in {"Host": f"127.0.0.1:{port}", **cookie(port), "X-Wirenote-Token": "wrong",
                 "Content-Length": str(2 << 20)}.items():
        conn.putheader(k, v)
    conn.endheaders()
    resp = conn.getresponse()
    assert resp.status == 403 and resp.getheader("Connection") == "close"
    conn.close()


# Paths from manifests stay inside our folders -------------------------

def test_manifest_note_outside_notes_dir_is_ignored(store, tmp_path):
    from wirenote.capture.recorder import write_manifest

    st, rec_id = store
    victim = tmp_path / "victim.txt"
    victim.write_text("keep me")
    d = st.rec_dir(rec_id)
    m = read_manifest(d)
    m["note"] = str(victim)
    m["segments"][0]["file"] = "../../victim.txt"
    write_manifest(d, m)
    with pytest.raises(NotFound):
        st.note_path(rec_id)
    assert st.call(rec_id)["note"] is None
    with pytest.raises(NotFound):
        st.mixed_audio(rec_id)
    st.delete_call(rec_id)
    assert victim.read_text() == "keep me"


def test_call_reports_summary_backend(store):
    st, rec_id = store
    assert st.call(rec_id)["summary_backend"] == "fake:model"
    assert st.calls()[0]["summary_backend"] == "fake:model"


def test_api_doctor_lists_checks(server, monkeypatch):
    from wirenote.tools import setup
    from wirenote.tools.setup import Check

    monkeypatch.setattr(setup, "checks", lambda s: [Check("ok", "daemon running"),
                                                    Check("bad", "files readable", "wirenote doctor --fix")])
    _, port, _ = server
    status, body = request(port, "GET", "/api/doctor")
    data = json.loads(body)
    assert status == 200 and data["version"]
    assert data["checks"][1] == {"status": "bad", "message": "files readable", "fix": "wirenote doctor --fix"}
    assert request(port, "GET", "/api/doctor", session=False)[0] == 401


def test_page_shows_version(server):
    from wirenote import __version__

    _, port, _ = server
    assert f"Wirenote {__version__}".encode() in request(port, "GET", "/")[1]


def test_version_matches_pyproject():
    import tomllib

    from wirenote import __version__

    root = Path(__file__).parent.parent
    assert tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"] == __version__


# Settings page writes ------------------------------

@pytest.fixture
def cfg_env(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    pokes = []
    monkeypatch.setattr(data_mod, "poke_daemon", pokes.append)
    return tmp_path / "cfg" / "wirenote" / "config.toml", pokes


def test_settings_update_validates_and_writes(store, cfg_env):
    import stat

    from wirenote.config.settings import parse_settings

    st, _ = store
    path, pokes = cfg_env
    line = 'OK if I record? "Notes" only\\nthanks'
    info = st.update_settings({"consent_line": line, "whisper_language": "de"}, confirm_cloud=False)
    assert info["editable"]["consent_line"] == line
    assert parse_settings(path.read_text()).consent_line == line
    assert st.settings.whisper_language == "de"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert pokes == ["reload-settings"]


def test_settings_unknown_or_bad_type_leaves_file_unchanged(store, cfg_env):
    st, _ = store
    path, _ = cfg_env
    st.update_settings({"consent_line": "x"}, False)
    before = path.read_bytes()
    for bad in ({"ui_port": "1"}, {"consent_line": 5}, {"summary_backend": "evil"},
                {"whisper_model": "x" * 501}, {}):
        with pytest.raises(Invalid):
            st.update_settings(bad, False)
    assert path.read_bytes() == before


def test_switching_to_claude_needs_confirmation(store, cfg_env):
    st, _ = store
    with pytest.raises(Invalid, match="confirm"):
        st.update_settings({"summary_backend": "anthropic"}, confirm_cloud=False)
    assert st.update_settings({"summary_backend": "anthropic"}, confirm_cloud=True)["summary_backend"] == "anthropic"
    assert "120,000 characters" in st.settings_info()["cloud_disclosure"]


def test_api_key_is_write_only(server, cfg_env):
    app, port, _ = server
    key = "sk-ant-" + "a" * 40
    hdrs = {"X-Wirenote-Token": app.token}
    assert request(port, "POST", "/api/settings/anthropic-key", body={"key": "short"}, headers=hdrs)[0] == 400
    assert request(port, "POST", "/api/settings/anthropic-key", body={"key": key}, headers=hdrs)[0] == 200
    status, body = request(port, "GET", "/api/settings")
    assert status == 200 and key.encode() not in body and json.loads(body)["anthropic_key_set"] is True
    other = "sk-or-v1-" + "b" * 40
    assert request(port, "POST", "/api/settings/openai-key", body={"key": other}, headers=hdrs)[0] == 200
    status, body = request(port, "GET", "/api/settings")
    assert other.encode() not in body and json.loads(body)["openai_key_set"] is True


def test_calendars_never_show_links(store, cfg_env, monkeypatch):
    from wirenote.calendar import ics

    st, _ = store
    _, pokes = cfg_env
    link = "https://cal.example/private/SECRET123/basic.ics"
    monkeypatch.setattr(ics, "fetch", lambda src: b"ics")
    monkeypatch.setattr(ics, "parse", lambda text, now: [])
    cals = st.add_calendar(link)
    assert cals == [{"id": cals[0]["id"], "label": "cal.example", "kind": "link"}]
    assert "SECRET123" not in json.dumps(st.settings_info())
    with pytest.raises(Invalid):
        st.add_calendar("http://cal.example/x.ics")
    st.remove_calendar(cals[0]["id"])
    assert st.calendars() == [] and "refresh-calendar" in pokes
    with pytest.raises(NotFound):
        st.remove_calendar("nope")


def test_settings_put_requires_token_and_cookie(server, cfg_env):
    app, port, _ = server
    body = {"changes": {"consent_line": "hi"}}
    assert request(port, "PUT", "/api/settings", body=body)[0] == 403
    assert request(port, "PUT", "/api/settings", body=body, session=False,
                   headers={"X-Wirenote-Token": app.token})[0] == 401
    status, resp = request(port, "PUT", "/api/settings", body=body, headers={"X-Wirenote-Token": app.token})
    assert status == 200 and json.loads(resp)["editable"]["consent_line"] == "hi"


def test_write_token_survives_a_restart(tmp_path, store):
    """An open tab keeps saving notes after the UI service restarts (review: Minor 1)."""
    st, _ = store
    a = App(st.settings, tmp_path / "s", 1)
    b = App(st.settings, tmp_path / "s", 1)
    assert a.token == b.token and a.token != a.session_key
    assert App(st.settings, tmp_path / "other", 1).token != a.token


# Updates (docs/dev/updates-design.md) ---------------------------------------------

def test_updates_info_offers_a_newer_version_once_checked(store, monkeypatch):
    from wirenote import updates

    st, _ = store
    monkeypatch.setattr(updates, "install_kind", lambda: updates.Install("uv", Path("/x")))
    info = st.updates_info()
    assert info["available"] is None and info["check"] is False and info["can_update"]
    assert info["whats_new"] is not None          # recordings exist: an upgrade, not a new install
    (st.state_dir / updates.CHECK).write_text(json.dumps({"checked": 1.0, "latest": "99.0.0"}))
    assert st.updates_info()["available"]["version"] == "99.0.0"
    st.mark_seen()
    assert st.updates_info()["whats_new"] is None


def test_update_check_switch_writes_config_and_tells_the_daemon(store, monkeypatch):
    from wirenote.config.settings import config_dir, load_settings

    st, _ = store
    poked = []
    monkeypatch.setattr(data_mod, "poke_daemon", poked.append)
    assert st.set_update_check(True)["check"] is True
    assert load_settings(config_dir() / "config.toml").update_check is True
    assert st.set_update_check(False)["check"] is False and poked == ["reload-settings"] * 2


def test_update_runs_as_its_own_systemd_unit(store, monkeypatch):
    from wirenote import updates

    st, _ = store
    monkeypatch.setattr(updates, "install_kind", lambda: updates.Install("uv", Path("/x")))
    monkeypatch.setattr(updates, "wirenote_exe", lambda: "/bin/wirenote")
    monkeypatch.setattr(data_mod.shutil, "which", lambda n: f"/bin/{n}")
    ran = []
    monkeypatch.setattr(data_mod.subprocess, "run",
                        lambda argv, **kw: ran.append(argv) or subprocess.CompletedProcess(argv, 0, "", ""))
    st.start_update()
    assert ran == [["/bin/systemd-run", "--user", "--collect", "--quiet", "--unit", "wirenote-update",
                    "/bin/wirenote", "update", "--yes", "--wait"]]
    with pytest.raises(Invalid, match="already running"):
        st.start_update()


def test_update_is_refused_for_a_source_checkout(store, monkeypatch):
    from wirenote import updates

    st, _ = store
    monkeypatch.setattr(updates, "install_kind", lambda: updates.Install("source", Path("/x")))
    with pytest.raises(Invalid, match="git pull"):
        st.start_update()


def test_update_routes(server, monkeypatch):
    from wirenote import updates

    app, port, _ = server
    monkeypatch.setattr(updates, "check", lambda state_dir: {})
    status, body = request(port, "GET", "/api/updates")
    assert status == 200 and json.loads(body)["current"]
    status, body = request(port, "GET", "/api/changelog")
    assert status == 200 and json.loads(body)["releases"][0]["groups"]
    write = {"X-Wirenote-Token": app.token}
    assert request(port, "POST", "/api/updates/seen", headers=write, body={})[0] == 200
    assert request(port, "POST", "/api/updates/check", headers=write, body={})[0] == 200
    assert request(port, "POST", "/api/updates/seen", body={})[0] == 403       # no token
    assert request(port, "GET", "/api/updates", session=False)[0] == 401


def test_an_online_api_needs_confirmation_but_one_on_this_computer_does_not(store, cfg_env, monkeypatch):
    from wirenote.process import summarize as summ

    def no_dns(*a, **k):
        raise OSError("offline")
    monkeypatch.setattr(summ.socket, "getaddrinfo", no_dns)
    st, _ = store
    lm_studio = {"summary_backend": "openai", "openai_url": "http://127.0.0.1:1234/v1"}
    assert st.update_settings(lm_studio, confirm_cloud=False)["summary_backend"] == "openai"
    with pytest.raises(Invalid, match="confirm what is sent to openrouter.ai"):
        st.update_settings({"openai_url": "https://openrouter.ai/api/v1"}, confirm_cloud=False)
    st.update_settings({"openai_url": "https://openrouter.ai/api/v1"}, confirm_cloud=True)
    st.update_settings({"openai_model": "qwen/qwen3.8-flash"}, confirm_cloud=False)   # same place
    with pytest.raises(Invalid, match="confirm what is sent to api.openai.com"):     # another one
        st.update_settings({"openai_url": "https://api.openai.com/v1"}, confirm_cloud=False)
    with pytest.raises(Invalid, match="confirm what is sent to Claude"):
        st.update_settings({"summary_backend": "anthropic"}, confirm_cloud=False)
    with pytest.raises(Invalid, match="https://"):
        st.update_settings({"openai_url": "http://api.example.com/v1"}, confirm_cloud=True)
    info = st.settings_info()
    assert info["model"] == "qwen/qwen3.8-flash" and "OpenRouter" in info["cloud_disclosure"]


def _with_sections(st, rec_id):
    path = st.rec_dir(rec_id) / "summary.json"
    data = json.loads(path.read_text())
    data.update(template="standup", sections={"updates": [], "blockers": ["Ben waits for a review"]})
    path.write_text(json.dumps(data))


def test_followup_includes_template_sections(store, monkeypatch):
    st, rec_id = store
    _with_sections(st, rec_id)
    backend = FakeBackend([{"subject": "s", "body": "b"}])
    monkeypatch.setattr(data_mod, "make_backend", lambda s: backend)
    st.followup(rec_id)
    assert "Blockers:\n- Ben waits for a review" in backend.calls[0][1]
    assert "Updates by person" not in backend.calls[0][1]          # empty sections stay out


def test_search_finds_section_items_by_section_key(store):
    st, rec_id = store
    _with_sections(st, rec_id)
    hit = st.calls("waits for a review")[0]["match"]
    assert hit == {"where": "blockers", "text": "Ben waits for a review"}


def test_chosen_template_is_exposed_and_automatic_clears_it(store):
    st, rec_id = store
    assert st.call(rec_id)["chosen_template"] is None
    st.requeue(rec_id, summary_only=True, template="lecture")
    assert st.call(rec_id)["chosen_template"] == "lecture"
    st.requeue(rec_id, summary_only=True, template="")
    assert st.call(rec_id)["chosen_template"] is None and "template" not in read_manifest(st.rec_dir(rec_id))
