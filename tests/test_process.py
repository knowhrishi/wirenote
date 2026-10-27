# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hrishikesh Jadhav
"""Processing: merge and echo dedupe, summary chunking, jobs, the worker and the note."""

from __future__ import annotations

import io

import json
from dataclasses import replace
from pathlib import Path

import pytest

from wirenote.capture.recorder import read_manifest, write_manifest
from wirenote.config.settings import Settings
from wirenote.process.jobs import MAX_ATTEMPTS, Jobs
from wirenote.process.merge import Line, as_text, dedupe_echo, paragraphs
from wirenote.process.note import title_for
from wirenote.process.summarize import (CHUNK_SCHEMA, FINAL_SCHEMA, Summary, SummaryError,
                                        chunks, summarize)
from wirenote.process.worker import TRANSCRIPT, run_job

GOOD = {"title": "Release planning", "summary": ["Planned the 2.0 release."],
        "decisions": ["Ship on Friday."],
        "action_items": [{"owner": "Me", "task": "Send the changelog", "due": "Thursday"}],
        "open_questions": ["Who tests on Firefox?"]}


class FakeBackend:
    name = "fake:model"

    def __init__(self, replies=None) -> None:
        self.replies = list(replies or [])
        self.calls: list[tuple[str, str, dict]] = []

    def complete_json(self, system, user, schema):
        self.calls.append((system, user, schema))
        reply = self.replies.pop(0) if self.replies else None
        if isinstance(reply, Exception):
            raise reply
        if reply is not None:
            return reply
        return GOOD if schema is FINAL_SCHEMA else {k: GOOD[k] for k in CHUNK_SCHEMA["required"]}


# Merge -------------------------------------------------------------------------

def test_echo_of_the_far_side_in_the_mic_is_dropped():
    lines = [Line("Others", 10.0, 14.0, "Can everyone hear me now?"),
             Line("Me", 10.3, 14.1, "can everyone hear me now"),     # echo
             Line("Me", 15.0, 16.0, "Yes, loud and clear."),
             Line("Me", 10.5, 13.0, "Something else entirely")]      # overlaps, differs
    kept, dropped = dedupe_echo(lines, 0.5, 0.8)
    assert [l.text for l in dropped] == ["can everyone hear me now"]
    assert len(kept) == 3


def test_paragraphs_join_close_lines_of_one_speaker_in_time_order():
    lines = [Line("Me", 5.0, 6.0, "second."), Line("Me", 0.0, 4.0, "First,"),
             Line("Others", 6.5, 8.0, "Reply."), Line("Me", 20.0, 21.0, "Later."),
             Line("Others", 9.0, 9.5, "  ")]
    out = paragraphs(lines, 2.0)
    assert [(l.speaker, l.text) for l in out] == [
        ("Me", "First, second."), ("Others", "Reply."), ("Me", "Later.")]
    assert as_text(out).splitlines()[0] == "[00:00:00] Me: First, second."


# Summary -----------------------------------------------------------------------

def test_short_call_is_one_request_with_title():
    backend = FakeBackend()
    s = summarize([Line("Me", 0, 5, "hello")], backend, 900, "auto", context="Call in Chrome")
    assert len(backend.calls) == 1 and backend.calls[0][2] is FINAL_SCHEMA
    assert "same language as the transcript" in backend.calls[0][0]
    assert backend.calls[0][1].startswith("Call in Chrome")
    assert s.title == "Release planning" and s.action_items[0].due == "Thursday"


def test_long_call_is_summarized_in_chunks_then_combined():
    lines = [Line("Me", t, t + 5, f"point {t}") for t in range(0, 2700, 300)]
    assert [len(c) for c in chunks(lines, 900)] == [3, 3, 3]
    backend = FakeBackend()
    summarize(lines, backend, 900, "German")
    schemas = [c[2] for c in backend.calls]
    assert schemas == [CHUNK_SCHEMA] * 3 + [FINAL_SCHEMA]
    assert "including the title, in German." in backend.calls[0][0]
    assert "Part 3:" in backend.calls[-1][1]


