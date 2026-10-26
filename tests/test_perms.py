# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hrishikesh Jadhav
"""Private files: umask 077, and doctor's check/fix of loose modes."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from wirenote.config.perms import fix_modes, loose_files
from wirenote.process.jobs import Jobs
from wirenote.process.worker import run_job

from test_process import FakeBackend, FakeTranscriber, make_recording, no_voices, settings_for

REPO = Path(__file__).parent.parent


@pytest.fixture
def xdg(tmp_path, monkeypatch):
    for var in ("XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_DATA_HOME", "XDG_RUNTIME_DIR"):
        monkeypatch.setenv(var, str(tmp_path / var.lower()))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    return tmp_path


@pytest.fixture
def restore_umask():
    old = os.umask(0o022)
    os.umask(old)
    yield
    os.umask(old)


def _mode(p: Path) -> int:
    return stat.S_IMODE(p.stat().st_mode)


def test_cli_sets_umask(xdg, restore_umask):
    from wirenote import cli

    os.umask(0o022)
    cli.main(["status"])
    current = os.umask(0o022)
    assert current == 0o077


def test_loose_files_and_fix(tmp_path):
    root = tmp_path / "data"
    sub = root / "rec"
    sub.mkdir(parents=True)
    os.chmod(root, 0o700)
    os.chmod(sub, 0o755)
    f = sub / "transcript.json"
    f.write_text("{}")
    os.chmod(f, 0o644)
    assert set(loose_files([root])) == {sub, f}
    assert fix_modes([root]) == 2
    assert loose_files([root]) == []
    assert _mode(sub) == 0o700 and _mode(f) == 0o600


def test_missing_roots_are_skipped(tmp_path):
    assert loose_files([tmp_path / "nope"]) == []
    assert fix_modes([tmp_path / "nope"]) == 0


def test_worker_outputs_are_private(tmp_path, restore_umask):
    os.umask(0o077)
    rec = make_recording(tmp_path)
    jobs = Jobs(tmp_path / "jobs.db")
    job = run_job(jobs.add(rec), settings_for(tmp_path), jobs, FakeTranscriber(),
                  lambda s: FakeBackend(), voices=no_voices(tmp_path))
    written = [p for p in rec.iterdir() if p.name != "mic-000.wav"] + [Path(job.note)]
    assert written
    for p in written:
        assert _mode(p) == 0o600, p


def test_units_have_umask():
    for unit in ("wirenote.service", "wirenote-ui.service"):
        assert "UMask=0077" in (REPO / "src" / "wirenote" / "data" / unit).read_text()


def test_fix_keeps_the_owners_bits(tmp_path):
    root = tmp_path / "data"
    root.mkdir(mode=0o700)
    script = root / "hook.sh"
    script.write_text("#!/bin/sh\n")
    os.chmod(script, 0o755)
    fix_modes([root])
    assert _mode(script) == 0o700


def test_only_wirenote_notes_in_the_notes_folder(tmp_path, xdg):
    """Review: Important 6. notes_dir may be a vault or ~/Documents: touch only our notes."""
    import json
    from dataclasses import replace

    from wirenote.config.perms import private_paths
    from wirenote.config.settings import Settings

    notes = tmp_path / "vault"
    (notes / "2026" / "10").mkdir(parents=True)
    mine = notes / "2026" / "10" / "call.md"
    mine.write_text("note")
    theirs = notes / "deploy.sh"
    theirs.write_text("#!/bin/sh\n")
    for p, mode in ((notes, 0o755), (mine, 0o644), (theirs, 0o755)):
        os.chmod(p, mode)
    s = replace(Settings(), notes_dir=str(notes), recordings_dir=str(tmp_path / "rec"))
    rec = tmp_path / "rec" / "20261027-100000-chrome"
    rec.mkdir(parents=True)
    (rec / "manifest.json").write_text(json.dumps({"note": str(mine)}))
    roots, files = private_paths(s)
    assert notes not in roots and files == [mine]
    assert mine in loose_files(roots, files) and theirs not in loose_files(roots, files)
    fix_modes(roots, files)
    assert _mode(mine) == 0o600 and _mode(theirs) == 0o755 and _mode(notes) == 0o755
