# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hrishikesh Jadhav
"""`wirenote setup install` / `uninstall`: systemd user units, the app-menu entry
and its icon, so nobody copies files by hand.

Nothing here needs root: everything goes to the user's own config and data folders.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path

from ..config.secrets import set_config_value
from ..config.settings import config_dir, data_dir, load_settings, state_dir

UNITS = ("wirenote.service", "wirenote-ui.service")
OLD_UNITS = ("meetnote.service", "meetnote-ui.service")


@dataclass(frozen=True)
class Action:
    kind: str          # "write" | "run" | "remove"
    target: str        # a path, or a description of the command
    payload: object    # file text, or argv


def _units_dir() -> Path:
    return config_dir().parent / "systemd" / "user"


def _xdg_data() -> Path:
    return data_dir().parent


def _desktop_path() -> Path:
    return _xdg_data() / "applications" / "wirenote.desktop"


def _icon_path() -> Path:
    return _xdg_data() / "icons" / "hicolor" / "scalable" / "apps" / "wirenote.svg"


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
    units = _units_dir()
    actions = [Action("write", str(units / u), _template(u, exe)) for u in UNITS]
    actions.append(Action("write", str(_desktop_path()), _template("wirenote.desktop", exe)))
    actions.append(Action("write", str(_icon_path()),
                          (files("wirenote") / "ui" / "static" / "icon.svg").read_text()))
    actions.append(_systemctl("daemon-reload"))
    if any((units / u).exists() for u in OLD_UNITS):
        # Before enabling ours: the old UI holds the same port.
        actions.append(_systemctl("disable", "--now", *OLD_UNITS))
        actions += [Action("remove", str(units / u), None) for u in OLD_UNITS]
        actions.append(_systemctl("daemon-reload"))
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
                Action("remove", str(_icon_path()), None), _systemctl("daemon-reload")]
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
    for f in folders:
        print(f"remove {f}")
        if not dry_run:
            shutil.rmtree(f, ignore_errors=True)
    return rc
