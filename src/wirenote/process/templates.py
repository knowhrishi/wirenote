# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hrishikesh Jadhav
"""Summary templates: the sections a kind of meeting needs, and how Wirenote picks one.

See docs/dev/PLAN.md › Defaults › Summary template and docs/usage.md › Summary templates.

A template adds a meeting type, a few instructions and extra sections (lists of short
points) to the summary request. Five are built in; your own live as TOML files in
`~/.config/wirenote/templates/`, and a file named like a built-in replaces it. A calendar
title only ever selects a template, by matching words; it is never put in the instructions.
"""

from __future__ import annotations

import json
import logging
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from ..config.settings import config_dir

log = logging.getLogger(__name__)

SERIES = "series.json"
KEY = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
TEMPLATE_ID = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
# docs/dev/PLAN.md › Defaults › Summary template
MAX_SECTIONS = 6
MAX_INSTRUCTIONS = 2000
MAX_FILE_BYTES = 64 * 1024


@dataclass(frozen=True)
class Section:
    key: str
    title: str
    hint: str


@dataclass(frozen=True)
class Template:
    id: str
    name: str
    instructions: str
    sections: tuple[Section, ...]
    titles: tuple[str, ...]     # words in a meeting's title that select this template
    apps: tuple[str, ...]       # call apps (the manifest's "app") that select it


def _builtin(id_: str, name: str, instructions: str, sections: tuple[Section, ...] = (),
             titles: tuple[str, ...] = ()) -> Template:
    return Template(id_, name, instructions, sections, titles, ())


BUILTIN: dict[str, Template] = {t.id: t for t in (
    _builtin("general", "General", ""),
    _builtin("one-on-one", "One-on-one",
             "A conversation between two people, often a manager and the person they work with. "
             "Say who gave each piece of feedback.",
             (Section("feedback", "Feedback",
                      "feedback either person gave the other, one short point each, saying who gave it"),
              Section("next_time", "Topics for next time",
                      "topics that were put off, or that someone wants to cover at the next meeting")),
             ("1:1", "1-1", "1on1", "one on one")),
    _builtin("standup", "Standup",
             "A short status meeting. Report what each person said, under their own name.",
             (Section("updates", "Updates by person",
                      "one point per person: their name, then what they did and plan to do"),
              Section("blockers", "Blockers",
                      "things that hold someone up, saying who is blocked; empty if none")),
             ("standup", "stand-up", "daily", "scrum")),
    _builtin("interview", "Interview",
             "A job interview. Record only what was said. Do not rate, rank, score or judge the "
             "candidate, do not say whether they fit or should be hired, and do not guess at "
             "their traits or motives. The people who were there decide.",
             (Section("experience", "What the candidate said about their experience",
                      "what the candidate said about their own work, roles and skills, as "
                      "statements of what was said, with no rating"),
              Section("candidate_questions", "Questions the candidate asked",
                      "questions the candidate asked, each with the gist of the answer"),
              Section("follow_ups", "Follow-ups",
                      "things either side said they would send or do after the interview")),
             ("interview", "screening", "candidate")),
    _builtin("lecture", "Lecture",
             "A talk or class with one main speaker. Write down what was taught.",
             (Section("concepts", "Key concepts", "the main ideas that were taught, one point each"),
              Section("terms", "Terms and definitions",
                      "terms the speaker defined, as 'term: definition' in the speaker's words"),
              Section("references", "Books, papers and links mentioned",
                      "books, papers, tools and links the speaker named, with the author if said")),
             ("lecture", "seminar", "class", "webinar")),
)}


# Loading ---------------------------------------------------------------------

def templates_dir() -> Path:
    return config_dir() / "templates"


