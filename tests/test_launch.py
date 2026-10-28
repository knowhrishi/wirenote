# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hrishikesh Jadhav
"""Opening the UI through a one-time login link."""

from __future__ import annotations

import os
import stat

import pytest

from wirenote.ui import launch


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "run"))
    return tmp_path / "run"


def test_codes_are_private_single_files(runtime):
    code = launch.new_login_code()
    path = launch.login_dir() / code
    assert len(code) == 32 and path.is_file()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(launch.login_dir().stat().st_mode) == 0o700
    assert launch.redeem_login_code(code) is True
    assert launch.redeem_login_code(code) is False
    assert not path.exists()


def test_login_url_quotes_the_route(runtime):
    url = launch.login_url(8765, "#/call/x")
    assert url.startswith("http://127.0.0.1:8765/login?code=") and url.endswith("&next=%23%2Fcall%2Fx")


def test_open_ui_falls_back_when_port_not_mine(runtime, monkeypatch):
    opened = []
    monkeypatch.setattr(launch, "listener_uid", lambda port: os.getuid() + 1)
    monkeypatch.setattr(launch, "_xdg_open", opened.append)
    assert launch.open_ui(8765, "#/", fallback="/notes/x.md") is False
    assert opened == ["/notes/x.md"]
    assert not any(launch.login_dir().iterdir())


def test_open_ui_opens_login_link_when_mine(runtime, monkeypatch):
    opened = []
    monkeypatch.setattr(launch, "listener_uid", lambda port: os.getuid())
    monkeypatch.setattr(launch, "_xdg_open", opened.append)
    assert launch.open_ui(8765, "#/call/x") is True
    assert opened[0].startswith("http://127.0.0.1:8765/login?code=")


def test_cli_open_refuses_a_port_owned_by_someone_else(runtime, monkeypatch, capsys):
    from wirenote import cli

    monkeypatch.setenv("XDG_CONFIG_HOME", str(runtime.parent / "cfg"))
    monkeypatch.setattr(launch, "listener_uid", lambda port: os.getuid() + 1)
    opened = []
    monkeypatch.setattr(launch, "_xdg_open", opened.append)
    assert cli.main(["open"]) == 1
    assert opened == [] and "another user" in capsys.readouterr().err


def test_cli_open_opens_the_call_route(runtime, monkeypatch):
    from wirenote import cli

    monkeypatch.setenv("XDG_CONFIG_HOME", str(runtime.parent / "cfg"))
    monkeypatch.setattr(launch, "listener_uid", lambda port: os.getuid())
    opened = []
    monkeypatch.setattr(launch, "_xdg_open", opened.append)
    assert cli.main(["open", "20261027-100000-chrome"]) == 0
    assert opened[0].endswith("&next=%23%2Fcall%2F20261027-100000-chrome")


def test_daemon_open_note_falls_back_to_markdown(runtime, monkeypatch):
    from wirenote.detector.daemon import Daemon

    monkeypatch.setattr(launch, "listener_uid", lambda port: None)
    opened = []
    monkeypatch.setattr(launch, "_xdg_open", opened.append)

    class Log:
        def write(self, *a, **k):
            pass
    fake = type("D", (), {"settings": type("S", (), {"ui_port": 8765})(), "log": Log()})()
    Daemon._open_note(fake, "20261027-100000-chrome", "/notes/x.md")
    assert opened == ["/notes/x.md"]


@pytest.mark.parametrize("unit, systemctl, spawned", [
    (True, 0, False),                    # the installed service starts it: it outlives a launcher
    (True, 5, True),                     # systemctl failed: a process of its own instead
    (True, FileNotFoundError, True),     # no systemctl at all
    (False, None, True),                 # no unit installed: as before
])
def test_ensure_ui_starts_the_installed_service(runtime, monkeypatch, unit, systemctl, spawned):
    from wirenote.config.settings import config_dir

    if unit:
        units = config_dir().parent / "systemd" / "user"
        units.mkdir(parents=True)
        (units / "wirenote-ui.service").write_text("[Service]\n")
    started = []
    answers = iter([None, os.getuid(), os.getuid()])      # nothing yet, then ours
    monkeypatch.setattr(launch, "listener_uid", lambda port: next(answers))
    monkeypatch.setattr(launch, "_spawn_ui", lambda: started.append("spawn"))

    def fake_systemctl(argv):
        started.append(argv)
        if isinstance(systemctl, type):
            raise systemctl("systemctl")
        return systemctl
    monkeypatch.setattr(launch, "_systemctl", fake_systemctl)
    assert launch.ensure_ui(8765) is True
    assert (["systemctl", "--user", "start", "wirenote-ui.service"] in started) is unit
    assert ("spawn" in started) is spawned


def test_ensure_ui_starts_nothing_when_a_server_answers(runtime, monkeypatch):
    monkeypatch.setattr(launch, "listener_uid", lambda port: os.getuid() + 1)
    monkeypatch.setattr(launch, "_spawn_ui", lambda: pytest.fail("spawned"))
    monkeypatch.setattr(launch, "_systemctl", lambda argv: pytest.fail("started"))
    assert launch.ensure_ui(8765) is False