def test_bad_json_is_retried_once_then_fails():
    ok = summarize([Line("Me", 0, 1, "x")], FakeBackend([ValueError("not json")]), 900, "auto")
    assert ok.title
    with pytest.raises(SummaryError, match="unusable JSON twice"):
        summarize([Line("Me", 0, 1, "x")],
                  FakeBackend([{"title": 1}, ValueError("x")]), 900, "auto")


# Jobs --------------------------------------------------------------------------

def test_jobs_retry_then_fail_and_requeue_interrupted(tmp_path):
    jobs = Jobs(tmp_path / "jobs.db")
    a = jobs.add(tmp_path / "rec-a")
    assert jobs.next_queued().id == a
    for _ in range(MAX_ATTEMPTS - 1):
        assert jobs.fail(a, "boom").state == "queued"
    assert jobs.fail(a, "boom").state == "failed"
    assert jobs.next_queued() is None
    assert jobs.add(tmp_path / "rec-a") == a and jobs.get(a).attempts == 0   # reprocess
    jobs.set_state(a, "transcribing")
    assert [j.state for j in jobs.requeue_interrupted()] == ["queued"]
    assert jobs.get(a).attempts == 1



def test_jobs_schema_is_versioned_and_reads_by_column_name(tmp_path):
    """docs/dev/changes.md › Contracts: jobs.db."""
    import sqlite3

    from wirenote.process.jobs import MIGRATIONS

    jobs = Jobs(tmp_path / "jobs.db")
    assert jobs.db.execute("PRAGMA user_version").fetchone() == (len(MIGRATIONS),)
    jobs.close()
    # A later version added a column at the front and rebuilt the table: still reads,
    # and opening it neither lowers its version nor touches the table.
    db = sqlite3.connect(tmp_path / "newer.db")
    db.executescript("""
        CREATE TABLE jobs (extra TEXT, updated TEXT NOT NULL, created TEXT NOT NULL,
            note TEXT, error TEXT, summary_only INTEGER NOT NULL DEFAULT 0,
            attempts INTEGER NOT NULL DEFAULT 0, state TEXT NOT NULL,
            recording TEXT NOT NULL UNIQUE, id INTEGER PRIMARY KEY);
        INSERT INTO jobs VALUES ('x', 'u', 'c', '/n.md', NULL, 0, 1, 'done', '/rec', 7);
        PRAGMA user_version = 99;""")
    db.close()
    jobs = Jobs(tmp_path / "newer.db")
    (job,) = jobs.recent()
    assert (job.id, job.recording, job.state, job.attempts, job.note) == (7, Path("/rec"), "done", 1, "/n.md")
    assert jobs.db.execute("PRAGMA user_version").fetchone() == (99,)


# Worker ------------------------------------------------------------------------

ANNA_VOICE = [1.0, 0.0, 0.0, 0.0]


class FakeTranscriber:
    """One other speaker (S1) whose voice print is ANNA_VOICE."""

    def __init__(self) -> None:
        self.calls = 0

    def recording(self, rec_dir, manifest):
        from wirenote.process.speakers import SpeakerBook, _unit

        self.calls += 1
        book = SpeakerBook(0.55)
        book.label_for(_unit(ANNA_VOICE))
        book.add_time("S1", 2.0)
        return ([Line("S1", 1.0, 3.0, "Shall we ship on Friday?"),
                 Line("Me", 1.2, 3.1, "shall we ship on friday"),
                 Line("Me", 4.0, 5.0, "Yes, Friday works.")],
                [{"file": "mic-000.wav", "language": "en"}], book)


def make_recording(tmp_path: Path, **extra) -> Path:
    rec = tmp_path / "recordings" / "20261027-100000-chrome"
    rec.mkdir(parents=True)
    write_manifest(rec, {"version": 1, "id": rec.name, "app": "chrome", "name": "Chrome",
                         "service": None, "room": None, "calendar": None,
                         "started": "2026-10-27T10:00:00+01:00", "ended": "x",
                         "end_reason": "app left the mic",
                         "segments": [{"track": "mic", "file": "mic-000.wav", "target": None,
                                       "start_offset_s": 0.0, "end_offset_s": 1800.0}], **extra})
    (rec / "mic-000.wav").write_bytes(b"")
    return rec


def settings_for(tmp_path) -> Settings:
    return replace(Settings(), notes_dir=str(tmp_path / "Meetings"))


