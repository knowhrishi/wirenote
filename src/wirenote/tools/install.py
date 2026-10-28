# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hrishikesh Jadhav
"""`wirenote setup install` / `uninstall` / `search`: systemd user units, the app-menu entry
and its icon, and the launcher search (KRunner's plugin and the D-Bus service that starts
it), so nobody copies files by hand.

The launcher search is off until you turn it on (launcher_search, docs/dev/PLAN.md › Defaults
› Launcher search): install writes its two files only when it is on, and removes them when
it is off. Nothing here needs root: everything goes to the user's own config and data
folders. GNOME's search provider file is the exception: GNOME reads it only from system
folders, so it is a documented sudo step (docs/usage.md › Find a meeting from the launcher),
and uninstall and `setup search --off` only say how to remove it.
"""

from __future__ import annotations

import re
import shlex
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path

from .. import APP_ID
from ..config.secrets import set_config_value
from ..config.settings import config_dir, data_dir, load_settings, state_dir
from ..notify import search_provider

UNITS = ("wirenote.service", "wirenote-ui.service")
OLD_UNITS = ("meetnote.service", "meetnote-ui.service")


@dataclass(frozen=True)
class Action:
    kind: str          # "write" | "run" | "try" (a command whose failure is only noted) | "remove"
    target: str        # a path, or a description of the command
    payload: object    # file text, or argv
    note: str = ""     # "try": what its failure means for you


SEARCH_ON_LATER = "the launcher search starts working after your next login"
SEARCH_OFF_LATER = "the launcher may still list Wirenote until your next login, and finds nothing"


def _units_dir() -> Path:
    return config_dir().parent / "systemd" / "user"


def _xdg_data() -> Path:
    return data_dir().parent


def _desktop_path() -> Path:
    # Named for the app id: the shortcuts portal finds Wirenote's desktop entry by it
    return _xdg_data() / "applications" / f"{APP_ID}.desktop"


def _old_desktop_path() -> Path:
    """Its earlier name; left in place it would list Wirenote twice in the app menu."""
    return _xdg_data() / "applications" / "wirenote.desktop"


def _icon_path() -> Path:
    return _xdg_data() / "icons" / "hicolor" / "scalable" / "apps" / "wirenote.svg"


def _krunner_path() -> Path:
    return _xdg_data() / "krunner" / "dbusplugins" / f"{APP_ID}.desktop"


def _dbus_service_path() -> Path:
    # The bus looks the name up by the file's name (dbus-broker requires the two to match)
    return _xdg_data() / "dbus-1" / "services" / f"{search_provider.BUS_NAME}.service"


def _reload_bus(search_on: bool) -> Action:
    """The session bus learns of a new or removed service file now, not at the next login."""
    argv = ["busctl", "--user", "call", "org.freedesktop.DBus", "/org/freedesktop/DBus",
            "org.freedesktop.DBus", "ReloadConfig"]
    return Action("try", " ".join(argv), argv, SEARCH_ON_LATER if search_on else SEARCH_OFF_LATER)


def _search_files(exe: str, on: bool) -> list[Action]:
    """Written when the launcher search is on; removed, if there, when it is off."""
    if on:
        return [Action("write", str(_krunner_path()), _template("krunner-plugin.desktop", exe)),
                Action("write", str(_dbus_service_path()),
                       _template(f"{search_provider.BUS_NAME}.service", exe))]
    return [Action("remove", str(p), None) for p in (_krunner_path(), _dbus_service_path()) if p.exists()]


def _launcher_search_on() -> bool:
    return load_settings(config_dir() / "config.toml").launcher_search


def _exe() -> str:
    found = shutil.which("wirenote")
    return found or str(Path(sys.argv[0]).resolve())


def _run(argv: list[str]) -> int:
    return subprocess.run(argv, check=False).returncode


def _template(name: str, exe: str) -> str:
    quoted = f'"{exe}"' if " " in exe else exe
    return (files("wirenote") / "data" / name).read_text().replace("@EXEC@", quoted)


def _systemctl(*args: str) -> Action:
    argv = ["systemctl", "--user", *args]
    return Action("run", " ".join(argv), argv)


def plan_install(exe: str) -> list[Action]:
    search_on = _launcher_search_on()
    units = _units_dir()
    actions = [Action("write", str(units / u), _template(u, exe)) for u in UNITS]
    actions.append(Action("write", str(_desktop_path()), _template(f"{APP_ID}.desktop", exe)))
    if _old_desktop_path().exists():
        actions.append(Action("remove", str(_old_desktop_path()), None))
    actions.append(Action("write", str(_icon_path()),
                          (files("wirenote") / "ui" / "static" / "icon.svg").read_text()))
    actions += _search_files(exe, search_on)
    actions.append(_systemctl("daemon-reload"))
    if any((units / u).exists() for u in OLD_UNITS):
        # Before enabling ours: the old UI holds the same port.
        actions.append(_systemctl("disable", "--now", *OLD_UNITS))
        actions += [Action("remove", str(units / u), None) for u in OLD_UNITS]
        actions.append(_systemctl("daemon-reload"))
    actions.append(_reload_bus(search_on))
    actions.append(_systemctl("enable", *UNITS))
    # Restart rather than start, so an update runs the new code.
    actions.append(_systemctl("restart", *UNITS))
    return actions


