# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hrishikesh Jadhav
"""Importing audio files as recordings, and exporting a call as a Word file."""

from __future__ import annotations

import datetime as dt
import http.client
import io
import json
import shutil
import wave
import zipfile
from dataclasses import replace
from pathlib import Path
from xml.dom import minidom

import numpy as np
import pytest

from wirenote.capture.importer import ImportFailed, import_audio
from wirenote.capture.recorder import read_manifest
from wirenote.process.note import title_for
from wirenote.process.summarize import Summary
from wirenote.ui.export import docx_bytes, export_name

import test_ui
from test_ui import SESSIONS

store, server = test_ui.store, test_ui.server      # the app server fixtures, reused

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")


def stereo_44k(path: Path, seconds: float = 2.0) -> Path:
    """What a phone or another recorder makes: 44.1 kHz stereo."""
    t = np.arange(int(seconds * 44100)) / 44100
    tone = (0.3 * np.sin(2 * np.pi * 330 * t) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(44100)
        w.writeframes(np.repeat(tone, 2).tobytes())
    return path


@needs_ffmpeg
def test_import_converts_to_the_recording_format_and_keeps_the_original(tmp_path):
    src = stereo_44k(tmp_path / "Team sync.wav")
    before = src.read_bytes()
    started = dt.datetime(2026, 10, 27, 14, 0).astimezone()
    rec = import_audio(src, tmp_path / "recordings", title="  Q4 planning ", started=started)
    assert rec.name == "20261027-140000-import" and src.read_bytes() == before
    with wave.open(str(rec / "system-000.wav")) as w:
        assert (w.getframerate(), w.getnchannels(), w.getsampwidth()) == (16000, 1, 2)
        assert abs(w.getnframes() / 16000 - 2.0) < 0.05
    m = read_manifest(rec)
    assert m["app"] == "import" and m["name"] == "Team sync" and m["end_reason"] == "imported"
    assert m["title_override"] == "Q4 planning" and m["ended"] > m["started"]
    assert title_for(m, Summary(title="From the summary")) == "Q4 planning"   # names the note too
    (seg,) = m["segments"]
    assert seg["track"] == "system" and abs(seg["end_offset_s"] - 2.0) < 0.05
    assert (rec / "system-000.wav").stat().st_mode & 0o077 == 0
    # The same start time again gets its own folder.
    again = import_audio(src, tmp_path / "recordings", started=started)
    assert again.name == "20261027-140000-import-2"
    assert title_for(read_manifest(again), None) == "Team sync"


@needs_ffmpeg
def test_a_file_without_audio_leaves_nothing_behind(tmp_path):
    bad = tmp_path / "notes.txt"
    bad.write_text("not audio")
    with pytest.raises(ImportFailed, match="not an audio or video file") as e:
        import_audio(bad, tmp_path / "recordings")
    assert str(tmp_path) not in str(e.value)               # no paths in the message
    assert list((tmp_path / "recordings").iterdir()) == []
    with pytest.raises(ImportFailed, match="no such file"):
        import_audio(tmp_path / "missing.m4a", tmp_path / "recordings")


def test_import_needs_ffmpeg(tmp_path, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    src = tmp_path / "a.mp3"
    src.write_bytes(b"x")
    with pytest.raises(ImportFailed, match="apt install ffmpeg"):
        import_audio(src, tmp_path / "recordings")


@needs_ffmpeg
def test_cli_import_queues_a_job_and_pokes_the_daemon(tmp_path, monkeypatch, capsys):
    from wirenote import cli
    from wirenote.config.settings import state_dir
    from wirenote.process.jobs import Jobs
    from wirenote.tools import setup

    poked = []
    monkeypatch.setattr(setup, "_poke_daemon", poked.append)
    src = stereo_44k(tmp_path / "call.wav")
    assert cli.main(["import", str(src), "--date", "2026-10-27 09:30"]) == 0
    job = Jobs(state_dir() / "jobs.db").next_queued()
    assert job.recording.name == "20261027-093000-import" and poked == ["kick-jobs"]
    assert "imported as 20261027-093000-import" in capsys.readouterr().out
    assert cli.main(["import", str(src), "--date", "Tuesday"]) == 2


def call_data(**extra) -> dict:
    return {"title": "Release / planning", "started": "2026-10-27T10:00:00+01:00",
            "duration_s": 1800, "service": "Jitsi", "room": "Room",
            "people": [{"label": "Me", "name": "Me"}, {"label": "S1", "name": "Anna"}],
            "names": {"Me": "Me", "S1": "Anna"},
            "summary": {"summary": ["Planned the release <2.0> & more."],
                        "decisions": ["Ship on Friday."],
                        "action_items": [{"owner": "Me", "task": "Send the changelog",
                                          "due": "Thursday", "done": True}],
                        "open_questions": []},
            "my_notes": "- check \x07 the CI",
            "lines": [{"speaker": "S1", "start": 61.0, "end": 63.0, "text": "Shall we ship?"}],
            **extra}


def docx_text(data: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = set(z.namelist())
        assert {"[Content_Types].xml", "_rels/.rels", "word/document.xml",
                "word/styles.xml", "word/_rels/document.xml.rels", "docProps/core.xml"} <= names
        for name in names:
            minidom.parseString(z.read(name))              # every part is well-formed XML
        doc = minidom.parseString(z.read("word/document.xml"))
    return "".join(t.firstChild.data for t in doc.getElementsByTagName("w:t") if t.firstChild)


def test_word_export_holds_notes_and_transcript():
    text = docx_text(docx_bytes(call_data()))
    for part in ("Release / planning", "Tuesday, 27 October 2026, 10:00 · 30 min · Jitsi · Room",
                 "With: Anna", "Planned the release <2.0> & more.", "Ship on Friday.",
                 "☑", "Send the changelog (due: Thursday)", "check  the CI",
                 "Anna", "00:01:01", "Shall we ship?"):
        assert part in text, part
    assert "Open questions" not in text
    assert export_name(call_data(), "docx") == "2026-10-27 1000 Release  planning.docx"
    empty = docx_text(docx_bytes(call_data(summary=None, lines=[], my_notes="")))
    assert "No speech was recognized." in empty and "Summary" not in empty


def raw_request(port, method, path, body: bytes, headers: dict):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    conn.request(method, path, body=body, headers={
        "Host": f"127.0.0.1:{port}", "Cookie": f"wirenote_session={SESSIONS[port]}", **headers})
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    return resp.status, dict(resp.getheaders()), data


def test_the_app_serves_the_word_file_as_a_download(server):
    app, port, rec_id = server
    status, headers, body = raw_request(port, "GET", f"/api/calls/{rec_id}/docx", None, {})
    assert status == 200 and headers["Content-Type"].endswith("wordprocessingml.document")
    assert headers["Content-Disposition"].startswith('attachment; filename="2026-10-27 1000 ')
    assert "Shall we ship on Friday?" in docx_text(body)


@needs_ffmpeg
def test_upload_through_the_app_becomes_a_queued_call(server, tmp_path, monkeypatch):
    from wirenote.ui import data as data_mod

    monkeypatch.setattr(data_mod, "poke_daemon", lambda cmd: None)
    app, port, _ = server
    wav_bytes = stereo_44k(tmp_path / "x.wav").read_bytes()
    token = {"X-Wirenote-Token": app.token, "Content-Type": "application/octet-stream"}
    modified = str(int(dt.datetime(2026, 10, 20, 9, 0).timestamp() * 1000))
    status, _, body = raw_request(port, "POST", "/api/import", wav_bytes, {
        **token, "X-Filename": "..%2F..%2FWeekly%20sync.wav", "X-Last-Modified": modified})
    assert status == 200, body
    r = json.loads(body)
    assert r["id"] == "20261020-090000-import"
    call = app.store.call(r["id"])
    assert call["state"] == "queued" and call["name"] == "Weekly sync"
    root = Path(app.store.settings.recordings_path())
    assert not [p for p in root.iterdir() if p.name.startswith(".import-")]   # upload removed
    # Without the token nothing is read or stored.
    assert raw_request(port, "POST", "/api/import", wav_bytes, {})[0] == 403
    status, _, body = raw_request(port, "POST", "/api/import", b"not audio at all", token)
    assert status == 400 and b"not an audio or video file" in body


def test_upload_limits(tmp_path):
    from wirenote.ui.data import Invalid, Store

    from test_process import no_voices, settings_for

    s = replace(settings_for(tmp_path), recordings_dir=str(tmp_path / "recordings"))
    st = Store(s, tmp_path / "state", voices=no_voices(tmp_path))
    with pytest.raises(Invalid, match="up to 4 GB"):
        st.import_upload(io.BytesIO(b"").read, 0, "a.wav")
    with pytest.raises(Invalid, match="stopped before the end"):
        st.import_upload(io.BytesIO(b"abc").read, 10, "a.wav")


def test_daemon_starts_a_queued_job_when_told(tmp_path):
    from test_daemon import make

    d = make(tmp_path)
    assert not d._worker_kick.is_set()
    assert d._control("kick-jobs")["ok"] and d._worker_kick.is_set()