def no_voices(tmp_path):
    from wirenote.process.speakers import Voices
    return Voices(tmp_path / "voices.json")


def test_worker_writes_a_full_note(tmp_path):
    rec = make_recording(tmp_path)
    jobs = Jobs(tmp_path / "jobs.db")
    job = run_job(jobs.add(rec), settings_for(tmp_path), jobs, FakeTranscriber(),
                  lambda s: FakeBackend(), voices=no_voices(tmp_path))
    assert job.state == "done"
    note = Path(job.note)
    assert note == tmp_path / "Meetings/2026/10/2026-10-27 1000 Release planning.md"
    text = note.read_text()
    assert text.startswith("---\ntitle: \"Release planning\"\n")
    assert "duration_min: 30.0" in text and 'summary_backend: "fake:model"' in text
    assert "## Action items\n\n- [ ] **Me**: Send the changelog (due: Thursday)" in text
    assert "**Speaker 1** [00:00:01] Shall we ship on Friday?" in text
    assert "shall we ship on friday" not in text                   # echo dropped
    cache = json.loads((rec / TRANSCRIPT).read_text())
    assert len(cache["dropped_echo"]) == 1
    assert read_manifest(rec)["note"] == str(note)


def test_summary_failure_still_writes_the_transcript(tmp_path):
    rec = make_recording(tmp_path)
    jobs = Jobs(tmp_path / "jobs.db")

    def broken(_settings):
        raise SummaryError("Ollama not reachable")

    job = run_job(jobs.add(rec), settings_for(tmp_path), jobs, FakeTranscriber(), broken,
                  voices=no_voices(tmp_path))
    assert job.state == "done"
    text = Path(job.note).read_text()
    assert "> Summary not available: Ollama not reachable" in text
    assert "wirenote reprocess 20261027-100000-chrome --summary" in text
    assert Path(job.note).name == "2026-10-27 1000 Chrome call.md"

    # Redo only the summary: no transcription, the note gets its new title.
    tr = FakeTranscriber()
    job = run_job(jobs.add(rec, summary_only=True), settings_for(tmp_path), jobs, tr,
                  lambda s: FakeBackend(), voices=no_voices(tmp_path))
    assert tr.calls == 0 and Path(job.note).name.endswith("Release planning.md")
    assert not (tmp_path / "Meetings/2026/10/2026-10-27 1000 Chrome call.md").exists()


def test_worker_crash_is_a_failed_attempt(tmp_path):
    rec = make_recording(tmp_path)
    jobs = Jobs(tmp_path / "jobs.db")

    class Boom:
        def recording(self, *a):
            raise RuntimeError("model missing")

    job = run_job(jobs.add(rec), settings_for(tmp_path), jobs, Boom(), lambda s: None,
                  voices=no_voices(tmp_path))
    assert job.state == "queued" and job.attempts == 1 and "model missing" in job.error


def test_title_prefers_calendar_then_summary_then_room():
    m = {"app": "firefox", "name": "Firefox", "service": "Jitsi", "room": "Room"}
    assert title_for({**m, "calendar": {"title": "Weekly sync"}}, Summary("LLM title")) == "Weekly sync"
    assert title_for(m, Summary("LLM title")) == "LLM title"
    assert title_for(m, None) == "Jitsi Room"
    assert title_for({"app": "chrome", "name": "Chrome"}, None) == "Chrome call"


def test_remembered_voice_names_the_speaker_and_my_notes_reach_the_summary(tmp_path):
    rec = make_recording(tmp_path)
    (rec / "my_notes.md").write_text("- ship Friday?\n- changelog: me")
    voices = no_voices(tmp_path)
    voices.remember("Anna Schmidt", [0.9, 0.1, 0.0, 0.0])
    jobs = Jobs(tmp_path / "jobs.db")
    backend = FakeBackend()
    job = run_job(jobs.add(rec), settings_for(tmp_path), jobs, FakeTranscriber(),
                  lambda s: backend, voices=voices)
    text = Path(job.note).read_text()
    assert "**Anna Schmidt** [00:00:01] Shall we ship on Friday?" in text
    assert 'speakers: ["Anna Schmidt"]' in text
    assert "## My notes\n\n- ship Friday?\n- changelog: me" in text
    summary_call = [c for c in backend.calls if c[2] is FINAL_SCHEMA][0]
    assert "- changelog: me" in summary_call[1] and "Anna Schmidt: Shall we ship" in summary_call[1]
    speakers = json.loads((rec / TRANSCRIPT).read_text())["speakers"]
    assert speakers["S1"]["name"] == "Anna Schmidt" and speakers["S1"]["source"] == "voice"