def _apply(actions: list[Action], dry_run: bool) -> int:
    failed = 0
    for a in actions:
        if a.kind == "write":
            print(f"write {a.target}")
            if dry_run:
                print("".join(f"    {line}\n" for line in str(a.payload).splitlines()), end="")
                continue
            path = Path(a.target)
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            path.write_text(str(a.payload))
        elif a.kind == "remove":
            print(f"remove {a.target}")
            if not dry_run:
                Path(a.target).unlink(missing_ok=True)
        elif a.kind == "try":
            print(f"run {a.target}")
            if dry_run:
                continue
            try:
                ok = _run(list(a.payload)) == 0
            except OSError:
                ok = False
            if not ok:
                print(f"  ! the session bus did not reload its services: {a.note}")
        else:
            print(f"run {a.target}")
            if not dry_run and _run(list(a.payload)) != 0:
                print(f"  ✗ {a.target} failed")
                failed += 1
    return 1 if failed else 0


UPDATE_QUESTION = ("Check once a day for a new Wirenote version? This asks pypi.org (and github.com "
                   "for the release notes); nothing about you or your calls is sent, but those "
                   "sites see your IP address. You can change it in Settings. [y/N] ")


def ask_update_check(confirm: Callable[[str], str] = input,
                     interactive: Callable[[], bool] = lambda: sys.stdin.isatty()) -> None:
    """Ask once, in a terminal, whether to check for updates; the setting stays off otherwise."""
    path = config_dir() / "config.toml"
    load_settings(path)     # writes the default file on a first install
    if re.search(r"^[ \t]*update_check[ \t]*=", path.read_text(), re.MULTILINE) or not interactive():
        return
    try:
        yes = confirm(UPDATE_QUESTION).strip().lower() in ("y", "yes")
    except EOFError:
        return
    set_config_value(path, "update_check", "true" if yes else "false")
    print("  update check on: Wirenote tells you when a new version is out" if yes else
          "  update check off: run `wirenote update --check` any time")


def install(dry_run: bool = False) -> int:
    rc = _apply(plan_install(_exe()), dry_run)
    if not dry_run and rc == 0:
        ask_update_check()
        print("\n✓ Wirenote runs in the background and starts with your session.")
        print("  Open it from the app menu, or run: wirenote open. Check the setup: wirenote doctor")
    return rc


def uninstall(purge: bool = False, dry_run: bool = False,
              confirm: Callable[[str], str] = input) -> int:
    actions = [_systemctl("disable", "--now", *UNITS)]
    actions += [Action("remove", str(_units_dir() / u), None) for u in UNITS]
    actions += [Action("remove", str(_desktop_path()), None),
                Action("remove", str(_old_desktop_path()), None),
                Action("remove", str(_icon_path()), None),
                Action("remove", str(_krunner_path()), None),
                Action("remove", str(_dbus_service_path()), None),
                _systemctl("daemon-reload"), _reload_bus(False)]
    folders: list[Path] = []
    if purge:
        settings = load_settings(config_dir() / "config.toml")
        folders = [config_dir(), state_dir(), data_dir()]
        if not settings.recordings_path().is_relative_to(data_dir()):
            folders.append(settings.recordings_path())
        print("This deletes all recordings, transcripts, settings and voice prints:")
        for f in folders:
            print(f"  {f}")
        print(f"Your Markdown notes in {settings.notes_path()} are kept.")
        if not dry_run and confirm("Type DELETE to remove them: ").strip() != "DELETE":
            print("nothing removed")
            return 1
    rc = _apply(actions, dry_run)
    _point_at_gnome_ini()
    for f in folders:
        print(f"remove {f}")
        if not dry_run:
            shutil.rmtree(f, ignore_errors=True)
    return rc


def _point_at_gnome_ini() -> None:
    for d in search_provider.GNOME_PROVIDER_DIRS[:1]:    # /usr/share belongs to a package
        if (d / search_provider.GNOME_INI).exists():
            print(f"GNOME's search still lists Wirenote; remove it with: sudo rm {d / search_provider.GNOME_INI}")


def search(on: bool, dry_run: bool = False) -> int:
    """`wirenote setup search [--off]`: set launcher_search, then write or remove KRunner's
    plugin and the D-Bus service file (docs/usage.md › Find a meeting from the launcher)."""
    if not dry_run:
        set_config_value(config_dir() / "config.toml", "launcher_search", "true" if on else "false")
    print(f"✓ launcher_search = {'true' if on else 'false'}")
    rc = _apply([*_search_files(_exe(), on), _reload_bus(on)], dry_run)
    if on:
        target = search_provider.GNOME_PROVIDER_DIRS[0] / search_provider.GNOME_INI
        print("  KDE Plasma: KRunner finds your meetings now. If it does not, run kquitapp6 krunner.")
        print("  GNOME: copy the search provider file once, then log out and in:")
        print(f"    sudo install -Dm644 {shlex.quote(str(search_provider.gnome_ini_path()))} "
              f"{shlex.quote(str(target))}")
    else:
        print("  The launcher search is off: it finds nothing, and its files are removed.")
        _point_at_gnome_ini()
    return rc
