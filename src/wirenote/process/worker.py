# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hrishikesh Jadhav
"""One processing job: transcribe, tell speakers apart and name them, summarize, write the note.

Runs as its own process (`wirenote process <job id>`, started by the daemon at
nice 10), so a slow or crashing transcription never stalls detection. A
summary failure still writes the note with the transcript; only a failure
before the note exists counts as a failed attempt. A summary that failed for a
passing reason (SummaryUnavailable: no memory for the model, the server
restarting) is queued again on its own, up to MAX_ATTEMPTS.
"""

from __future__ import annotations

import datetime as dt
import gc
import json
import sys
import traceback
from pathlib import Path

from ..capture.recorder import read_flags, read_manifest, write_manifest
from ..config.settings import Settings
from ..config.vocabulary import load_vocabulary
from .engines import engine_label, with_overrides
from .jobs import MAX_ATTEMPTS, Job, Jobs
from .live import LIVE, prefetch
from .merge import Line, as_text, dedupe_echo, flagged_lines, paragraphs
from .note import action_line, note_path, own_note, render, segment_file, title_for
from .speakers import Voices, display_name, guess_names
from .summarize import Summary, SummaryError, SummaryUnavailable, make_backend, summarize

TRANSCRIPT = "transcript.json"
SUMMARY = "summary.json"
MY_NOTES = "my_notes.md"


def log(msg: str) -> None:
    print(f"worker: {msg}", file=sys.stderr, flush=True)


def run_job(job_id: int, settings: Settings, jobs: Jobs, transcriber=None,
            backend_factory=make_backend, voices: Voices | None = None) -> Job:
    job = jobs.get(job_id)
    if job is None:
        raise SystemExit(f"no job {job_id}")
    try:
        _run(job, settings, jobs, transcriber, backend_factory, voices)
    except Exception:  # noqa: BLE001 - recorded on the job, retried by the daemon
        err = traceback.format_exc()
        log(f"job {job_id} failed:\n{err}")
        return jobs.fail(job_id, err)
    return jobs.get(job_id)


def read_my_notes(rec: Path) -> str:
    try:
        return (rec / MY_NOTES).read_text().strip()
    except FileNotFoundError:
        return ""


def speaker_names(speakers: dict) -> dict[str, str]:
    return {label: display_name(label, speakers) for label in speakers}


