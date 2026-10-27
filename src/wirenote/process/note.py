# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hrishikesh Jadhav
"""The Markdown note (docs/dev/PLAN.md › Processing, step 4): frontmatter, summary, transcript."""

from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path

from .merge import Line, stamp
from .summarize import Summary


# Calendar titles (anyone can send an invite) and LLM output (a transcript can carry
# prompt injection) are not trusted Markdown: no HTML, no images, no links, since
# note viewers like Obsidian load and follow them. Escaping every
# bracket (and the backslash, so it cannot undo that) leaves no link syntax at all,
# where matching link patterns always misses a variant.
_MD_ESCAPES = str.maketrans({"\\": "\\\\", "[": "\\[", "]": "\\]", "<": "&lt;"})


def md_safe(text: str) -> str:
    return text.translate(_MD_ESCAPES)


def action_line(owner: str, task: str, due: str, done: bool) -> str:
    """The one place an action item's Markdown line is built (render, tick, rewrite)."""
    due = f" (due: {md_safe(due)})" if due else ""
    return f"- [{'x' if done else ' '}] **{md_safe(owner)}**: {md_safe(task)}{due}"


def inside(path: Path, *roots: Path) -> bool:
    """path is under one of roots (after resolving symlinks and ..)."""
    p = Path(path).expanduser().resolve()
    return any(p.is_relative_to(Path(r).expanduser().resolve()) for r in roots)


def own_note(manifest: dict, notes_root: Path) -> Path | None:
    """The manifest's note, only if it lies in the notes folder: a manifest is a file
    on disk, and Wirenote never opens or deletes anything else on its word."""
    note = manifest.get("note")
    return Path(note) if note and inside(Path(note), notes_root) else None


def segment_file(rec: Path, seg: dict) -> Path | None:
    """A segment's audio file, only if it is a plain file name in the recording folder."""
    name = str(seg.get("file") or "")
    return rec / name if name and Path(name).name == name and name not in (".", "..") else None


def title_for(manifest: dict, summary: Summary | None) -> str:
    if manifest.get("title_override"):          # renamed in the app, or `import --title`
        return manifest["title_override"]
    cal = manifest.get("calendar") or {}
    if cal.get("title"):
        return cal["title"]
    if summary and summary.title:
        return summary.title
    if manifest.get("service") and manifest.get("room"):
        return f"{manifest['service']} {manifest['room']}"
    if manifest.get("app") in ("manual", "import"):
        return manifest.get("name") or "Meeting"
    return f"{manifest.get('name') or manifest.get('app', 'Call')} call"


def note_path(notes_root: Path, started: dt.datetime, title: str) -> Path:
    safe = re.sub(r"[^\w\- ]+", "", title, flags=re.UNICODE).strip()[:60] or "Call"
    return notes_root / f"{started:%Y}" / f"{started:%m}" / f"{started:%Y-%m-%d %H%M} {safe}.md"


def _yaml(value) -> str:
    # JSON scalars and flow lists are valid YAML.
    return json.dumps(value, ensure_ascii=False)


def render(manifest: dict, lines: list[Line], summary: Summary | None, *, title: str,
           duration_min: float, audio: list[str], transcript_model: str,
           summary_backend: str | None, summary_error: str | None = None,
           names: dict[str, str] | None = None, my_notes: str = "") -> str:
    names = names or {}
    cal = manifest.get("calendar") or {}
    front = {
        "title": title,
        "date": manifest["started"],
        "duration_min": round(duration_min, 1),
        "app": manifest.get("app"),
        "service": manifest.get("service"),
        "room": manifest.get("room"),
        "calendar": cal.get("title"),
        "attendees": cal.get("attendees") or None,
        "speakers": sorted({names.get(l.speaker, l.speaker) for l in lines} - {"Me"}) or None,
        "audio": audio or None,
        "transcript_model": transcript_model,
        "summary_backend": summary_backend if summary else None,
        "recording_id": manifest["id"],
    }
    out = ["---"]
    out += [f"{k}: {_yaml(v)}" for k, v in front.items() if v is not None]
    out += ["---", "", f"# {md_safe(title)}", ""]

    if summary:
        out += ["## Summary", ""] + [f"- {md_safe(p)}" for p in summary.summary] + [""]
        if summary.decisions:
            out += ["## Decisions", ""] + [f"- {md_safe(d)}" for d in summary.decisions] + [""]
        if summary.action_items:
            out += ["## Action items", ""]
            out += [action_line(a.owner, a.task, a.due, False) for a in summary.action_items]
            out.append("")
        if summary.open_questions:
            out += ["## Open questions", ""] + [f"- {md_safe(q)}" for q in summary.open_questions] + [""]
    elif summary_error:
        out += [f"> Summary not available: {md_safe(summary_error)}",
                f"> Run `wirenote reprocess {manifest['id']} --summary` to try again.", ""]

    if my_notes:
        out += ["## My notes", "", my_notes.strip(), ""]

    out += ["## Transcript", ""]
    if not lines:
        out += ["_No speech was recognized._", ""]
    for line in lines:
        out += [f"**{md_safe(names.get(line.speaker, line.speaker))}** [{stamp(line.start)}] "
                f"{md_safe(line.text)}", ""]
    return "\n".join(out).rstrip() + "\n"
