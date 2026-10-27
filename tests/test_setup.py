# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hrishikesh Jadhav
"""secrets.toml and config.toml edits behind `wirenote setup` / `wirenote calendar`."""

from __future__ import annotations

import tomllib

from wirenote.config.secrets import (calendar_sources, load_secrets, save_secrets, secrets_path,
                                     set_config_value)
from wirenote.config.settings import DEFAULT_CONFIG_TOML, parse_settings


def test_secrets_round_trip_private(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert load_secrets() == {}
    save_secrets({"anthropic_api_key": 'sk-"quoted"', "calendar_urls": ["https://a/b.ics?x=1&y=ü"]})
    assert secrets_path().stat().st_mode & 0o777 == 0o600
    assert load_secrets() == {"anthropic_api_key": 'sk-"quoted"',
                              "calendar_urls": ["https://a/b.ics?x=1&y=ü"]}
    assert calendar_sources(("file.ics", "https://a/b.ics?x=1&y=ü")) == (
        "file.ics", "https://a/b.ics?x=1&y=ü")


def test_set_config_value_replaces_the_commented_default(tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text(DEFAULT_CONFIG_TOML)
    set_config_value(cfg, "summary_backend", '"anthropic"')
    set_config_value(cfg, "consent_line", '"Ist das okay?"')
    set_config_value(cfg, "summary_backend", '"ollama"')         # again: same line
    text = cfg.read_text()
    assert text.count("summary_backend =") == 1
    s = parse_settings(text)
    assert (s.summary_backend, s.consent_line) == ("ollama", "Ist das okay?")
    set_config_value(cfg, "brand_new", "1")
    assert tomllib.loads(cfg.read_text())["brand_new"] == 1


def test_calendar_add_error_hides_the_link(tmp_path, monkeypatch, capsys):
    import urllib.error

    from wirenote.calendar import ics
    from wirenote.tools import setup

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    link = "https://cal.example/private/SECRET123/basic.ics"
    monkeypatch.setattr(setup.getpass, "getpass", lambda prompt: link)

    def fail(*_a, **_k):
        raise urllib.error.URLError(f"cannot reach {link}")
    monkeypatch.setattr(ics, "fetch", fail)
    assert setup.calendar_add() == 1
    out = capsys.readouterr().out
    assert "SECRET123" not in out and "cal.example" in out


# doctor as data -----------------------------------

def _offline_checks(monkeypatch):
    from wirenote.tools import setup

    for name in ("_daemon_checks", "_pipewire_checks", "_model_checks", "_summary_checks",
                 "_calendar_checks"):
        monkeypatch.setattr(setup, name, lambda *a: [])


def test_checks_report_loose_permissions_and_fix(tmp_path, monkeypatch, capsys):
    import os
    import stat
    from dataclasses import replace

    from wirenote.config.settings import Settings
    from wirenote.tools import setup

    for var in ("XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_DATA_HOME"):
        monkeypatch.setenv(var, str(tmp_path / var.lower()))
    _offline_checks(monkeypatch)
    notes = tmp_path / "Meetings"
    notes.mkdir(mode=0o700)
    s = replace(Settings(), notes_dir=str(notes))
    rec = s.recordings_path() / "20261027-100000-chrome"
    rec.mkdir(parents=True)
    loose = rec / "transcript.json"
    loose.write_text("x")
    os.chmod(loose, 0o644)
    monkeypatch.setattr(setup, "_settings", lambda: s)
    bad = [c for c in setup.checks(s) if c.status == "bad"]
    assert any("readable by other" in c.message and c.fix == "wirenote doctor --fix" for c in bad)
    setup.doctor(fix=True)
    assert stat.S_IMODE(loose.stat().st_mode) == 0o600
    assert not [c for c in setup.checks(s) if "readable by other" in c.message]


def test_checks_warn_about_links_in_config(tmp_path, monkeypatch):
    from dataclasses import replace

    from wirenote.config.settings import Settings
    from wirenote.tools import setup

    for var in ("XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_DATA_HOME"):
        monkeypatch.setenv(var, str(tmp_path / var.lower()))
    _offline_checks(monkeypatch)
    s = replace(Settings(), notes_dir=str(tmp_path / "n"),
                calendar_urls=("https://cal.example/x.ics",))
    msgs = [c.message for c in setup.checks(s) if c.status != "ok"]
    assert any("calendar link in config.toml" in m for m in msgs)
    assert not any("cal.example/x.ics" in m for m in msgs)


def test_config_value_with_backslashes_round_trips(tmp_path):
    import json

    cfg = tmp_path / "config.toml"
    cfg.write_text(DEFAULT_CONFIG_TOML)
    text = 'Recording "this" call\\ok?\nline two'
    set_config_value(cfg, "consent_line", json.dumps(text))
    assert parse_settings(cfg.read_text()).consent_line == text


def test_doctor_ollama_owned_by_another_user(monkeypatch):
    import os
    from dataclasses import replace

    from wirenote.config.settings import Settings
    from wirenote.tools import setup

    monkeypatch.setattr(setup, "listener_uid", lambda port, host="127.0.0.1": os.getuid() + 7)
    monkeypatch.setattr(setup, "trusted_owner", lambda uid: uid == os.getuid())

    def never(*_a, **_k):
        raise AssertionError("must not contact it")
    monkeypatch.setattr(setup.urllib.request, "urlopen", never)
    ok, msg = setup._check_ollama(replace(Settings(), ollama_url="http://localhost:11434"))
    assert not ok and "another user" in msg and str(os.getuid() + 7) in msg


def test_config_value_replaces_a_multiline_value():
    import tomllib

    from wirenote.config.secrets import with_config_value

    text = 'a = 1\ncalendar_urls = [\n  "x", # one\n  "y",\n]\nb = """two\nlines"""\n# c = 3\n'
    out = with_config_value(text, "calendar_urls", '["z"]')
    assert tomllib.loads(out) == {"a": 1, "calendar_urls": ["z"], "b": "two\nlines"}
    out = with_config_value(out, "b", '"one"')
    assert tomllib.loads(out) == {"a": 1, "calendar_urls": ["z"], "b": "one"}
    assert tomllib.loads(with_config_value(out, "c", "4"))["c"] == 4


def test_cli_refuses_to_run_outside_linux(monkeypatch, capsys):
    from wirenote import cli

    monkeypatch.setattr(cli.sys, "platform", "darwin")
    assert cli.main(["status"]) == 2
    assert "Linux" in capsys.readouterr().err


def test_doctor_reports_updates_from_the_last_check_only(monkeypatch):
    import json
    from dataclasses import replace

    import pytest

    from wirenote import updates
    from wirenote.config.settings import Settings, state_dir
    from wirenote.tools import setup

    monkeypatch.setattr(updates, "_get", lambda url, timeout=10.0: pytest.fail("went online"))
    (off,) = setup._update_checks(Settings())
    assert off.status == "ok" and "update --check" in off.message
    on = replace(Settings(), update_check=True)
    state_dir().mkdir(parents=True, exist_ok=True)
    (state_dir() / updates.CHECK).write_text(json.dumps({"checked": 1.0, "latest": "99.0.0"}))
    (avail,) = setup._update_checks(on)
    assert avail.status == "warn" and "99.0.0" in avail.message and avail.fix == "wirenote update"


def test_setup_openai_checks_before_it_saves(monkeypatch, capsys):
    import io
    import json
    import urllib.error

    from wirenote.config.settings import config_dir, load_settings
    from wirenote.process import summarize as summ
    from wirenote.tools import setup

    answers = iter(["", ""])                       # the default address and model
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
    monkeypatch.setattr(setup.getpass, "getpass", lambda prompt: "sk-or-v1-test1234")
    monkeypatch.setattr(summ.socket, "getaddrinfo", lambda *a, **k: (_ for _ in ()).throw(OSError()))
    replies = [urllib.error.HTTPError("u", 401, "x", {}, io.BytesIO(b"{}")), {"ok": "ok"}]

    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            self.close()

    def urlopen(req, timeout):
        r = replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return Resp(json.dumps({"choices": [{"message": {"content": json.dumps(r)}}]}).encode())
    monkeypatch.setattr(summ.urllib.request, "urlopen", urlopen)

    assert setup.setup_openai() == 1                          # the key was rejected
    assert "rejected the API key" in capsys.readouterr().out and load_secrets() == {}
    answers = iter(["", ""])
    assert setup.setup_openai() == 0
    out = capsys.readouterr().out
    assert "sent to openrouter.ai" in out and "sk-or-v1-test1234" not in out
    assert load_secrets()["openai_api_key"] == "sk-or-v1-test1234"
    s = load_settings(config_dir() / "config.toml")
    assert (s.summary_backend, s.openai_url) == ("openai", "https://openrouter.ai/api/v1")


def test_setup_claude_without_a_key_says_how_to_log_in(monkeypatch, capsys):
    import anthropic

    from wirenote.tools import setup

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(setup.getpass, "getpass", lambda prompt: "")

    class NoCredentials:
        def __init__(self, api_key=None):
            self.models = self

        def retrieve(self, model):
            raise TypeError("Could not resolve authentication method")
    monkeypatch.setattr(anthropic, "Anthropic", NoCredentials)
    assert setup.setup_claude() == 1
    out = capsys.readouterr().out
    assert "ant auth login" in out and "subscription cannot be used" in out
