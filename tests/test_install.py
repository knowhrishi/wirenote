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
    desktop = (tmp / "xdg_data_home" / "applications" / "io.github.knowhrishi.wirenote.desktop").read_text()
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
    for name in ("wirenote.service", "wirenote-ui.service", "io.github.knowhrishi.wirenote.desktop"):
        assert "@EXEC@" in (data / name).read_text()
    assert not (data / "wirenote.desktop").is_file()


def test_desktop_entry_is_named_for_the_app_id(home):
    """The shortcuts portal finds Wirenote by its app id: the entry's name must match it,
    and an entry under the old name would show Wirenote twice in the app menu."""
    from wirenote import APP_ID

    tmp, ran = home
    apps = tmp / "xdg_data_home" / "applications"
    apps.mkdir(parents=True)
    (apps / "wirenote.desktop").write_text("[Desktop Entry]\n")
    install.install(dry_run=False)
    assert (apps / f"{APP_ID}.desktop").is_file() and not (apps / "wirenote.desktop").exists()

    (apps / "wirenote.desktop").write_text("[Desktop Entry]\n")
    assert install.uninstall(dry_run=False) == 0
    assert not (apps / f"{APP_ID}.desktop").exists() and not (apps / "wirenote.desktop").exists()


def test_cli_setup_install_dry_run(home, capsys):
    from wirenote import cli

    assert cli.main(["setup", "install", "--dry-run"]) == 0
    assert "wirenote.service" in capsys.readouterr().out
    assert cli.main(["setup", "uninstall", "--dry-run"]) == 0


def test_install_asks_once_about_update_checks(home, monkeypatch):
    from wirenote.config.settings import config_dir, load_settings

    asked = []
    install.ask_update_check(confirm=lambda q: asked.append(q) or "y", interactive=lambda: True)
    assert load_settings(config_dir() / "config.toml").update_check is True
    install.ask_update_check(confirm=lambda q: asked.append(q) or "n", interactive=lambda: True)
    assert len(asked) == 1 and "pypi.org" in asked[0]


def test_install_without_a_terminal_leaves_update_checks_off(home):
    from wirenote.config.settings import config_dir, load_settings

    install.ask_update_check(confirm=lambda q: pytest.fail("asked"), interactive=lambda: False)
    assert load_settings(config_dir() / "config.toml").update_check is False
