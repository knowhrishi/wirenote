# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hrishikesh Jadhav
"""Open the web UI through a one-time login link.

A launcher (the app menu, `wirenote open`, "Open note") writes a random code as
an empty file in a folder only this user can read, and opens
/login?code=…; the server accepts a code only if that file exists, belongs to
its own user and is fresh. Another user on the machine cannot make one.
"""

from __future__ import annotations

import os
import re
import secrets
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import quote

from ..net.peer import listener_uid

CODE = re.compile(r"^[A-Za-z0-9_-]{32}$")
MAX_AGE_S = 60.0


def runtime_dir() -> Path:
    return Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))


def login_dir() -> Path:
    d = runtime_dir() / "wirenote" / "login"
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(d.parent, 0o700)
    os.chmod(d, 0o700)
    return d


def _sweep(d: Path) -> None:
    """Drop codes nobody redeemed."""
    for f in d.iterdir():
        try:
            if time.time() - f.lstat().st_mtime > MAX_AGE_S:
                f.unlink()
        except OSError:
            pass


def new_login_code() -> str:
    d = login_dir()
    _sweep(d)
    code = secrets.token_urlsafe(24)
    os.close(os.open(d / code, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
    return code


def redeem_login_code(code: str) -> bool:
    """True once for a fresh code of this user's; the code is used up either way."""
    if not CODE.match(code or ""):
        return False
    path = login_dir() / code
    try:
        st = path.lstat()
        path.unlink()
    except OSError:
        return False
    return st.st_uid == os.getuid() and time.time() - st.st_mtime <= MAX_AGE_S


def login_url(port: int, route: str = "#/") -> str:
    return (f"http://127.0.0.1:{port}/login?code={new_login_code()}"
            f"&next={quote(route, safe='')}")


def _xdg_open(target: str) -> None:
    subprocess.Popen(["xdg-open", target], start_new_session=True,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


UI_UNIT = "wirenote-ui.service"


def _ui_unit_installed() -> bool:
    from ..config.settings import config_dir

    return (config_dir().parent / "systemd" / "user" / UI_UNIT).is_file()


def _systemctl(argv: list[str]) -> int:
    return subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL, timeout=15, check=False).returncode


def _spawn_ui() -> None:
    subprocess.Popen([sys.executable, "-m", "wirenote.cli", "ui"], start_new_session=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL)


def _start_ui() -> None:
    """Through its systemd unit when `setup install` put one there: a child of a launcher's
    process (the search provider runs in a unit of its own that the bus stops) would end with
    it. Without the unit, or if systemctl fails, as a process of its own."""
    if _ui_unit_installed():
        try:
            if _systemctl(["systemctl", "--user", "start", UI_UNIT]) == 0:
                return
        except (OSError, subprocess.SubprocessError):
            pass
    _spawn_ui()


def ensure_ui(port: int) -> bool:
    """Start this user's web app if nothing listens on port. True once it is ours that answers
    there; False for nothing, or someone else's server, which must never get the login link."""
    if listener_uid(port) is None:
        _start_ui()
        for _ in range(50):
            if listener_uid(port) is not None:
                break
            time.sleep(0.1)
    return listener_uid(port) == os.getuid()


def open_ui(port: int, route: str = "#/", fallback: str | None = None) -> bool:
    """Open the UI if this user's server listens on port; else open fallback, if any.
    Never opens a server that belongs to someone else."""
    if listener_uid(port) == os.getuid():
        _xdg_open(login_url(port, route))
        return True
    if fallback:
        _xdg_open(fallback)
    return False
