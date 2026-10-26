# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hrishikesh Jadhav
"""`wirenote setup install` / `uninstall`."""

from __future__ import annotations

import pytest

from wirenote.tools import install


@pytest.fixture
def home(tmp_path, monkeypatch):
    for var in ("XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_DATA_HOME", "XDG_RUNTIME_DIR"):
        monkeypatch.setenv(var, str(tmp_path / var.lower()))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(install, "_exe", lambda: "/x/bin/wirenote")
    ran = []
    monkeypatch.setattr(install, "_run", lambda argv: ran.append(argv) or 0)
    return tmp_path, ran


def test_install_dry_run_lists_files_and_commands(home, capsys):
    tmp, ran = home
    assert install.install(dry_run=True) == 0
    out = capsys.readouterr().out
    units = tmp / "xdg_config_home" / "systemd" / "user"
    assert str(units / "wirenote.service") in out and str(units / "wirenote-ui.service") in out
    assert "ExecStart=/x/bin/wirenote run" in out and "UMask=0077" in out
    assert "systemctl --user enable wirenote.service wirenote-ui.service" in out
    assert "systemctl --user restart wirenote.service wirenote-ui.service" in out
    assert ran == [] and not units.exists()


def test_install_writes_files_and_runs_systemctl(home):
    tmp, ran = home
    assert install.install(dry_run=False) == 0
    units = tmp / "xdg_config_home" / "systemd" / "user"
    ui = (units / "wirenote-ui.service").read_text()
    assert "ExecStart=/x/bin/wirenote ui" in ui and "UMask=0077" in ui
    desktop = (tmp / "xdg_data_home" / "applications" / "wirenote.desktop").read_text()
    assert "Exec=/x/bin/wirenote open" in desktop and "Name=Wirenote" in desktop
    assert (tmp / "xdg_data_home" / "icons/hicolor/scalable/apps/wirenote.svg").read_text().startswith("<svg")
    assert ran == [["systemctl", "--user", "daemon-reload"],
                   ["systemctl", "--user", "enable", "wirenote.service", "wirenote-ui.service"],
                   # an update must run the new code: restart, which also starts them
                   ["systemctl", "--user", "restart", "wirenote.service", "wirenote-ui.service"]]


def test_install_disables_old_meetnote_units_first(home):
    tmp, ran = home
    units = tmp / "xdg_config_home" / "systemd" / "user"
    units.mkdir(parents=True)
    (units / "meetnote.service").write_text("[Service]\n")
    (units / "meetnote-ui.service").write_text("[Service]\n")
    install.install(dry_run=False)
    assert ran[1] == ["systemctl", "--user", "disable", "--now", "meetnote.service", "meetnote-ui.service"]
    assert ran[-2][:3] == ["systemctl", "--user", "enable"]
    assert not (units / "meetnote.service").exists()


def test_uninstall_purge_needs_confirmation(home):
    tmp, ran = home
    install.install(dry_run=False)
    data = tmp / "xdg_data_home" / "wirenote"
    data.mkdir(parents=True)
    notes = tmp / "home" / "Meetings"
    notes.mkdir(parents=True)
    assert install.uninstall(purge=True, dry_run=False, confirm=lambda prompt: "no") == 1
    assert data.exists()
    assert install.uninstall(purge=True, dry_run=False, confirm=lambda prompt: "DELETE") == 0
    assert not data.exists() and notes.exists()
    assert not (tmp / "xdg_config_home" / "systemd" / "user" / "wirenote.service").exists()
    assert ["systemctl", "--user", "disable", "--now", "wirenote.service", "wirenote-ui.service"] in ran


def test_package_ships_templates():
    from importlib.resources import files

    data = files("wirenote") / "data"
    for name in ("wirenote.service", "wirenote-ui.service", "wirenote.desktop"):
        assert "@EXEC@" in (data / name).read_text()


def test_cli_setup_install_dry_run(home, capsys):
    from wirenote import cli

    assert cli.main(["setup", "install", "--dry-run"]) == 0
    assert "wirenote.service" in capsys.readouterr().out
    assert cli.main(["setup", "uninstall", "--dry-run"]) == 0