def _run(job: Job, settings: Settings, jobs: Jobs, transcriber, backend_factory,
         voices: Voices | None) -> None:
    rec = job.recording
    manifest = read_manifest(rec)
    cache = rec / TRANSCRIPT
    vocabulary = load_vocabulary()
    backend, backend_error = None, None
    try:
        backend = backend_factory(settings)
    except SummaryError as e:
        backend_error = str(e)

    if job.summary_only and cache.exists():
        data = json.loads(cache.read_text())
        lines = [Line(**l) for l in data["lines"]]
        speakers = data.get("speakers", {})
        model = data.get("model") or settings.whisper_model
        log(f"job {job.id}: reusing {cache}")
    else:
        jobs.set_state(job.id, "transcribing")
        heard_with = with_overrides(settings, manifest)
        if transcriber is None:
            from .transcribe import Transcriber
            transcriber = Transcriber(heard_with, vocabulary)
        engine = getattr(transcriber, "engine", None)
        model = engine.label if engine is not None else engine_label(heard_with, vocabulary)
        log(f"job {job.id}: transcribing {rec.name} with {model}")
        raw, runs, book = transcriber.recording(rec, manifest)
        # The speech models hold 1-2 GB; let them go before the summary, when Ollama
        # loads its own model (a 30-min call ran out of memory on a 14 GB laptop).
        transcriber = engine = None
        gc.collect()
        kept, dropped = dedupe_echo(raw, settings.echo_overlap, settings.echo_similarity)
        lines = paragraphs(kept, settings.merge_gap_s)
        speakers = _name_speakers(book, lines, manifest, settings, backend, voices)
        cache.write_text(json.dumps({
            "model": model, "runs": runs,
            "lines": [l.as_dict() for l in lines],
            "speakers": speakers,
            "dropped_echo": [l.as_dict() for l in dropped]}, indent=2, ensure_ascii=False))
        (rec / LIVE).unlink(missing_ok=True)     # the preview is replaced by the transcript
        log(f"job {job.id}: {len(lines)} paragraphs, {len(speakers)} other speaker(s), "
            f"{len(dropped)} echo lines dropped")

    jobs.set_state(job.id, "summarizing")
    names = speaker_names(speakers)
    my_notes = read_my_notes(rec)
    summary, error, backend_name, retry = None, backend_error, None, False
    if backend is not None and lines:
        backend_name = backend.name
        log(f"job {job.id}: summarizing with {backend_name}")
        try:
            summary = summarize(lines, backend, settings.summary_chunk_s, settings.summary_language,
                                context=_context(manifest, vocabulary, my_notes,
                                                 _flagged(lines, read_flags(rec), settings, names)),
                                names=names)
        except SummaryError as e:
            error = str(e)
            log(f"job {job.id}: no summary: {error}")
            retry = isinstance(e, SummaryUnavailable) and job.attempts + 1 < MAX_ATTEMPTS
            if retry:
                error += "; Wirenote tries again in a few minutes"
    if summary:
        (rec / SUMMARY).write_text(json.dumps(summary.as_dict(), indent=2, ensure_ascii=False))

    path = _write_note(rec, manifest, lines, summary, settings, backend_name, error, names, my_notes,
                       model, read_flags(rec))
    manifest.update(note=str(path), summary_error=error, summary_backend=backend_name,
                    note_written=dt.datetime.now().astimezone().isoformat(timespec="seconds"))
    write_manifest(rec, manifest)
    if retry:
        jobs.retry_summary(job.id, error, note=str(path))
        log(f"job {job.id}: note written; summary queued again")
        return
    jobs.set_state(job.id, "done", note=str(path))
    log(f"job {job.id}: note written")   # no path: it carries the title
    if settings.live_transcript:
        try:
            prefetch(settings)
        except Exception as e:  # noqa: BLE001 - the note is done; the next one retries
            log(f"live transcript model not downloaded: {type(e).__name__}")


def _name_speakers(book, lines: list[Line], manifest: dict, settings: Settings, backend,
                   voices: Voices | None) -> dict:
    """{label: {name, source, evidence, seconds, embedding}} for every S label."""
    if book is None or not book.prints:
        return {}
    voices = voices if voices is not None else Voices()
    speakers: dict[str, dict] = {}
    taken: set[str] = set()
    for label, vec in sorted(book.prints.items(), key=lambda kv: int(kv[0][1:])):
        name, score = voices.match(vec, settings.voice_match)
        if name in taken:
            name = None
        speakers[label] = {"name": name, "source": "voice" if name else None,
                           "score": round(score, 2), "evidence": "",
                           "seconds": round(book.seconds.get(label, 0.0), 1),
                           "embedding": [round(float(x), 6) for x in vec]}
        if name:
            taken.add(name)
    open_labels = [l for l, s in speakers.items() if not s["name"]]
    if backend is not None and open_labels and lines:
        attendees = (manifest.get("calendar") or {}).get("attendees") or []
        try:
            guesses = guess_names(as_text(lines), open_labels, attendees, backend)
        except Exception as e:  # noqa: BLE001 - names are a bonus; keep the labels
            log(f"speaker names not guessed: {e!r}")
            guesses = {}
        for label, g in guesses.items():
            if g["name"] not in taken:
                speakers[label].update(name=g["name"], source="transcript", evidence=g["evidence"])
                taken.add(g["name"])
    log("speakers: " + ", ".join(f"{l}={'named' if s['name'] else '?'}({s['source'] or '-'})"
                                 for l, s in speakers.items()))
    return speakers


