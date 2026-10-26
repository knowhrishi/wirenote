# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hrishikesh Jadhav
"""One-time move of meetnote's folders to Wirenote's."""

from __future__ import annotations

import json
import os
import socket
import sqlite3
import stat

import pytest

from wirenote.config import migrate
from wirenote.config.migrate import MigrationError, migrate_if_needed
from wirenote.process.jobs import Jobs


@pytest.fixture
def xdg(tmp_path, monkeypatch):
    dirs = {}
    for var in ("XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_DATA_HOME", "XDG_RUNTIME_DIR"):
        d = tmp_path / var.lower()
        d.mkdir()
        monkeypatch.setenv(var, str(d))
        dirs[var] = d
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    return dirs


def old_tree(xdg) -> dict:
    cfg = xdg["XDG_CONFIG_HOME"] / "meetnote"
    state = xdg["XDG_STATE_HOME"] / "meetnote"
    data = xdg["XDG_DATA_HOME"] / "meetnote"
    rec = data / "recordings" / "20261027-100000-chrome"
    rec.mkdir(parents=True)
    cfg.mkdir()
    state.mkdir()
    (cfg / "config.toml").write_text('consent_line = "hi"\n')
    (rec / "manifest.json").write_text(json.dumps({
        "id": rec.name, "note": str(data / "notes" / "x.md"),
        "segments": [{"file": "mic-000.wav", "path": str(rec / "mic-000.wav")}]}))
    os.chmod(rec / "manifest.json", 0o644)
    jobs = Jobs(state / "jobs.db")
    jobs.add(rec)
    jobs.close()
    return {"cfg": cfg, "state": state, "data": data, "rec": rec}


def test_migrates_dirs_rewrites_paths_and_removes_old(xdg):
    old = old_tree(xdg)
    msg = migrate_if_needed()
    assert msg and "migrated" in msg
    new_data = xdg["XDG_DATA_HOME"] / "wirenote"
    new_state = xdg["XDG_STATE_HOME"] / "wirenote"
    manifest = json.loads((new_data / "recordings" / old["rec"].name / "manifest.json").read_text())
    assert manifest["note"] == str(new_data / "notes" / "x.md")
    assert manifest["segments"][0]["path"].startswith(str(new_data))
    with sqlite3.connect(new_state / "jobs.db") as db:
        (recording,) = db.execute("SELECT recording FROM jobs").fetchone()
    assert "/wirenote/" in recording and "/meetnote/" not in recording
    assert (xdg["XDG_CONFIG_HOME"] / "wirenote" / "config.toml").read_text() == 'consent_line = "hi"\n'
    assert not any(p.exists() for p in (old["cfg"], old["state"], old["data"]))
    mode = stat.S_IMODE((new_data / "recordings" / old["rec"].name / "manifest.json").stat().st_mode)
    assert mode == 0o600


def test_noop_without_old_dirs(xdg):
    assert migrate_if_needed() is None


def test_noop_when_new_dirs_exist(xdg):
    old = old_tree(xdg)
    (xdg["XDG_CONFIG_HOME"] / "wirenote").mkdir()
    assert migrate_if_needed() is None
    assert old["data"].exists() and old["state"].exists()


def test_refuses_while_old_daemon_listens(xdg):
    old = old_tree(xdg)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(str(xdg["XDG_RUNTIME_DIR"] / "meetnote.sock"))
    sock.listen()
    try:
        with pytest.raises(MigrationError, match="stop the old service"):
            migrate_if_needed()
    finally:
        sock.close()
    assert old["data"].exists()
    assert not (xdg["XDG_DATA_HOME"] / "wirenote").exists()


def test_failure_leaves_old_dirs_untouched(xdg, monkeypatch):
    old = old_tree(xdg)

    def boom(*_a, **_k):
        raise OSError("disk full")
    monkeypatch.setattr(migrate, "_rewrite_paths", boom)
    with pytest.raises(MigrationError, match="disk full"):
        migrate_if_needed()
    assert old["data"].exists() and old["state"].exists() and old["cfg"].exists()
    for base in ("XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_DATA_HOME"):
        assert sorted(p.name for p in xdg[base].iterdir()) == ["meetnote"]


def test_moves_calendar_links_to_secrets(xdg):
    old = old_tree(xdg)
    (old["cfg"] / "config.toml").write_text(
        'calendar_urls = ["https://x.example/a.ics", "/home/u/c.ics"]\n')
    migrate_if_needed()
    cfg = xdg["XDG_CONFIG_HOME"] / "wirenote"
    text = (cfg / "config.toml").read_text()
    assert "https://" not in text and "/home/u/c.ics" in text
    secrets = cfg / "secrets.toml"
    assert "https://x.example/a.ics" in secrets.read_text()
    assert stat.S_IMODE(secrets.stat().st_mode) == 0o600


def test_cli_migrates_before_any_command(xdg, capsys):
    from wirenote import cli

    old_tree(xdg)
    cli.main(["status"])
    assert (xdg["XDG_DATA_HOME"] / "wirenote" / "recordings").is_dir()
    assert "migrated your meetnote data" in capsys.readouterr().err


def test_cli_stops_when_migration_is_refused(xdg, capsys):
    from wirenote import cli

    old_tree(xdg)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(str(xdg["XDG_RUNTIME_DIR"] / "meetnote.sock"))
    sock.listen()
    try:
        assert cli.main(["status"]) == 2
    finally:
        sock.close()
    assert "stop the old service" in capsys.readouterr().err


def test_multiline_calendar_urls_move_cleanly(xdg):
    """Review: Important 4. A list over several lines must be replaced whole."""
    import tomllib

    old = old_tree(xdg)
    (old["cfg"] / "config.toml").write_text(
        'consent_line = "hi"\ncalendar_urls = [\n  "https://x.example/a.ics",\n'
        '  "/home/u/c.ics",\n]\nui_port = 8765\n')
    migrate_if_needed()
    text = (xdg["XDG_CONFIG_HOME"] / "wirenote" / "config.toml").read_text()
    data = tomllib.loads(text)
    assert data["calendar_urls"] == ["/home/u/c.ics"] and data["ui_port"] == 8765
    assert "https://" not in text


def test_unreadable_result_rolls_back(xdg, monkeypatch):
    from wirenote.config import secrets

    old = old_tree(xdg)
    (old["cfg"] / "config.toml").write_text('calendar_urls = ["https://x.example/a.ics"]\n')
    monkeypatch.setattr(secrets, "with_config_value", lambda text, key, value: text + "= broken\n")
    with pytest.raises(MigrationError):
        migrate_if_needed()
    assert (old["cfg"] / "config.toml").exists()
    assert not (xdg["XDG_CONFIG_HOME"] / "wirenote").exists()
