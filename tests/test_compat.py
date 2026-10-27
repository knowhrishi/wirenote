# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hrishikesh Jadhav
"""What every released version wrote to disk still reads (docs/dev/changes.md › Stored data).

tests/compat/<version>/ holds one finished recording without its audio (as after
audio retention), its jobs.db, a config.toml that sets every setting that version
knew, and its default apps.toml. People upgrade with all three on disk; a rename or a removed
field that breaks them fails here, before a release.
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import replace
from pathlib import Path

import pytest

from wirenote.config.apps import parse_apps
from wirenote.config.settings import Settings, parse_settings
from wirenote.process.jobs import MIGRATIONS, Jobs
from wirenote.process.worker import rewrite_note
from wirenote.ui.data import Store

from test_process import no_voices

REPO = Path(__file__).parent.parent
COMPAT = Path(__file__).parent / "compat"
VERSIONS = sorted(p.name for p in COMPAT.iterdir() if p.is_dir())


def test_every_release_has_a_snapshot():
    released = re.findall(r"^## \[(\d+\.\d+\.\d+[^\]]*)\] - \d{4}-\d{2}-\d{2}$",
                          (REPO / "CHANGELOG.md").read_text(), re.MULTILINE)
    assert released, "no dated release in CHANGELOG.md"
    missing = [v for v in released if v not in VERSIONS]
    assert not missing, (f"add tests/compat/{missing[0]}/ (docs/dev/releasing.md, "
                         "step 'Save this version's stored data')")


@pytest.mark.parametrize("version", VERSIONS)
def test_config_and_app_rules_still_parse(version):
    parse_settings((COMPAT / version / "config.toml").read_text())
    assert parse_apps((COMPAT / version / "apps.toml").read_text()).rules


@pytest.mark.parametrize("version", VERSIONS)
def test_job_queue_still_reads(version, tmp_path):
    db = tmp_path / "jobs.db"
    shutil.copy(COMPAT / version / "jobs.db", db)
    jobs = Jobs(db)
    assert jobs.db.execute("PRAGMA user_version").fetchone() == (len(MIGRATIONS),)
    (job,) = jobs.recent()
    assert job.state == "done" and job.note
    assert jobs.get(jobs.add(Path("/elsewhere/rec"))).state == "queued"


@pytest.mark.parametrize("version", VERSIONS)
def test_recording_still_renders_and_opens_in_the_app(version, tmp_path):
    src = COMPAT / version / "recording"
    manifest = json.loads((src / "manifest.json").read_text())
    transcript = json.loads((src / "transcript.json").read_text())
    summary = json.loads((src / "summary.json").read_text())
    settings = replace(Settings(), recordings_dir=str(tmp_path / "recordings"),
                       notes_dir=str(tmp_path / "Meetings"))
    rec = tmp_path / "recordings" / manifest["id"]
    shutil.copytree(src, rec)

    note = rewrite_note(rec, settings).read_text()
    assert summary["title"] in note
    for line in transcript["lines"]:
        assert line["text"] in note

    store = Store(settings, tmp_path / "state", voices=no_voices(tmp_path))
    (call,) = store.calls()
    assert call["title"] == summary["title"]
    detail = store.call(call["id"])
    assert [l["text"] for l in detail["lines"]] == [l["text"] for l in transcript["lines"]]
    if summary["action_items"]:
        store.set_action_done(call["id"], 0, True)
        assert store.call(call["id"])["summary"]["action_items"][0]["done"] is True