def test_names_are_guessed_only_with_evidence_in_the_transcript():
    from wirenote.process.speakers import guess_names

    text = "[00:00:01] S1: Hi, I'm Priya from support.\n[00:00:05] S2: Thanks Priya."
    backend = FakeBackend([{"speakers": [
        {"label": "S1", "name": "Priya", "evidence": "I'm Priya"},
        {"label": "S2", "name": "Jonas", "evidence": ""}]}])
    got = guess_names(text, ["S1", "S2"], ["Priya Nair", "Jonas Weber"], backend)
    assert got == {"S1": {"name": "Priya Nair", "evidence": "I'm Priya"}}   # Jonas never said


# Ollama on a port another user owns ----------------------------------

def test_ollama_refuses_foreign_listener(monkeypatch):
    import os

    from wirenote.process import summarize as summ

    monkeypatch.setattr(summ, "listener_uid", lambda port, host="127.0.0.1": os.getuid() + 1)
    monkeypatch.setattr(summ, "trusted_owner", lambda uid: uid == os.getuid())

    def never(*_a, **_k):
        raise AssertionError("must not send")
    monkeypatch.setattr(summ.urllib.request, "urlopen", never)
    with pytest.raises(SummaryError, match="another user"):
        summ.OllamaBackend("http://127.0.0.1:11434", "qwen3:4b").complete_json("s", "u", {})


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def test_ollama_sends_to_the_address_it_checked(monkeypatch):
    """localhost may resolve to ::1 first; the check and the request use one address."""
    from wirenote.process import summarize as summ

    checked, sent = [], []
    monkeypatch.setattr(summ, "listener_uid", lambda port, host="127.0.0.1": checked.append((host, port)) or 0)
    monkeypatch.setattr(summ.urllib.request, "urlopen", lambda req, timeout: sent.append(req.full_url) or
                        _Resp(json.dumps({"message": {"content": "{}"}}).encode()))
    summ.OllamaBackend("http://localhost:11434/", "qwen3:4b").complete_json("s", "u", {})
    assert checked == [("127.0.0.1", 11434)] and sent[-1] == "http://127.0.0.1:11434/api/chat"
    assert all(u.startswith("http://127.0.0.1:11434/api/") for u in sent)   # the memory check too