def _strings(value, what: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
        raise ValueError(f"{what} must be a list of text")
    return tuple(value)


def parse_template(id_: str, text: str) -> Template:
    """A template from the text of its file; ValueError (with a fixed message, never the
    file's text) when it is not valid."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        raise ValueError("not valid TOML") from None
    name, instructions = data.get("name", id_), data.get("instructions", "")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("name must be text")
    if not isinstance(instructions, str) or len(instructions) > MAX_INSTRUCTIONS:
        raise ValueError(f"instructions must be text of at most {MAX_INSTRUCTIONS} characters")
    raw = data.get("section", [])
    if not isinstance(raw, list) or not all(isinstance(s, dict) for s in raw):
        raise ValueError("[[section]] must be a table")
    if len(raw) > MAX_SECTIONS:
        raise ValueError(f"more than {MAX_SECTIONS} sections")
    sections = []
    for s in raw:
        key, title, hint = s.get("key"), s.get("title"), s.get("hint", "")
        if not isinstance(key, str) or not KEY.match(key):
            raise ValueError("a section key must be a-z, 0-9 and _, starting with a letter")
        if not isinstance(title, str) or not title.strip() or not isinstance(hint, str):
            raise ValueError("a section needs a title and a hint in text")
        sections.append(Section(key, title.strip(), hint.strip()))
    if len({s.key for s in sections}) != len(sections):
        raise ValueError("two sections have the same key")
    return Template(id_, name.strip(), instructions.strip(), tuple(sections),
                    _strings(data.get("titles", []), "titles"), _strings(data.get("apps", []), "apps"))


def scan(folder: Path | None = None) -> tuple[dict[str, Template], list[tuple[str, str]]]:
    """(all templates, [(file name, why it was skipped)]). A custom template with a
    built-in's id replaces that built-in."""
    folder = folder if folder is not None else templates_dir()
    found, skipped = dict(BUILTIN), []
    try:
        files = sorted(p for p in folder.iterdir() if p.suffix == ".toml")
    except OSError:
        return found, skipped
    for path in files:
        try:
            if not TEMPLATE_ID.match(path.stem):
                raise ValueError("the file name must be a-z, 0-9, - and _, starting with a letter")
            if path.stat().st_size > MAX_FILE_BYTES:
                raise ValueError("file too large")
            found[path.stem] = parse_template(path.stem, path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            skipped.append((path.name, "cannot be read"))
        except ValueError as e:
            skipped.append((path.name, str(e)))
    return found, skipped


def load_templates(folder: Path | None = None) -> dict[str, Template]:
    found, skipped = scan(folder)
    for name, why in skipped:
        log.warning("template file %s skipped: %s", name, why)    # never the file's text
    return found


# Picking ---------------------------------------------------------------------

def _has_word(text: str, words: tuple[str, ...]) -> bool:
    text = text.casefold()
    return any(re.search(rf"(?<!\w){re.escape(w.casefold())}(?!\w)", text) for w in words if w)


def pick(manifest: dict, templates: dict[str, Template], series: dict[str, str],
         default: str) -> Template:
    """The template for a call: the one chosen for it, the one remembered for its
    calendar series, a title match, an app match, the default, else General. An id that
    no longer exists falls through to the next step. Your own templates are matched before
    the built-ins, so a file's words win over a built-in's."""
    cal = manifest.get("calendar") or {}
    # An import has no calendar event; its title is the name it was given.
    title = cal.get("title") or manifest.get("title_override") or manifest.get("title") or ""
    for chosen in (manifest.get("template"), series.get(str(cal.get("uid") or ""))):
        if isinstance(chosen, str) and chosen in templates:
            return templates[chosen]
    ordered = sorted(templates.values(), key=lambda t: BUILTIN.get(t.id) is t)    # files first
    if title:
        for t in ordered:
            if _has_word(str(title), t.titles):
                return t
    app = manifest.get("app")
    if app:
        for t in ordered:
            if app in t.apps:
                return t
    return templates.get(default) or templates.get("general") or BUILTIN["general"]


# Series ----------------------------------------------------------------------

def series_templates(state_dir: Path) -> dict[str, str]:
    """{calendar event uid: template id} for series the user chose a template for."""
    try:
        data = json.loads((state_dir / SERIES).read_text())
    except (OSError, ValueError):
        return {}
    return {str(k): v for k, v in data.items() if isinstance(v, str)} if isinstance(data, dict) else {}


def remember_series(state_dir: Path, uid: str, template_id: str) -> None:
    """Keep template_id for the calendar series uid; an empty id forgets the series, so
    its meetings go back to the automatic choice."""
    series = series_templates(state_dir)
    if template_id:
        series[uid] = template_id
    else:
        series.pop(uid, None)
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = state_dir / SERIES
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(series, indent=2, ensure_ascii=False))
    tmp.replace(path)
