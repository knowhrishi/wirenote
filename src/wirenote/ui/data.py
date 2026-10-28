# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hrishikesh Jadhav
"""Read and change past calls for the UI (docs/dev/ui-design.md, docs/dev/features-2-design.md).

Everything comes from what the daemon and worker already write: each
recording's manifest.json, transcript.json and summary.json, the jobs
database and status.json. Nothing here touches a running recording, except
my_notes.md, which the person writes during the call.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

from ..capture.recorder import add_flag, read_flags, read_manifest, write_flags, write_manifest
from ..config.secrets import (calendar_sources, load_secrets, save_secrets, with_config_value,
                              write_config_atomic)
from ..config.settings import DEFAULT_CONFIG_TOML, Settings, config_dir, parse_settings
from ..config.vocabulary import load_vocabulary, save_vocabulary
from ..process.engines import ENGINES, OVERRIDES, check_overrides
from ..process.jobs import Jobs
from ..process.live import LIVE
from ..process.merge import Line, as_text, flag_excerpt, paragraphs, stamp
from ..process.note import action_line, own_note, segment_file, title_for
from ..process.speakers import Voices, display_name
from ..process.summarize import Summary, SummaryError, make_backend, pin_local
from ..process.templates import remember_series, scan, series_templates
from ..process.worker import MY_NOTES, SUMMARY, TRANSCRIPT, read_my_notes, rewrite_note

REC_ID = re.compile(r"^\d{8}-\d{6}-[A-Za-z0-9_.-]{1,64}$")
LABEL = re.compile(r"^(S\d{1,3}|Others)$")
MIX = "mix.ogg"
ASKS = "asks.json"
FOLLOWUP = "followup.json"
_WORD = re.compile(r"\w+", re.UNICODE)
# Ask: how much transcript a local model gets (its context is 8k tokens).
OLLAMA_ASK_CHARS = 18000
LOCAL_BACKENDS = ("ollama:", "local:")      # Backend.name prefixes of models on this computer
ALL_CALLS_CHARS_API = 120000

ASK_SYSTEM = """You answer questions about one call, using only its transcript.
"Me" is the person asking you; other speakers appear by name or as "Speaker N".
If the transcript does not contain the answer, say so plainly.
Answer briefly, in the language of the question.
citations: the [HH:MM:SS] timestamps of the transcript lines your answer rests on, \
at most 5, as "HH:MM:SS"."""

ASK_SCHEMA = {"type": "object",
              "properties": {"answer": {"type": "string"},
                             "citations": {"type": "array", "items": {"type": "string"}}},
              "required": ["answer", "citations"], "additionalProperties": False}

ASK_ALL_SYSTEM = """You answer questions about the person's past calls, using only the \
excerpts given. Each call has an id like C3; each excerpt line starts with [C3 HH:MM:SS].
"Me" is the person asking you; other speakers appear by name or as "Speaker N".
If the excerpts do not contain the answer, say so plainly. Mention which call (title, \
date) an answer comes from. Answer briefly, in the language of the question.
citations: the labels of the lines your answer rests on, at most 6, as "C3 HH:MM:SS"."""

FOLLOWUP_SYSTEM = """You write a short follow-up email after a call, from its notes.
Write it in the language of the notes, friendly and plain, as the person who \
recorded the call ("Me" in the notes means the writer, so write "I").
Structure: greeting; one or two sentences on what the call covered; decisions; \
action items with owner and due date where known; a short closing line such as "Best,".
Do not add a name or "Me" after the closing. Do not invent anything that is not in \
the notes, in particular no deadlines that are not written there.
subject: a short subject line."""

# Settings the UI may change; everything else stays
# in config.toml.
EDITABLE = ("consent_line", "summary_backend", "ollama_model", "anthropic_model",
            "openai_url", "openai_model",
            "whisper_model", "whisper_language", "summary_language", "asr_engine",
            "live_transcript", "default_template", "global_shortcuts", "launcher_search")
# The EDITABLE settings that are true or false
SWITCHES = ("live_transcript", "global_shortcuts", "launcher_search")
LIVE_READ = 256 * 1024             # most of live.jsonl sent per poll
BACKENDS = ("ollama", "anthropic", "openai", "none")
API_KEYS = {"anthropic": "anthropic_api_key", "openai": "openai_api_key"}   # secrets.toml
UPDATE_UNIT = "wirenote-update"
MAX_SETTING = 500
CALENDAR_LINK = re.compile(r"^(https|webcal)://\S{1,2000}$", re.IGNORECASE)

CLOUD_DISCLOSURE = ("With Claude or an online API on, these go to Anthropic, or to the API you "
                    "set (for OpenRouter: OpenRouter and the model's provider): the transcript "
                    "of each call (in parts), the calendar title and invitees, your notes, your "
                    "vocabulary, questions you ask with the transcript, and for Ask across "
                    "meetings up to 120,000 characters from all your calls.")

UPDATE_DISCLOSURE = ("Once a day, Wirenote asks pypi.org for the newest version and, when there "
                     "is one, fetches its release notes from github.com. Nothing about you or "
                     "your calls is sent, but those sites see your IP address.")

FOLLOWUP_SCHEMA = {"type": "object",
                   "properties": {"subject": {"type": "string"}, "body": {"type": "string"}},
                   "required": ["subject", "body"], "additionalProperties": False}


MAX_FLAG_S = 24 * 3600      # docs/dev/PLAN.md › Defaults › Flag a finished call


class NotFound(Exception):
    pass


class Invalid(Exception):
    pass


def _read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def _write_json(path: Path, data) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    tmp.replace(path)


def _words(text: str) -> set[str]:
    return {w for w in _WORD.findall(text.lower()) if len(w) > 2}


def _stems(text: str) -> set[str]:
    """Words cut to 5 letters, so German inflections (Release/Releases) still match."""
    return {w[:5] for w in _words(text)}


def poke_daemon(command: str) -> None:
    """Tell the daemon something changed; it is fine if it is not running."""
    from ..tools.setup import _poke_daemon

    _poke_daemon(command)


def _calendar_id(source: str) -> str:
    return hashlib.sha256(source.encode()).hexdigest()[:12]


def _destination(s: Settings) -> str | None:
    """Who gets call data with these settings: None while it stays on this computer."""
    if s.summary_backend == "anthropic":
        return "Claude"
    if s.summary_backend == "openai" and not pin_local(s.openai_url):
        from urllib.parse import urlsplit

        return urlsplit(s.openai_url).hostname or s.openai_url
    return None


def _secrets() -> dict:
    try:
        return load_secrets()
    except ValueError as e:
        raise Invalid("secrets.toml cannot be read; fix or delete it") from e


def _now() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


class Store:
    def __init__(self, settings: Settings, state_dir: Path, voices: Voices | None = None) -> None:
        self.settings = settings
        self.root = settings.recordings_path()
        self.state_dir = state_dir
        self.jobs = Jobs(state_dir / "jobs.db")
        self.voices = voices or Voices()
        self._jobs_lock = threading.Lock()
        self._mix_locks: dict[str, threading.Lock] = {}
        self._upcoming: tuple[float, list] = (0.0, [])
        # The launcher's quick search: per call, what it matches, keyed by file times
        self._quick: dict[str, tuple[tuple, dict]] = {}
        self._quick_speakers: dict[str, tuple[int | None, list[str]]] = {}

    # Paths -----------------------------------------------------------------

    def rec_dir(self, rec_id: str) -> Path:
        if not REC_ID.match(rec_id or ""):
            raise NotFound(rec_id)
        d = self.root / rec_id
        if not (d / "manifest.json").is_file():
            raise NotFound(rec_id)
        return d

    def _note(self, manifest: dict) -> Path | None:
        return own_note(manifest, self.settings.notes_path())

    def _rec_dirs(self) -> list[Path]:
        if not self.root.is_dir():
            return []
        return [d for d in sorted(self.root.iterdir(), reverse=True)
                if (d / "manifest.json").is_file()]

    # Status ----------------------------------------------------------------

    def status(self) -> dict:
        st = _read_json(self.state_dir / "status.json", {}) or {}
        pid = st.get("pid")
        st["alive"] = bool(st.get("running") and pid and Path(f"/proc/{pid}").exists())
        return st

    # Listing ---------------------------------------------------------------

    def _job_state(self, d: Path) -> tuple[str | None, str | None]:
        with self._jobs_lock:
            job = self.jobs.by_recording(d)
        if job is None:
            return None, None
        return job.state, (job.error.strip().splitlines()[-1] if job.error else None)

    def _summary(self, d: Path) -> dict | None:
        return _read_json(d / SUMMARY)

    def _transcript(self, d: Path) -> dict:
        return _read_json(d / TRANSCRIPT, {}) or {}

    def _title(self, m: dict, summary: dict | None) -> str:
        if m.get("title_override"):
            return m["title_override"]
        return title_for(m, Summary(title=(summary or {}).get("title", "")))

    def _brief(self, d: Path, recording_id: str | None) -> dict:
        m = read_manifest(d)
        s = self._summary(d)
        state, error = self._job_state(d)
        if recording_id == m["id"]:
            state = "recording"
        segs = m.get("segments", [])
        duration = max((x.get("end_offset_s") or 0.0 for x in segs), default=0.0)
        actions = (s or {}).get("action_items", [])
        cal = m.get("calendar") or {}
        speakers = self._transcript(d).get("speakers", {})
        return {"id": m["id"], "title": self._title(m, s), "started": m["started"],
                "duration_s": round(duration), "app": m.get("app"), "name": m.get("name"),
                "service": m.get("service"), "room": m.get("room"),
                "calendar": cal.get("title"), "attendees": cal.get("attendees") or [],
                "emails": cal.get("emails") or [],
                "speakers": [display_name(l, speakers) for l in sorted(speakers)],
                "state": state, "error": error, "has_summary": s is not None,
                "has_transcript": (d / TRANSCRIPT).exists(),
                "summary_error": m.get("summary_error"),
                "summary_backend": m.get("summary_backend"),
                "actions": len(actions),
                "open_actions": sum(1 for a in actions if not a.get("done")),
                "my_open_actions": sum(1 for a in actions
                                       if not a.get("done") and a.get("owner") == "Me"),
                "preview": ((s or {}).get("summary") or [""])[0]}

    def calls(self, query: str = "", deep: bool = True, limit: int | None = None) -> list[dict]:
        """Every call, newest first; with a query, those that match it.

        deep=False is the launcher's search: title, people and summary only, never your notes
        or the transcript, stopping after limit hits. Its results are short ({id, title,
        started, attendees, speakers, match}), and what it reads is kept until the files
        change, so the next key press costs a few stat calls per meeting."""
        q = query.strip().lower()
        if q and not deep:
            return self._quick_calls(q, limit)
        recording_id = self.status().get("recording_id")
        q = query.strip().lower()
        out = []
        for d in self._rec_dirs():
            try:
                brief = self._brief(d, recording_id)
                if q:
                    hit = self._search(d, brief, q)
                    if hit is None:
                        continue
                    brief["match"] = hit
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                continue        # a broken call folder hides that call, not the list
            out.append(brief)
            if limit is not None and len(out) >= limit:
                break
        return out

    def quick_brief(self, rec_id: str) -> dict:
        """One call as the launcher shows it; NotFound for an id that is not a call."""
        d = self.rec_dir(rec_id)
        try:
            entry = dict(self._quick_entry(d))
            entry["speakers"] = self._quick_people(d)
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
            raise NotFound(rec_id) from e
        return entry

    def _quick_calls(self, q: str, limit: int | None) -> list[dict]:
        out = []
        for d in self._rec_dirs():
            try:
                entry = self._quick_entry(d)
                hit = _quick_hit(entry, q)
                if hit is None:
                    # Speaker names come from transcript.json: the dearest file, read last
                    hit = next(({"where": "people", "text": n} for n in self._quick_people(d)
                                if q in n.lower()), None)
                if hit is None:
                    continue
                found = {k: entry[k] for k in ("id", "title", "started", "attendees")}
                found["speakers"] = [] if entry["attendees"] else self._quick_people(d)
                found["match"] = hit
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                continue        # a broken call folder hides that call, not the list
            out.append(found)
            if limit is not None and len(out) >= limit:
                break
        return out

    def _quick_entry(self, d: Path) -> dict:
        """Title, start, invitees and summary text of one call, read once per change."""
        key = tuple(_mtime_ns(d / name) for name in ("manifest.json", SUMMARY))
        cached = self._quick.get(d.name)
        if cached is not None and cached[0] == key:
            return cached[1]
        m = _read_json(d / "manifest.json")
        if not isinstance(m, dict):
            raise ValueError("bad manifest")
        s = _read_json(d / SUMMARY)
        s = s if isinstance(s, dict) else None
        cal = m.get("calendar") or {}
        texts = [item for key_ in ("summary", "decisions", "open_questions")
                 for item in (s or {}).get(key_) or []]
        texts += [item for items in ((s or {}).get("sections") or {}).values() for item in items or []]
        texts += [a.get("task") for a in (s or {}).get("action_items") or [] if isinstance(a, dict)]
        entry = {"id": m["id"], "title": self._title(m, s), "started": m.get("started"),
                 "attendees": [a for a in cal.get("attendees") or [] if isinstance(a, str)],
                 "texts": [t for t in texts if isinstance(t, str)]}
        self._quick[d.name] = (key, entry)
        return entry

    def _quick_people(self, d: Path) -> list[str]:
        key = _mtime_ns(d / TRANSCRIPT)
        cached = self._quick_speakers.get(d.name)
        if cached is not None and cached[0] == key:
            return cached[1]
        tr = _read_json(d / TRANSCRIPT, {}) if key is not None else {}
        speakers = tr.get("speakers") if isinstance(tr, dict) else None
        names = [display_name(l, speakers) for l in sorted(speakers)] if isinstance(speakers, dict) else []
        names = [n for n in names if isinstance(n, str)]
        self._quick_speakers[d.name] = (key, names)
        return names

    def _search(self, d: Path, brief: dict, q: str) -> dict | None:
        if q in brief["title"].lower():
            return {"where": "title", "text": brief["title"]}
        for name in brief["speakers"] + brief["attendees"]:
            if q in name.lower():
                return {"where": "people", "text": name}
        s = self._summary(d) or {}
        for key, items in (("summary", s.get("summary", [])), ("decisions", s.get("decisions", [])),
                           ("open_questions", s.get("open_questions", [])),
                           *(s.get("sections") or {}).items()):
            for item in items or []:
                if q in item.lower():
                    return {"where": key, "text": item}
        for a in s.get("action_items", []):
            if q in a.get("task", "").lower():
                return {"where": "action_items", "text": a["task"]}
        if q in read_my_notes(d).lower():
            return {"where": "my_notes", "text": read_my_notes(d)[:140]}
        for line in self._transcript(d).get("lines", []):
            text = line.get("text", "")
            i = text.lower().find(q)
            if i >= 0:
                lo, hi = max(0, i - 60), min(len(text), i + len(q) + 80)
                snippet = ("…" if lo else "") + text[lo:hi] + ("…" if hi < len(text) else "")
                return {"where": "transcript", "text": snippet, "start": line.get("start"),
                        "speaker": line.get("speaker")}
        return None

    # One call --------------------------------------------------------------

    def call(self, rec_id: str) -> dict:
        d = self.rec_dir(rec_id)
        brief = self._brief(d, self.status().get("recording_id"))
        m = read_manifest(d)
        tr = self._transcript(d)
        speakers = tr.get("speakers", {})
        note = self._note(m)
        has_audio = any(d.glob("*.wav"))
        lines = tr.get("lines", [])
        summary = self._summary(d)
        used = (summary or {}).get("template") or "general"
        template = scan()[0].get(used)
        uid = (m.get("calendar") or {}).get("uid")
        talk: dict[str, float] = {}
        for l in lines:
            talk[l["speaker"]] = talk.get(l["speaker"], 0.0) + max(0.0, l["end"] - l["start"])
        people = [{"label": label, "name": display_name(label, speakers),
                   "named": bool((speakers.get(label) or {}).get("name")),
                   "source": (speakers.get(label) or {}).get("source"),
                   "evidence": (speakers.get(label) or {}).get("evidence", ""),
                   "seconds": round(talk.get(label, 0.0)),
                   "can_remember": bool((speakers.get(label) or {}).get("embedding"))}
                  for label in sorted(talk, key=lambda l: (l != "Me", -talk[l]))]
        return {**brief,
                "summary": summary,
                "template": used,
                "templates": self.templates(),
                "section_titles": {s.key: s.title for s in template.sections} if template else {},
                "chosen_template": m.get("template"),
                "in_series": bool(uid),
                "series_template": series_templates(self.state_dir).get(str(uid)) if uid else None,
                "lines": lines,
                "people": people,
                "names": {p["label"]: p["name"] for p in people},
                "runs": tr.get("runs", []),
                "echo_dropped": len(tr.get("dropped_echo", [])),
                "transcript_model": tr.get("model"),
                "transcribe": {**{k: getattr(self.settings, v) for k, v in OVERRIDES.items()},
                               **(m.get("transcribe") or {})},
                "note": str(note) if note and note.exists() else None,
                "has_audio": has_audio,
                "audio_deleted": m.get("audio_deleted"),
                "end_reason": m.get("end_reason"),
                "my_notes": read_my_notes(d),
                "flags": self._flags(d, lines, speakers),
                "followup": _read_json(d / FOLLOWUP),
                "asks": _read_json(d / ASKS, []) or [],
                "known_voices": [v["name"] for v in self.voices.names()]}

    def _flags(self, d: Path, lines: list[dict], speakers: dict) -> list[dict]:
        """Each flag with what was said at it, as in the note (process/merge.py
        flag_excerpt; None before there is a transcript)."""
        found = [Line(**l) for l in lines]
        out = []
        for t in read_flags(d):
            line = flag_excerpt(found, t, self.settings.flag_before_s, self.settings.flag_after_s)
            out.append({"t": t, "start": line.start if line else t,
                        "speaker": line.speaker if line else None,
                        "name": display_name(line.speaker, speakers) if line else None,
                        "text": line.text if line else ""})
        return out

    def control(self, command: str) -> dict:
        """Send the daemon a command (flag); its reply, or an error if it is not running."""
        from ..tools.setup import control

        return control(command)

    def add_flag(self, rec_id: str, t: float) -> list[float]:
        """Flag a moment of a finished call (a line of the transcript, an imported call).
        While the call records the daemon owns flags.json, so the app flags through it."""
        d = self.rec_dir(rec_id)
        if isinstance(t, bool) or not isinstance(t, (int, float)) or not 0 <= t <= MAX_FLAG_S:
            raise Invalid("bad time")
        if self.status().get("recording_id") == rec_id:
            raise Invalid("This call is still recording. Flag it with Flag this moment.")
        m = read_manifest(d)
        length = max((x.get("end_offset_s") or 0.0 for x in m.get("segments", [])), default=0.0)
        if length > 0:
            t = min(t, length)       # a flag after the end is about the end
        flags = read_flags(d)
        added = add_flag(flags, round(float(t), 1))
        if added is None:
            return flags
        write_flags(d, added)
        state, _ = self._job_state(d)
        if state == "done" and read_manifest(d).get("note"):
            rewrite_note(d, self.settings)
        return added

    def rename(self, rec_id: str, title: str) -> None:
        d = self.rec_dir(rec_id)
        title = title.strip()[:120]
        m = read_manifest(d)
        if title:
            m["title_override"] = title
        else:
            m.pop("title_override", None)
        write_manifest(d, m)

    def set_action_done(self, rec_id: str, index: int, done: bool) -> None:
        d = self.rec_dir(rec_id)
        s = self._summary(d)
        if not s or not 0 <= index < len(s.get("action_items", [])):
            raise Invalid("no such action item")
        item = s["action_items"][index]
        item["done"] = bool(done)
        _write_json(d / SUMMARY, s)
        note = self._note(read_manifest(d))
        if note and note.exists():
            _tick_in_note(note, item, done)

    def live(self, rec_id: str, after: int = 0) -> dict:
        """Live transcript lines written since byte `after` of live.jsonl (process/live.py).
        Starts over from 0 when the file is shorter than `after` (a new recording)."""
        path = self.rec_dir(rec_id) / LIVE
        out = {"enabled": self.settings.live_transcript, "status": None, "lines": [],
               "after": 0}
        try:
            with path.open("rb") as f:
                size = f.seek(0, 2)
                after = after if 0 <= after <= size else 0
                f.seek(after)
                chunk = f.read(LIVE_READ)
        except OSError:
            return out
        end = chunk.rfind(b"\n") + 1         # only whole lines; the rest comes next poll
        for raw in chunk[:end].splitlines():
            try:
                item = json.loads(raw)
            except ValueError:
                continue
            if "status" in item:
                out["status"] = {k: item.get(k) for k in ("status", "engine", "reason")}
            elif {"speaker", "start", "end", "text"} <= item.keys():
                out["lines"].append({k: item[k] for k in ("speaker", "start", "end", "text")})
        out["after"] = after + end
        return out

    def save_my_notes(self, rec_id: str, text: str, base: str | None = None) -> None:
        """base: the notes the editor started from. If they changed since (another tab,
        e.g. the one opened when the recording stopped), nothing is overwritten."""
        d = self.rec_dir(rec_id)
        if base is not None and base.strip() != read_my_notes(d):
            raise Invalid("These notes changed in another tab. Reload this page to see them.")
        text = text[:20000]
        (d / MY_NOTES).write_text(text)
        state, _ = self._job_state(d)
        if state == "done" and read_manifest(d).get("note"):
            rewrite_note(d, self.settings)

    def rename_speaker(self, rec_id: str, label: str, name: str = "", remember: bool = False,
                       merge_into: str = "") -> None:
        """Name a speaker (and optionally remember the voice), or merge two that are one person."""
        d = self.rec_dir(rec_id)
        if not LABEL.match(label or ""):
            raise Invalid("bad speaker")
        tr = self._transcript(d)
        if not tr:
            raise Invalid("no transcript yet")
        speakers = tr.setdefault("speakers", {})
        lines = [Line(**l) for l in tr.get("lines", [])]
        if merge_into:
            if not LABEL.match(merge_into) or merge_into == label:
                raise Invalid("bad merge target")
            lines = paragraphs([Line(merge_into if l.speaker == label else l.speaker,
                                     l.start, l.end, l.text) for l in lines],
                               self.settings.merge_gap_s)
            gone = speakers.pop(label, None) or {}
            target = speakers.setdefault(merge_into, {"name": None, "source": None})
            target["seconds"] = round((target.get("seconds") or 0) + (gone.get("seconds") or 0), 1)
            if not target.get("name") and gone.get("name"):
                target.update(name=gone["name"], source=gone.get("source"))
            tr["lines"] = [l.as_dict() for l in lines]
        else:
            name = name.strip()[:80]
            info = speakers.setdefault(label, {})
            for other, o in speakers.items():
                if other != label and name and (o.get("name") or "").lower() == name.lower():
                    raise Invalid(f"{name} is already {display_name(other, speakers)}; "
                                  "merge the two instead")
            info.update(name=name or None, source="you" if name else None, evidence="")
            if remember and name and info.get("embedding"):
                self.voices.remember(name, info["embedding"])
        _write_json(d / TRANSCRIPT, tr)
        if read_manifest(d).get("note"):
            rewrite_note(d, self.settings)

    # Import and export ---------------------------------------------------------

    def import_upload(self, read, size: int, filename: str, modified_ms: str = "") -> dict:
        """An uploaded file, read in pieces with read(n), becomes a recording and a job.
        The upload is kept only until ffmpeg has converted it."""
        import datetime as dt
        import tempfile

        from ..capture.importer import MAX_IMPORT_BYTES, ImportFailed, import_audio

        if not 0 < size <= MAX_IMPORT_BYTES:
            raise Invalid(f"files up to {MAX_IMPORT_BYTES // 1024 ** 3} GB can be imported")
        name = Path(filename.replace("\\", "/")).name[:200] or "upload"
        started = None
        if modified_ms.isdigit():
            try:
                started = dt.datetime.fromtimestamp(int(modified_ms) / 1000).astimezone()
            except (OverflowError, OSError, ValueError):
                started = None
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".import-", dir=self.root) as tmp:
            src = Path(tmp) / name
            left = size
            with src.open("wb") as f:
                while left:
                    chunk = read(min(left, 1 << 20))
                    if not chunk:
                        raise Invalid("the upload stopped before the end")
                    f.write(chunk)
                    left -= len(chunk)
            try:
                rec = import_audio(src, self.root, started=started)
            except ImportFailed as e:
                raise Invalid(str(e)) from e
        with self._jobs_lock:
            job = self.jobs.add(rec)
        poke_daemon("kick-jobs")
        return {"id": rec.name, "job": job}

    def export_docx(self, rec_id: str) -> tuple[bytes, str]:
        from .export import docx_bytes, export_name

        call = self.call(rec_id)
        return docx_bytes(call), export_name(call, "docx")

    # Processing ------------------------------------------------------------

    def templates(self) -> list[dict]:
        """The summary templates to choose from, built-in first (a bad file is left out)."""
        return [{"id": t.id, "name": t.name} for t in scan()[0].values()]

    def requeue(self, rec_id: str, summary_only: bool, choices: dict | None = None,
                template: str | None = None, series: bool = False) -> int:
        """Queue the call again; choices (engine, model, language) apply to this call's
        transcriptions from now on, and an empty one goes back to the setting. template
        sets the summary template for this call ("" goes back to automatic); series also
        remembers it for the calendar event's later occurrences, and with "" forgets it."""
        d = self.rec_dir(rec_id)
        if template and template not in scan()[0]:
            raise Invalid("no such summary template")
        uid = (read_manifest(d).get("calendar") or {}).get("uid")
        if series and (template is None or not uid):
            raise Invalid("a template can be kept for a series only for a call from a "
                          "calendar event, and needs a template or automatic")
        if not summary_only and not any(d.glob("*.wav")):
            raise Invalid("the audio was deleted; only the summary can be redone")
        if summary_only and not (d / TRANSCRIPT).exists():
            raise Invalid("no transcript yet")
        if choices is not None and not summary_only:
            if not isinstance(choices, dict):
                raise Invalid("choices must be an object")
            try:
                kept = check_overrides(choices)
            except ValueError as e:
                raise Invalid(str(e)) from e
            m = read_manifest(d)
            defaults = {k: getattr(self.settings, v) for k, v in OVERRIDES.items()}
            m["transcribe"] = {k: v for k, v in kept.items() if v != defaults[k]}
            write_manifest(d, m)
        if template is not None:
            m = read_manifest(d)
            if template:
                m["template"] = template
            else:
                m.pop("template", None)
            write_manifest(d, m)
            if series:
                remember_series(self.state_dir, str(uid), template)
        with self._jobs_lock:
            job = self.jobs.add(d, summary_only=summary_only)
        poke_daemon("kick-jobs")
        return job

    def _backend(self):
        try:
            backend = make_backend(self.settings)
        except SummaryError as e:
            raise Invalid(str(e)) from e
        if backend is None:
            raise Invalid('no AI backend (summary_backend = "none")')
        return backend

    def _complete(self, backend, system: str, user: str, schema: dict) -> dict:
        try:
            return backend.complete_json(system, user, schema)
        except SummaryError as e:
            raise Invalid(str(e)) from e
        except (ValueError, KeyError) as e:
            raise Invalid(f"{backend.name} gave an unusable answer: {e}") from e

    def ask(self, rec_id: str, question: str) -> dict:
        question = question.strip()
        if not question:
            raise Invalid("empty question")
        d = self.rec_dir(rec_id)
        tr = self._transcript(d)
        lines = [Line(**l) for l in tr.get("lines", [])]
        if not lines:
            raise Invalid("no transcript yet")
        backend = self._backend()
        names = {l: display_name(l, tr.get("speakers", {})) for l in tr.get("speakers", {})}
        limit = OLLAMA_ASK_CHARS if backend.name.startswith(LOCAL_BACKENDS) else None
        excerpt, partial = _relevant(lines, question, limit, names)
        head = "Transcript excerpts (most relevant parts):" if partial else "Transcript:"
        data = self._complete(backend, ASK_SYSTEM, f"{head}\n{excerpt}\n\nQuestion: {question}",
                              ASK_SCHEMA)
        cites = [c for c in data.get("citations", []) if re.match(r"^\d\d:\d\d:\d\d$", str(c))]
        entry = {"t": _now(), "question": question, "answer": str(data.get("answer", "")).strip(),
                 "citations": [_seconds(c) for c in cites][:5], "backend": backend.name,
                 "partial": partial}
        asks = _read_json(d / ASKS, []) or []
        asks.append(entry)
        _write_json(d / ASKS, asks[-50:])
        return entry

    def followup(self, rec_id: str) -> dict:
        d = self.rec_dir(rec_id)
        s = self._summary(d)
        if not s:
            raise Invalid("no summary yet; the follow-up is written from it")
        m = read_manifest(d)
        backend = self._backend()
        notes = [f"Call: {self._title(m, s)} ({m['started'][:16].replace('T', ' ')})"]
        cal = m.get("calendar") or {}
        if cal.get("attendees"):
            notes.append("Invited: " + ", ".join(cal["attendees"]))
        notes.append("Summary:\n" + "\n".join(f"- {x}" for x in s.get("summary", [])))
        template = scan()[0].get(s.get("template") or "general")
        for key, items in (s.get("sections") or {}).items():
            if items:
                head = next((x.title for x in template.sections if x.key == key), None) \
                    if template else None
                notes.append(f"{head or key.replace('_', ' ').capitalize()}:\n"
                             + "\n".join(f"- {x}" for x in items))
        if s.get("decisions"):
            notes.append("Decisions:\n" + "\n".join(f"- {x}" for x in s["decisions"]))
        if s.get("action_items"):
            notes.append("Action items:\n" + "\n".join(
                f"- {a['owner']}: {a['task']}"
                + (f" (due {a['due']})" if a.get("due") else " (no deadline given)")
                for a in s["action_items"]))
        if s.get("open_questions"):
            notes.append("Open questions:\n" + "\n".join(f"- {x}" for x in s["open_questions"]))
        if read_my_notes(d):
            notes.append("My own notes:\n" + read_my_notes(d)[:3000])
        data = self._complete(backend, FOLLOWUP_SYSTEM, "\n\n".join(notes), FOLLOWUP_SCHEMA)
        body = re.sub(r"\n+\s*(Me|Ich|I)\s*$", "", str(data.get("body", "")).strip())
        entry = {"t": _now(), "subject": str(data.get("subject", "")).strip(),
                 "body": body, "to": cal.get("emails") or [],
                 "backend": backend.name}
        _write_json(d / FOLLOWUP, entry)
        return entry

    # Across calls ----------------------------------------------------------

    def action_items(self) -> list[dict]:
        out = []
        for d in self._rec_dirs():
            s = self._summary(d)
            if not s:
                continue
            m = read_manifest(d)
            title = self._title(m, s)
            for i, a in enumerate(s.get("action_items", [])):
                out.append({"call": m["id"], "title": title, "started": m["started"], "index": i,
                            "owner": a.get("owner", ""), "task": a.get("task", ""),
                            "due": a.get("due", ""), "done": bool(a.get("done"))})
        return out

    def ask_all(self, question: str) -> dict:
        question = question.strip()
        if not question:
            raise Invalid("empty question")
        backend = self._backend()
        budget = OLLAMA_ASK_CHARS if backend.name.startswith(LOCAL_BACKENDS) else ALL_CALLS_CHARS_API
        q = _stems(question)
        calls, scored = [], []
        for d in self._rec_dirs():
            tr = self._transcript(d)
            if not tr.get("lines"):
                continue
            m = read_manifest(d)
            s = self._summary(d)
            idx = len(calls)
            names = {l: display_name(l, tr.get("speakers", {})) for l in tr.get("speakers", {})}
            lines = [Line(**l) for l in tr["lines"]]
            calls.append({"id": m["id"], "title": self._title(m, s), "started": m["started"],
                          "lines": lines, "names": names})
            topic = _stems(self._title(m, s) + " " + " ".join((s or {}).get("summary", [])))
            bonus = 0.5 * len(q & topic)
            for i, l in enumerate(lines):
                hits = len(q & _stems(l.text))
                if hits:
                    scored.append(((hits + bonus) / math.sqrt(len(_words(l.text)) + 4), idx, i))
        if not calls:
            raise Invalid("no transcripts yet")
        scored.sort(reverse=True)
        keep: dict[int, set[int]] = {}
        size = 0
        overview = "\n".join(f"C{i + 1} = {c['title']} ({c['started'][:16].replace('T', ' ')})"
                             for i, c in enumerate(calls[:60]))
        size += len(overview)
        for _, ci, li in scored:
            for j in (li - 1, li, li + 1):
                if 0 <= j < len(calls[ci]["lines"]) and j not in keep.get(ci, set()):
                    cost = len(calls[ci]["lines"][j].text) + 40
                    if size + cost > budget:
                        break
                    keep.setdefault(ci, set()).add(j)
                    size += cost
            if size >= budget * 0.95:
                break
        parts = [f"Calls:\n{overview}"]
        for ci in sorted(keep):
            c = calls[ci]
            body = "\n".join(
                f"[C{ci + 1} {stamp(c['lines'][j].start)}] "
                f"{c['names'].get(c['lines'][j].speaker, c['lines'][j].speaker)}: {c['lines'][j].text}"
                for j in sorted(keep[ci]))
            parts.append(f"C{ci + 1} {c['title']}:\n{body}")
        if not keep:
            parts.append("(No transcript line matched the question's words.)")
        data = self._complete(backend, ASK_ALL_SYSTEM,
                              "\n\n".join(parts) + f"\n\nQuestion: {question}", ASK_SCHEMA)
        cites = []
        for c in data.get("citations", []):
            mt = re.match(r"^C(\d+)\s+(\d\d:\d\d:\d\d)$", str(c).strip())
            if mt and 1 <= int(mt.group(1)) <= len(calls):
                call = calls[int(mt.group(1)) - 1]
                cites.append({"call": call["id"], "title": call["title"],
                              "t": _seconds(mt.group(2))})
        entry = {"t": _now(), "question": question, "answer": str(data.get("answer", "")).strip(),
                 "citations": cites[:6], "backend": backend.name,
                 "searched": len(calls), "used": len(keep)}
        path = self.state_dir / ASKS
        asks = _read_json(path, []) or []
        asks.append(entry)
        _write_json(path, asks[-100:])
        return entry

    def asks(self) -> list[dict]:
        return _read_json(self.state_dir / ASKS, []) or []

    # Settings --------------------------------------------------------------

    def settings_info(self) -> dict:
        s = self.settings
        try:
            secrets = load_secrets()
        except ValueError:
            secrets = {}
        models = {"ollama": s.ollama_model, "anthropic": s.anthropic_model, "openai": s.openai_model}
        return {"summary_backend": s.summary_backend,
                "model": models.get(s.summary_backend, ""),
                "whisper_model": s.whisper_model, "diarize": s.diarize,
                "audio_retention_days": s.audio_retention_days,
                "notes_dir": str(s.notes_path()),
                "vocabulary": load_vocabulary(), "voices": self.voices.names(),
                "calendars": self.calendars(),
                "editable": {k: getattr(s, k) for k in EDITABLE},
                "templates": self.templates(),
                "anthropic_key_set": bool(secrets.get("anthropic_api_key")
                                          or os.environ.get("ANTHROPIC_API_KEY")),
                "openai_key_set": bool(secrets.get("openai_api_key")),
                "cloud_disclosure": CLOUD_DISCLOSURE}

    def update_settings(self, changes: dict, confirm_cloud: bool) -> dict:
        """Change some of EDITABLE in config.toml; the file is untouched unless all are valid."""
        if not isinstance(changes, dict) or not changes:
            raise Invalid("nothing to change")
        unknown = sorted(set(changes) - set(EDITABLE))
        if unknown:
            raise Invalid(f"cannot change {', '.join(unknown)} here")
        for k, v in changes.items():
            if k in SWITCHES:
                if not isinstance(v, bool):
                    raise Invalid(f"{k}: true or false")
                continue
            if not isinstance(v, str) or len(v) > MAX_SETTING:
                raise Invalid(f"{k}: expected text of at most {MAX_SETTING} characters")
            if k != "consent_line" and not v.isprintable():
                raise Invalid(f"{k}: one line of plain text")
        if "asr_engine" in changes and changes["asr_engine"] not in ENGINES:
            raise Invalid(f"asr_engine: one of {', '.join(ENGINES)}")
        if "default_template" in changes and changes["default_template"] not in scan()[0]:
            raise Invalid("default_template: one of " + ", ".join(scan()[0]))
        backend = changes.get("summary_backend")
        if backend is not None and backend not in BACKENDS:
            raise Invalid(f"summary_backend: one of {', '.join(BACKENDS)}")
        url = changes.get("openai_url")
        if url is not None and not (url.startswith("https://") or
                                    (url.startswith("http://") and pin_local(url))):
            raise Invalid("openai_url: https://…, or http://… on this computer")
        before = _destination(self.settings)
        after = _destination(dataclasses.replace(self.settings, **{
            k: v for k, v in changes.items() if k in ("summary_backend", "openai_url")}))
        if after and after != before and not confirm_cloud:
            raise Invalid(f"confirm what is sent to {after} first")
        path = config_dir() / "config.toml"
        text = path.read_text() if path.exists() else DEFAULT_CONFIG_TOML
        for k, v in changes.items():
            text = with_config_value(text, k, json.dumps(v, ensure_ascii=False))
        try:
            new = parse_settings(text)
        except ValueError as e:
            raise Invalid(f"config.toml would not be valid: {e}") from e
        write_config_atomic(path, text)
        self.settings = dataclasses.replace(self.settings,
                                            **{k: getattr(new, k) for k in changes})
        poke_daemon("reload-settings")
        return self.settings_info()

    def set_api_key(self, key: str, backend: str = "anthropic") -> dict:
        """Store an API key in secrets.toml; it is never sent back to the page."""
        key = str(key).strip()
        if not 8 <= len(key) <= 300 or not key.isascii() or not key.isprintable() \
                or any(c.isspace() for c in key):
            raise Invalid("that does not look like an API key")
        secrets = _secrets()
        secrets[API_KEYS[backend]] = key
        save_secrets(secrets)
        return {"set": True}

    def calendars(self) -> list[dict]:
        """Calendars by id and host or file name; links are secret and never shown."""
        from ..calendar import ics

        try:
            links = set(load_secrets().get("calendar_urls", []))
        except ValueError:
            links = set()
        return [{"id": _calendar_id(src), "label": ics.source_label(src),
                 "kind": "link" if src in links else "file"}
                for src in calendar_sources(self.settings.calendar_urls)]

    def add_calendar(self, url: str) -> list[dict]:
        from ..calendar import ics

        url = str(url).strip()
        if not CALENDAR_LINK.match(url):
            raise Invalid("paste the calendar's https:// or webcal:// ICS link")
        try:
            ics.parse(ics.fetch(url), dt.datetime.now().astimezone())
        except Exception as e:  # noqa: BLE001 - say what failed, without the link
            raise Invalid(f"could not read that calendar: {ics.describe_error(url, e)}") from e
        secrets = _secrets()
        secrets["calendar_urls"] = list(dict.fromkeys([*secrets.get("calendar_urls", []), url]))
        save_secrets(secrets)
        self._upcoming = (0.0, [])
        poke_daemon("refresh-calendar")
        return self.calendars()

    def remove_calendar(self, cal_id: str) -> list[dict]:
        secrets = _secrets()
        links = secrets.get("calendar_urls", [])
        keep = [u for u in links if _calendar_id(u) != cal_id]
        if len(keep) == len(links):
            if any(_calendar_id(u) == cal_id for u in self.settings.calendar_urls):
                raise Invalid("this calendar is set in config.toml; remove it there")
            raise NotFound("calendar")
        secrets["calendar_urls"] = keep
        save_secrets(secrets)
        self._upcoming = (0.0, [])
        poke_daemon("refresh-calendar")
        return self.calendars()

    def doctor(self) -> dict:
        from .. import __version__
        from ..tools import setup

        return {"checks": [dataclasses.asdict(c) for c in setup.checks(self.settings)],
                "version": __version__}

    # Updates (docs/dev/updates-design.md) -------------------------------------

    def updates_info(self) -> dict:
        from .. import __version__, updates

        inst = updates.install_kind()
        last = updates.last_check(self.state_dir)
        return {"current": __version__, "check": self.settings.update_check,
                "available": updates.available(self.state_dir),
                "checked": last.get("checked"), "error": last.get("error"),
                "whats_new": updates.whats_new(self.state_dir, __version__,
                                               has_data=bool(self._rec_dirs())),
                "run": updates.run_state(self.state_dir),
                "can_update": inst.kind in ("uv", "pipx"), "how": updates.how_to_update(inst),
                "disclosure": UPDATE_DISCLOSURE}

    def changelog(self) -> dict:
        from .. import updates

        return {"releases": [r.as_dict() for r in updates.releases()]}

    def mark_seen(self) -> None:
        from .. import updates

        updates.mark_seen(self.state_dir)

    def check_updates(self) -> dict:
        """Asked for by a click, so it runs whether or not the daily check is on."""
        from .. import updates

        updates.check(self.state_dir)
        return self.updates_info()

    def set_update_check(self, on: bool) -> dict:
        path = config_dir() / "config.toml"
        text = path.read_text() if path.exists() else DEFAULT_CONFIG_TOML
        write_config_atomic(path, with_config_value(text, "update_check", "true" if on else "false"))
        self.settings = dataclasses.replace(self.settings, update_check=bool(on))
        poke_daemon("reload-settings")
        return self.updates_info()

    def start_update(self) -> dict:
        """Run `wirenote update` as its own systemd unit: restarting the services would
        otherwise end it halfway, since it is a child of this one."""
        from .. import updates

        inst = updates.install_kind()
        if inst.kind not in ("uv", "pipx"):
            raise Invalid(updates.how_to_update(inst))
        run = updates.run_state(self.state_dir)
        if run and run.get("state") in updates.ACTIVE:
            raise Invalid("an update is already running")
        argv = [updates.wirenote_exe(), "update", "--yes", "--wait"]
        updates.progress(self.state_dir, "starting")
        systemd_run = shutil.which("systemd-run")
        if systemd_run:
            r = subprocess.run([systemd_run, "--user", "--collect", "--quiet",
                                "--unit", UPDATE_UNIT, *argv],
                               capture_output=True, text=True, check=False)
            if r.returncode != 0:
                updates.progress(self.state_dir, "failed", message="could not start the update")
                raise Invalid("could not start the update: " + (r.stderr.strip().splitlines() or ["?"])[-1])
        else:
            subprocess.Popen(argv, start_new_session=True, stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return self.updates_info()

    def set_vocabulary(self, terms: list[str]) -> list[str]:
        return save_vocabulary([str(t) for t in terms])

    def forget_voice(self, name: str) -> None:
        if not self.voices.forget(name):
            raise NotFound(name)

    # Files -----------------------------------------------------------------

    def mixed_audio(self, rec_id: str) -> Path:
        """Both tracks mixed into one Opus file, made on first use and cached."""
        d = self.rec_dir(rec_id)
        m = read_manifest(d)
        segs = [s for s in m.get("segments", [])
                if (f := segment_file(d, s)) and f.exists() and f.stat().st_size > 44]
        if not segs:
            raise NotFound("no audio")
        out = d / MIX
        lock = self._mix_locks.setdefault(rec_id, threading.Lock())
        with lock:
            newest = max((d / s["file"]).stat().st_mtime for s in segs)
            if out.exists() and out.stat().st_mtime >= newest:
                return out
            cmd = ["ffmpeg", "-nostdin", "-v", "error", "-y"]
            for s in segs:
                cmd += ["-i", str(d / s["file"])]
            chains = [f"[{i}:a]adelay={int(s['start_offset_s'] * 1000)}:all=1[a{i}]"
                      for i, s in enumerate(segs)]
            mix = "".join(f"[a{i}]" for i in range(len(segs)))
            graph = ";".join(chains) + f";{mix}amix=inputs={len(segs)}:normalize=0[out]"
            tmp = d / (MIX + ".tmp.ogg")
            cmd += ["-filter_complex", graph, "-map", "[out]", "-ac", "1",
                    "-c:a", "libopus", "-b:a", "32k", str(tmp)]
            subprocess.run(cmd, check=True, timeout=600)
            tmp.replace(out)
        return out

    def delete_audio(self, rec_id: str) -> None:
        d = self.rec_dir(rec_id)
        if self.status().get("recording_id") == rec_id:
            raise Invalid("still recording")
        for f in [*d.glob("*.wav"), d / MIX]:
            f.unlink(missing_ok=True)
        m = read_manifest(d)
        m["audio_deleted"] = _now()
        write_manifest(d, m)

    def delete_call(self, rec_id: str) -> None:
        d = self.rec_dir(rec_id)
        if self.status().get("recording_id") == rec_id:
            raise Invalid("still recording")
        state, _ = self._job_state(d)
        if state in ("transcribing", "summarizing"):
            raise Invalid("a note is being written for this call; try again when it is done")
        note = self._note(read_manifest(d))
        if note:
            note.unlink(missing_ok=True)
        shutil.rmtree(d)
        with self._jobs_lock:
            self.jobs.delete(d)

    def note_path(self, rec_id: str) -> Path:
        note = self._note(read_manifest(self.rec_dir(rec_id)))
        if not note or not note.exists():
            raise NotFound("no note")
        return note

    # Calendar --------------------------------------------------------------

    def upcoming(self) -> list[dict]:
        from ..calendar import ics

        fetched, events = self._upcoming
        if not fetched or time.monotonic() - fetched >= self.settings.calendar_refresh_s:
            sources = calendar_sources(self.settings.calendar_urls)
            events = ics.load(sources, dt.datetime.now().astimezone())[0] if sources else []
            self._upcoming = (time.monotonic(), events)
        now = dt.datetime.now().astimezone()
        soon = [e for e in events if e.end > now and e.start < now + dt.timedelta(hours=18)]
        return [{"title": e.title, "start": e.start.isoformat(), "end": e.end.isoformat(),
                 "attendees": list(e.attendees), "call": bool(e.service_links(None))}
                for e in sorted(soon, key=lambda e: e.start)[:8]]


def _mtime_ns(path: Path) -> int | None:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return None


def _quick_hit(entry: dict, q: str) -> dict | None:
    if isinstance(entry["title"], str) and q in entry["title"].lower():
        return {"where": "title", "text": entry["title"]}
    for name in entry["attendees"]:
        if q in name.lower():
            return {"where": "people", "text": name}
    for text in entry["texts"]:
        if q in text.lower():
            return {"where": "summary", "text": text}
    return None


def _seconds(hhmmss: str) -> float:
    h, m, s = (int(x) for x in hhmmss.split(":"))
    return float(h * 3600 + m * 60 + s)


def _relevant(lines: list[Line], question: str, limit: int | None,
              names: dict[str, str] | None = None) -> tuple[str, bool]:
    """The whole transcript if it fits, else the paragraphs that share the most
    words with the question (with their neighbours), in time order."""
    full = as_text(lines, names)
    if limit is None or len(full) <= limit:
        return full, False
    q = _stems(question)
    ranked = sorted(range(len(lines)), key=lambda i: -len(q & _stems(lines[i].text)))
    keep: set[int] = set()
    size = 0
    for i in ranked:
        for j in (i - 1, i, i + 1):
            if 0 <= j < len(lines) and j not in keep:
                cost = len(lines[j].text) + 30
                if size + cost > limit:
                    break
                keep.add(j)
                size += cost
        if size >= limit * 0.95:
            break
    return as_text([lines[i] for i in sorted(keep)], names), True


def _tick_in_note(note: Path, item: dict, done: bool) -> None:
    args = (item["owner"], item["task"], item.get("due", ""))
    old, new = action_line(*args, not done), action_line(*args, done)
    text = note.read_text()
    if old in text:
        note.write_text(text.replace(old, new, 1))