def _write_note(rec: Path, manifest: dict, lines: list[Line], summary: Summary | None,
                settings: Settings, backend_name: str | None, error: str | None,
                names: dict[str, str], my_notes: str, transcript_model: str,
                flags: list[float]) -> Path:
    title = title_for(manifest, summary)
    started = dt.datetime.fromisoformat(manifest["started"])
    path = note_path(settings.notes_path(), started, title)
    old = own_note(manifest, settings.notes_path())
    if old and old != path and old.exists():
        old.unlink()   # reprocessed with a new title
    segs = manifest.get("segments", [])
    duration = max((s.get("end_offset_s") or 0.0 for s in segs), default=0.0) / 60
    audio = [str(f) for f in (segment_file(rec, s) for s in segs) if f and f.exists()]
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_suffix(".md.tmp")
    tmp.write_text(render(manifest, lines, summary, title=title, duration_min=duration,
                          audio=audio, transcript_model=transcript_model,
                          summary_backend=backend_name, summary_error=error, names=names,
                          my_notes=my_notes, flags=flags))
    tmp.replace(path)
    return path


def rewrite_note(rec: Path, settings: Settings) -> Path | None:
    """Re-render the Markdown note from saved data (after renaming a speaker or editing
    notes), without a model."""
    manifest = read_manifest(rec)
    try:
        data = json.loads((rec / TRANSCRIPT).read_text())
    except (OSError, ValueError):
        return None
    lines = [Line(**l) for l in data["lines"]]
    try:
        s = json.loads((rec / SUMMARY).read_text())
        summary = Summary(s.get("title", ""), s.get("summary", []), s.get("decisions", []), [],
                          s.get("open_questions", []))
        from .summarize import ActionItem
        summary.action_items = [ActionItem(a["owner"], a["task"], a.get("due", ""))
                                for a in s.get("action_items", [])]
        done = [bool(a.get("done")) for a in s.get("action_items", [])]
    except (OSError, ValueError):
        summary, done = None, []
    names = speaker_names(data.get("speakers", {}))
    path = _write_note(rec, manifest, lines, summary, settings, manifest.get("summary_backend"),
                       manifest.get("summary_error"), names, read_my_notes(rec),
                       data.get("model") or settings.whisper_model, read_flags(rec))
    if done and any(done):
        text = path.read_text()
        for a, d in zip(summary.action_items, done):
            if d:
                text = text.replace(action_line(a.owner, a.task, a.due, False),
                                    action_line(a.owner, a.task, a.due, True), 1)
        path.write_text(text)
    manifest["note"] = str(path)
    write_manifest(rec, manifest)
    return path


FLAGGED_CAP = 3000


def _flagged(lines: list[Line], flags: list[float], settings: Settings,
             names: dict[str, str]) -> str:
    """What was said around the moments Me flagged, for the summary prompt."""
    near = flagged_lines(lines, flags, settings.flag_before_s, settings.flag_after_s)
    return as_text([l for _, l in near], names)[:FLAGGED_CAP]


def _context(manifest: dict, vocabulary: list[str], my_notes: str, flagged: str = "") -> str:
    bits = [f"Call started {manifest['started']} in {manifest.get('name') or manifest.get('app')}"]
    if manifest.get("service"):
        room = f", room {manifest['room']}" if manifest.get("room") else ""
        bits.append(f"Service: {manifest['service']}{room}")
    cal = manifest.get("calendar") or {}
    if cal.get("title"):
        bits.append(f"Calendar event: {cal['title']}")
    if cal.get("attendees"):
        bits.append("Invited: " + ", ".join(cal["attendees"]))
    if vocabulary:
        bits.append("Correct spellings of names and terms that may be misheard: "
                    + ", ".join(vocabulary[:80]))
    if my_notes:
        bits.append("Notes the recording person (Me) took during the call. They show what "
                    "mattered to them: keep their points, add the details from the transcript, "
                    "and correct them where the transcript says otherwise:\n" + my_notes[:4000])
    if flagged:
        bits.append("Moments Me flagged as important. Make sure the summary covers them:\n" + flagged)
    return "\n".join(bits)
