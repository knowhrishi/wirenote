# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hrishikesh Jadhav
"""scripts/ci_apt_install.sh: bounded, retried apt installs on CI runners. apt and
everything run as root are faked."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/ci_apt_install.sh"

# Every command the script runs as root is a fake on PATH that logs its arguments.
# `timeout` notes which mirror apt would use, then exits 124 for the apt-get step
# named in FAKE_TIMEOUT, as GNU timeout does.
FAKES = {
    "sudo": 'exec "$@"',
    "timeout": 'shift\n[ -f "$CI_APT_MIRRORS" ] && head -n 1 "$CI_APT_MIRRORS" | cut -f 1 >> "$MIRROR_LOG"\n'
               'case " $* " in *" $FAKE_TIMEOUT "*) exit 124;; esac\nexec "$@"',
    "apt-get": 'echo "apt-get $*" >> "$LOG"\nexit "${FAKE_APT_RC:-0}"',
    "rm": 'echo "rm $*" >> "$LOG"',
    "fuser": 'echo "fuser $*"',
    "ps": 'echo "  4242    05:01 /usr/bin/dpkg --status-fd 23 --configure man-db"',
    "sleep": ":",
}


# The list on GitHub's Ubuntu runners, byte for byte.
MIRRORS = ("http://azure.archive.ubuntu.com/ubuntu/\tpriority:1\n"
           "https://archive.ubuntu.com/ubuntu/\tpriority:2\n"
           "https://security.ubuntu.com/ubuntu/\tpriority:3\n")


def run(tmp_path, mirrors=None, **env):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in FAKES.items():
        (bin_dir / name).write_text(f"#!/bin/sh\n{body}\n")
        (bin_dir / name).chmod(0o755)
    log = tmp_path / "log"
    log.touch()
    mirror_file = tmp_path / "apt-mirrors.txt"
    if mirrors is not None:
        mirror_file.write_text(mirrors)
    full_env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "LOG": str(log),
                "CI_APT_MIRRORS": str(mirror_file), "MIRROR_LOG": str(tmp_path / "mirror-log"),
                "FAKE_TIMEOUT": "none", **env}
    r = subprocess.run(["sh", str(SCRIPT), "ffmpeg"], env=full_env, capture_output=True, text=True)
    return r, log.read_text().splitlines()


def test_install_is_not_quieted_and_skips_man_db(tmp_path):
    r, log = run(tmp_path)
    assert r.returncode == 0, r.stderr
    assert log[0] == "rm -f /var/lib/man-db/auto-update"
    update, install = log[1:]
    assert update.endswith(" update") and install.endswith(" install -y --no-install-recommends ffmpeg")
    assert "-q" not in install.split()
    assert "-qq" not in install.split()


def test_timeout_prints_who_holds_the_locks(tmp_path):
    r, log = run(tmp_path, FAKE_TIMEOUT="install")
    assert r.returncode == 1
    assert r.stderr.count("apt-get install timed out after 300 s") == 3
    assert "fuser -v /var/lib/dpkg/lock-frontend /var/lib/dpkg/lock" in r.stderr
    assert "/usr/bin/dpkg --status-fd 23 --configure man-db" in r.stderr
    assert "apt attempt 3 failed" in r.stderr


def test_other_failures_do_not_print_locks(tmp_path):
    r, _ = run(tmp_path, FAKE_APT_RC="100")
    assert r.returncode == 1
    assert "timed out" not in r.stderr and "fuser" not in r.stderr
    assert "apt attempt 3 failed" in r.stderr


def test_a_timeout_moves_off_the_first_mirror_once(tmp_path):
    r, _ = run(tmp_path, mirrors=MIRRORS, FAKE_TIMEOUT="install")
    assert r.returncode == 1
    used = (tmp_path / "mirror-log").read_text().split()
    azure, archive = "http://azure.archive.ubuntu.com/ubuntu/", "https://archive.ubuntu.com/ubuntu/"
    assert used == [azure, azure] + [archive] * 4
    assert r.stderr.count("next attempts skip the mirror") == 1
    assert (tmp_path / "apt-mirrors.txt").read_text() == MIRRORS.split("\n", 1)[1]


def test_a_single_mirror_is_kept(tmp_path):
    one = "http://azure.archive.ubuntu.com/ubuntu/\tpriority:1\n"
    r, _ = run(tmp_path, mirrors=one, FAKE_TIMEOUT="install")
    assert r.returncode == 1 and "skip the mirror" not in r.stderr
    assert (tmp_path / "apt-mirrors.txt").read_text() == one
