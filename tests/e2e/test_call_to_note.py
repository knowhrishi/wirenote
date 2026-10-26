# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hrishikesh Jadhav
"""End to end: a call starts, is recorded, ends, becomes a note, and shows in the app.

One recorded Jitsi call in Firefox (tests/replay/jitsi-firefox.json) runs through
the real `wirenote run`, `wirenote record`, the worker and `wirenote ui`; see
harness.py for what is faked. The flow runs once per module and each test checks
one promise about its result, so a failure names the promise that broke.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import stat
import sys
from pathlib import Path

import pytest

from harness import SUMMARY, World

pytestmark = [pytest.mark.e2e,
              pytest.mark.skipif(not sys.platform.startswith("linux"),
                                 reason="wirenote runs on Linux only")]

ME = "I will send the changelog by Thursday."
OTHERS = "Great, then we ship the release on Friday."


def record_a_call(world: World) -> None:
    """Join, click Record (through `wirenote record`, as without notification buttons),
    talk for a few seconds, leave, and wait for the note."""
    world.start_daemon()
    world.wait_for(lambda: "prompt" in world.event_names(), 30, "the Record prompt")
    assert world.cli("record").returncode == 0
    world.wait_for(lambda: min(world.wav_bytes("mic"), world.wav_bytes("system")) > 44 + 4 * 16000 * 2,
                   30, "4 s of audio on both tracks")
    world.hangup()
    world.wait_for(lambda: "capture_stop" in world.event_names(), 30, "the recording to stop")
    note_done(world)


def note_done(world: World, timeout: float = 120) -> None:
    """The worker marks the job done just before it exits; the daemon logs job_end
    after that. Waiting for the log keeps a stop right after from racing it."""
    world.wait_for(lambda: "job_end" in world.event_names(), timeout, "the note")
    assert [state for state, _ in world.jobs()] == ["done"], world.diagnostics()


@pytest.fixture(scope="module")
def call(tmp_path_factory):
    world = World(tmp_path_factory.mktemp("e2e"))
    try:
        record_a_call(world)
        world.exit_code = world.stop_daemon()
        if os.environ.get("WIRENOTE_E2E_KEEP"):
            keep_for_compat(world, Path(os.environ["WIRENOTE_E2E_KEEP"]))
        yield world
    finally:
        world.close()


def keep_for_compat(world: World, dest: Path) -> None:
    """Save what this version stored, without audio, as tests/compat/<version>/
    (docs/dev/changes.md › Stored data)."""
    from dataclasses import fields

    from wirenote.config.apps import DEFAULT_APPS_TOML
    from wirenote.config.settings import Settings

    rec = world.recording_dir()
    (dest / "recording").mkdir(parents=True)
    for f in ("manifest.json", "transcript.json", "summary.json"):
        shutil.copy(rec / f, dest / "recording" / f)
    shutil.copy(world.state / "jobs.db", dest / "jobs.db")   # the daemon has stopped: no WAL left
    # Every setting this version knows, spelled out, as a user's config.toml may be.
    defaults = Settings()
    (dest / "config.toml").write_text("".join(
        f"{f.name} = {json.dumps(list(v) if isinstance(v := getattr(defaults, f.name), tuple) else v)}\n"
        for f in fields(Settings)))
    (dest / "apps.toml").write_text(DEFAULT_APPS_TOML)


def note_text(world: World) -> str:
    (_, note), = world.jobs()
    return open(note).read()


def test_note_holds_summary_actions_and_both_speakers(call):
    note = note_text(call)
    assert 'title: "Release planning"' in note
    assert 'app: "firefox"' in note and 'service: "Jitsi"' in note
    assert "- [ ] **Me**: Send the changelog (due: Thursday)" in note
    assert "Ship the release on Friday" in note
    me = next(line for line in note.splitlines() if ME in line)
    others = next(line for line in note.splitlines() if OTHERS in line)
    assert me.startswith("**Me**") and others.startswith("**Others**")


def test_recording_has_two_tracks_and_a_clean_end(call):
    rec = call.recording_dir()
    manifest = json.loads((rec / "manifest.json").read_text())
    assert manifest["end_reason"] == "app left the mic"
    assert {s["track"] for s in manifest["segments"]} == {"mic", "system"}
    for seg in manifest["segments"]:
        data = (rec / seg["file"]).read_bytes()
        assert int.from_bytes(data[40:44], "little") == len(data) - 44   # header sizes fixed
    assert manifest["note"] and manifest["summary_backend"] == "ollama:fake:latest"


def test_summary_request_went_to_the_local_backend_with_the_transcript(call):
    (req,) = [r for r in call.ollama.requests if "title" in r["format"]["properties"]]
    user = req["messages"][-1]["content"]
    assert ME in user and OTHERS in user
    assert req["model"] == "fake:latest"


def test_detector_log_tells_the_story_without_private_words(call):
    names = call.event_names()
    order = ["start", "prompt", "capture_start", "capture_stop", "job_start", "job_end", "stop"]
    assert [n for n in names if n in order] == order
    (end,) = [e for e in call.events() if e["event"] == "job_end"]
    assert end["state"] == "done" and end["note"] is True
    log = (call.state / "detector.jsonl").read_text()
    for private in ('"Room"', SUMMARY["title"], ME, OTHERS, str(call.notes)):
        assert private not in log


def test_everything_written_is_private_to_the_user(call):
    loose = [str(p) for top in (call.recordings, call.notes, call.state)
             for p in [top, *top.rglob("*")]
             if not p.is_symlink() and stat.S_IMODE(p.stat().st_mode) & 0o077]
    assert loose == []


def test_daemon_stops_cleanly(call):
    assert call.exit_code == 0
    status = json.loads((call.state / "status.json").read_text())
    assert status["running"] is False
    assert not (call.home / "run" / "wirenote.sock").exists()


def logged_in(world: World):
    if world._ui is None:
        world.start_ui()
    return world.login()


def test_meetings_app_lists_and_edits_the_call(call):
    ui = logged_in(call)
    (listed,) = ui.json("GET", "/api/calls")["calls"]
    assert listed["title"] == "Release planning" and listed["state"] == "done"
    detail = ui.json("GET", f"/api/calls/{listed['id']}")
    assert {line["text"] for line in detail["lines"]} >= {ME, OTHERS}
    assert ui.json("POST", f"/api/calls/{listed['id']}/action/0", {"done": True}) == {"ok": True}
    assert "- [x] **Me**: Send the changelog (due: Thursday)" in note_text(call)
    # Without the login cookie, nothing is served.
    assert ui.request("GET", "/api/calls", session=False)[0] == 401


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")
def test_meetings_app_plays_the_audio_with_seeking(call):
    ui = logged_in(call)
    rec_id = call.recording_dir().name
    status, headers, body = ui.request("GET", f"/api/calls/{rec_id}/audio",
                                       headers={"Range": "bytes=0-99"})
    assert status == 206 and len(body) == 100 and headers["Content-Type"] == "audio/ogg"


def test_a_killed_daemon_still_yields_the_note(tmp_path):
    """docs/dev/m2-m4-design.md › M2: `kill -9` mid-call loses no audio."""
    world = World(tmp_path)
    try:
        world.start_daemon()
        world.wait_for(lambda: "prompt" in world.event_names(), 30, "the Record prompt")
        assert world.cli("record").returncode == 0
        world.wait_for(lambda: world.wav_bytes("system") > 44 + 3 * 16000 * 2, 30, "audio")
        world.stop_daemon(signal.SIGKILL)
        world.hangup()                      # the call ends while Wirenote is down
        world.start_daemon()
        world.wait_for(lambda: "recovered" in world.event_names(), 30, "crash recovery")
        note_done(world)
        manifest = json.loads((world.recording_dir() / "manifest.json").read_text())
        assert manifest["end_reason"] == "crash" and manifest["note"]
        assert ME in note_text_of(manifest)
        assert world.stop_daemon() == 0
    finally:
        world.close()


def note_text_of(manifest: dict) -> str:
    return open(manifest["note"]).read()