def test_pin_local(monkeypatch):
    import socket

    from wirenote.process import summarize as summ
    from wirenote.process.summarize import pin_local

    assert pin_local("http://127.0.0.1:11434") == ("http://127.0.0.1:11434", "127.0.0.1", 11434)
    assert pin_local("http://localhost:11434/") == ("http://127.0.0.1:11434", "127.0.0.1", 11434)
    assert pin_local("http://[::1]:11434") == ("http://[::1]:11434", "::1", 11434)
    assert pin_local("http://localhost") == ("http://127.0.0.1:80", "127.0.0.1", 80)
    answers = {"gpu.lan": "192.168.1.5", "me.local": "::1"}
    monkeypatch.setattr(summ.socket, "getaddrinfo", lambda host, port, *a, **k:
                        [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", (answers[host], port))])
    assert pin_local("http://gpu.lan:11434") is None
    assert pin_local("http://me.local:11434") == ("http://[::1]:11434", "::1", 11434)


# Markdown from calendars and the LLM --------------------------------

def test_md_safe():
    from wirenote.process.note import md_safe

    assert md_safe("see ![x](https://t/p.png) <img src=x>") == "see !\\[x\\](https://t/p.png) &lt;img src=x>"
    assert md_safe("[ref]: https://t/beacon.png") == "\\[ref\\]: https://t/beacon.png"
    assert md_safe("plain 3 < 4") == "plain 3 &lt; 4"


def test_md_safe_leaves_no_link_syntax():
    """Review: Important 5. Pattern matching missed nested brackets, titles, newlines."""
    import re

    from wirenote.process.note import md_safe

    for bad in ("![a [b] c](http://evil/p.png)", '![x](http://e/a.png "t(")',
                "![x](\nhttp://evil/p.png)", "\\![x](http://evil/p.png)", "[y](javascript:alert(1))"):
        out = md_safe(bad)
        assert not re.search(r"(?<!\\)(?:\\\\)*[\[\]<]", out), (bad, out)


def test_note_neutralises_llm_and_calendar_text(tmp_path):
    from wirenote.process.note import render
    from wirenote.process.summarize import ActionItem

    manifest = {"id": "x", "started": "2026-10-27T10:00:00+01:00",
                "calendar": {"title": "<img src=https://t/a.png>"}}
    summary = Summary("t", ["[click](https://evil.example)"], ["![d](https://t/d.png)"],
                      [ActionItem("Me", "<script>x</script>", "")], [])
    lines = [Line("S1", 1.0, 2.0, "go to [here](https://evil.example)")]
    text = render(manifest, lines, summary, title="<img src=https://t/a.png>", duration_min=1,
                  audio=[], transcript_model="m", summary_backend="fake")
    body = text.split("---", 2)[2]
    assert "<img" not in body and "<script" not in body
    import re
    assert not re.search(r"(?<!\\)(?:\\\\)*\]\(https://(evil\.example|t/d\.png)", body)
    assert "\\[click\\](https://evil.example)" in body


def test_worker_log_has_no_names_or_paths(tmp_path, capsys):
    from wirenote.process.speakers import Voices

    voices = Voices(tmp_path / "voices.json")
    voices.remember("Anna Schmidt", ANNA_VOICE)
    rec = make_recording(tmp_path)
    jobs = Jobs(tmp_path / "jobs.db")
    job = run_job(jobs.add(rec), settings_for(tmp_path), jobs, FakeTranscriber(),
                  lambda s: FakeBackend(), voices=voices)
    assert job.state == "done"
    err = capsys.readouterr().err
    assert "Anna" not in err and "Release planning" not in err and str(tmp_path / "Meetings") not in err
    assert "note written" in err


# OpenAI-compatible APIs: OpenRouter, OpenAI, LM Studio ---------------------------

def fake_api(monkeypatch, answers):
    """urlopen for /chat/completions; each answer is the reply's text or an HTTP error code."""
    import urllib.error

    from wirenote.process import summarize as summ

    sent = []

    def urlopen(req, timeout):
        sent.append((req.full_url, dict(req.header_items()), json.loads(req.data)))
        a = answers.pop(0)
        if isinstance(a, int):
            raise urllib.error.HTTPError(req.full_url, a, "x", {}, io.BytesIO(b'{"error": "no"}'))
        return _Resp(json.dumps({"choices": [{"message": {"content": a}}]}).encode())

    def no_dns(*a, **k):
        raise OSError("offline")
    monkeypatch.setattr(summ.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(summ.socket, "getaddrinfo", no_dns)
    monkeypatch.setattr(summ, "listener_uid", lambda port, host="127.0.0.1": None)
    return sent


def test_openrouter_gets_the_schema_and_the_key(monkeypatch):
    from wirenote.process.summarize import OpenAIBackend

    sent = fake_api(monkeypatch, [json.dumps(GOOD)])
    api = OpenAIBackend("https://openrouter.ai/api/v1/", "anthropic/claude-sonnet-5.5", "sk-or-test-key")
    assert api.name == "openrouter.ai:anthropic/claude-sonnet-5.5"
    assert summarize([Line("Me", 0, 1, "x")], api, 900, "auto").title == "Release planning"
    url, headers, body = sent[0]
    assert url == "https://openrouter.ai/api/v1/chat/completions"
    assert headers["Authorization"] == "Bearer sk-or-test-key"
    assert body["model"] == "anthropic/claude-sonnet-5.5"
    assert body["response_format"]["json_schema"]["schema"] == FINAL_SCHEMA


def test_a_server_without_json_schemas_gets_the_schema_in_the_prompt(monkeypatch):
    from wirenote.process.summarize import OpenAIBackend

    sent = fake_api(monkeypatch, [400, "```json\n" + json.dumps(GOOD) + "\n```"])
    api = OpenAIBackend("http://127.0.0.1:1234/v1", "qwen3-8b")       # LM Studio, no key
    assert api.name == "local:qwen3-8b"
    assert api.complete_json("sys", "u", FINAL_SCHEMA)["title"] == "Release planning"
    retry = sent[1][2]
    assert "response_format" not in retry and '"required"' in retry["messages"][0]["content"]
    assert "Authorization" not in sent[0][1]


def test_api_addresses_and_keys_are_checked(monkeypatch):
    import os

    from wirenote.process import summarize as summ
    from wirenote.process.summarize import OpenAIBackend, make_backend

    sent = fake_api(monkeypatch, [401])
    with pytest.raises(SummaryError, match="needs https"):          # the key and transcript in clear
        OpenAIBackend("http://api.example.com/v1", "m", "key-123456").complete_json("s", "u", {})
    with pytest.raises(SummaryError, match="rejected the API key"):
        OpenAIBackend("https://api.example.com/v1", "m", "bad-key-1").complete_json("s", "u", {})
    s = replace(Settings(), summary_backend="openai")
    with pytest.raises(SummaryError, match="no API key for openrouter.ai"):
        make_backend(s)
    assert make_backend(replace(s, openai_url="http://localhost:1234/v1")).name.startswith("local:")
    # Someone else's server on this computer never gets a transcript.
    monkeypatch.setattr(summ, "listener_uid", lambda port, host="127.0.0.1": os.getuid() + 1)
    monkeypatch.setattr(summ, "trusted_owner", lambda uid: uid == os.getuid())
    with pytest.raises(SummaryError, match="another user"):
        OpenAIBackend("http://127.0.0.1:1234/v1", "m").complete_json("s", "u", {})
    assert len(sent) == 1


# Too little memory, a restarting server ----------------------------------------

GB = 10 ** 9


def fake_ollama(monkeypatch, loaded=(), size=2.5 * GB, chat=None):
    """Ollama's /api/ps, /api/tags and /api/chat; chat() answers or raises."""
    from wirenote.process import summarize as summ

    sent = []

    def urlopen(req, timeout):
        path = req.full_url.split("11434", 1)[1]
        sent.append(path)
        if path == "/api/ps":
            body = {"models": [{"name": m} for m in loaded]}
        elif path == "/api/tags":
            body = {"models": [{"name": "llama3:latest", "size": 9 * GB},
                               {"name": "qwen3:4b", "size": int(size)}]}
        else:
            body = chat() if chat else {"message": {"content": "{}"}}
        return _Resp(json.dumps(body).encode())
    monkeypatch.setattr(summ, "listener_uid", lambda port, host="127.0.0.1": None)
    monkeypatch.setattr(summ.urllib.request, "urlopen", urlopen)
    return sent


def test_ollama_waits_for_memory_before_loading_the_model(monkeypatch):
    from wirenote.process import summarize as summ

    sent = fake_ollama(monkeypatch)
    slept = []
    monkeypatch.setattr(summ.time, "sleep", slept.append)
    free = iter([1 * GB, 2 * GB, 4 * GB])            # programs close while it waits
    monkeypatch.setattr(summ, "free_memory", lambda: next(free))
    summ.OllamaBackend("http://127.0.0.1:11434", "qwen3:4b").complete_json("s", "u", {})
    assert slept == [summ.MEMORY_POLL_S] * 2 and sent[-1] == "/api/chat"

    # Never enough: give up after MEMORY_WAIT_S, without loading the model.
    sent.clear()
    monkeypatch.setattr(summ, "free_memory", lambda: 1 * GB)
    with pytest.raises(summ.SummaryUnavailable, match=r"needs about 3.6 GB, 1.0 GB are free"):
        summ.OllamaBackend("http://127.0.0.1:11434", "qwen3:4b").complete_json("s", "u", {})
    assert "/api/chat" not in sent

    # Already loaded: nothing more to make room for.
    fake_ollama(monkeypatch, loaded=["qwen3:4b"])
    summ.OllamaBackend("http://127.0.0.1:11434", "qwen3:4b").complete_json("s", "u", {})


def test_a_killed_or_restarting_ollama_is_a_passing_failure(monkeypatch):
    import urllib.error

    from wirenote.process import summarize as summ

    monkeypatch.setattr(summ, "free_memory", lambda: 99 * GB)
    ollama = summ.OllamaBackend("http://127.0.0.1:11434", "qwen3:4b")

    def raises(e):
        def chat():
            raise e
        return chat
    for e in (ConnectionRefusedError(111, "refused"), ConnectionResetError(104, "reset"),
              urllib.error.HTTPError("u", 500, "llama runner process has terminated", {}, io.BytesIO())):
        fake_ollama(monkeypatch, chat=raises(e))
        with pytest.raises(summ.SummaryUnavailable):
            ollama.complete_json("s", "u", {})
    fake_ollama(monkeypatch, chat=raises(urllib.error.HTTPError("u", 404, "model not found", {}, io.BytesIO())))
    with pytest.raises(SummaryError) as e:
        ollama.complete_json("s", "u", {})
    assert not isinstance(e.value, summ.SummaryUnavailable)    # asking again would not help


def test_a_chunk_is_asked_again_once_after_a_passing_failure(monkeypatch):
    from wirenote.process import summarize as summ

    slept = []
    monkeypatch.setattr(summ.time, "sleep", slept.append)
    down = summ.SummaryUnavailable("Ollama not reachable")
    assert summarize([Line("Me", 0, 1, "x")], FakeBackend([down]), 900, "auto").title
    assert slept == [summ.RETRY_PAUSE_S]
    with pytest.raises(summ.SummaryUnavailable):
        summarize([Line("Me", 0, 1, "x")], FakeBackend([down, down]), 900, "auto")


def test_the_summary_is_queued_again_until_attempts_are_used_up(tmp_path, monkeypatch):
    from wirenote.process import summarize as summ

    monkeypatch.setattr(summ.time, "sleep", lambda s: None)

    class Down(FakeBackend):
        def complete_json(self, system, user, schema):
            raise summ.SummaryUnavailable("not enough free memory for qwen3:4b")

    rec = make_recording(tmp_path)
    jobs = Jobs(tmp_path / "jobs.db")
    tr = FakeTranscriber()
    job_id = jobs.add(rec)
    for attempt in range(1, MAX_ATTEMPTS):
        job = run_job(job_id, settings_for(tmp_path), jobs, tr, lambda s: Down(),
                      voices=no_voices(tmp_path))
        assert (job.state, job.summary_only, job.attempts) == ("queued", True, attempt)
        assert "tries again in a few minutes" in Path(job.note).read_text()   # the transcript is there
    job = run_job(job_id, settings_for(tmp_path), jobs, tr, lambda s: Down(), voices=no_voices(tmp_path))
    assert job.state == "done" and tr.calls == 1                 # transcribed once
    text = Path(job.note).read_text()
    assert "not enough free memory" in text and "tries again" not in text


def test_an_api_that_is_busy_or_down_is_a_passing_failure(monkeypatch):
    from wirenote.process import summarize as summ
    from wirenote.process.summarize import OpenAIBackend

    api = OpenAIBackend("https://openrouter.ai/api/v1", "m", "sk-or-test-key")
    for code in (429, 502):
        fake_api(monkeypatch, [code])
        with pytest.raises(summ.SummaryUnavailable):
            api.complete_json("s", "u", {})
    fake_api(monkeypatch, [404])
    with pytest.raises(SummaryError) as e:
        api.complete_json("s", "u", {})
    assert not isinstance(e.value, summ.SummaryUnavailable)


def test_an_unreachable_api_is_a_passing_failure(monkeypatch):
    from wirenote.process import summarize as summ
    from wirenote.process.summarize import OpenAIBackend

    def refused(req, timeout):
        raise ConnectionRefusedError(111, "refused")
    monkeypatch.setattr(summ.urllib.request, "urlopen", refused)
    monkeypatch.setattr(summ, "listener_uid", lambda port, host="127.0.0.1": None)
    with pytest.raises(summ.SummaryUnavailable):                   # LM Studio not started yet
        OpenAIBackend("http://127.0.0.1:1234/v1", "m").complete_json("s", "u", {})
