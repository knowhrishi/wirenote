# SPDX-License-Identifier: AGPL-3.0-or-later
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/ci_apt_install.sh"

# Every command the script runs as root is a fake on PATH that logs its arguments.
# `timeout` exits 124 for the apt-get step named in FAKE_TIMEOUT, as GNU timeout does.
FAKES = {
    "sudo": 'exec "$@"',
    "timeout": 'shift\ncase " $* " in *" $FAKE_TIMEOUT "*) exit 124;; esac\nexec "$@"',
    "apt-get": 'echo "apt-get $*" >> "$LOG"\nexit "${FAKE_APT_RC:-0}"',
    "rm": 'echo "rm $*" >> "$LOG"',
    "fuser": 'echo "fuser $*"',
    "ps": 'echo "  4242    05:01 /usr/bin/dpkg --status-fd 23 --configure man-db"',
    "sleep": ":",
}


def run(tmp_path, **env):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in FAKES.items():
        (bin_dir / name).write_text(f"#!/bin/sh\n{body}\n")
        (bin_dir / name).chmod(0o755)
    log = tmp_path / "log"
    log.touch()
    full_env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "LOG": str(log),
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
