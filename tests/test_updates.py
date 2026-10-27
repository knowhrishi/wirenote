# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hrishikesh Jadhav
"""Updates: release notes, "What's new", the update check and `wirenote update`
(docs/dev/updates-design.md). The network and the installer are faked."""

from __future__ import annotations

import json
import os
import subprocess
import urllib.error
from pathlib import Path

import pytest

from wirenote import updates

CHANGELOG = """# Changelog

Intro.

## [Unreleased]

### Added

- Not out yet.

## [0.3.0] - 2026-11-20

### Added

- The `update` command, see [the docs](https://example.org/docs).
- A point that wraps
  onto a second line.

### Fixed

- A fix.

## [0.2.0] - 2026-10-27

### Changed

- Something changed.

## [0.1.0] - 2026-10-26

First public release.

### Added

- Everything.

[0.3.0]: https://example.org/v0.3.0
"""


def pypi(*versions, yanked=(), empty=()):
    """A PyPI JSON reply listing these versions."""
    rel = {v: ([] if v in empty else [{"yanked": v in yanked}]) for v in versions}
    return json.dumps({"info": {"version": versions[-1]}, "releases": rel}).encode()


def fake_get(replies: dict):
    calls = []

    def get(url, timeout=10.0):
        calls.append(url)
        reply = replies.get(url)
        if isinstance(reply, Exception):
            raise reply
        if reply is None:
            raise urllib.error.URLError("unreachable")
        return reply
    get.calls = calls
    return get


@pytest.fixture
def changelog(monkeypatch):
    monkeypatch.setattr(updates, "changelog_text", lambda: CHANGELOG)


# Versions and the changelog ---------------------------------------------------------

def test_version_order():
    order = ["0.2.0", "0.3.0.dev1", "0.3.0a1", "0.3.0b2", "0.3.0rc1", "0.3.0", "0.3.0.post1",
             "0.3.1", "0.10.0", "1.0"]
    assert sorted(order, key=updates.version_key) == order
    assert updates.newer("0.3.0", "0.2.9") and not updates.newer("0.2.0", "0.2.0")
    assert not updates.newer("garbage", "0.1.0") and not updates.newer(None, "0.1.0")
    assert updates.is_prerelease("0.3.0rc1") and not updates.is_prerelease("0.3.0")


def test_parse_changelog_leaves_out_unreleased_and_joins_wrapped_lines():
    rels = updates.parse_changelog(CHANGELOG)
    assert [r.version for r in rels] == ["0.3.0", "0.2.0", "0.1.0"]
    added = dict(rels[0].groups)["Added"]
    assert added[1] == "A point that wraps onto a second line."
    assert rels[2].intro == "First public release." and rels[0].date == "2026-11-20"
    assert updates.Release.from_dict(rels[0].as_dict()) == rels[0]


def test_between():
    rels = updates.parse_changelog(CHANGELOG)
    assert [r.version for r in updates.between(rels, "0.1.0", "0.3.0")] == ["0.3.0", "0.2.0"]
    assert [r.version for r in updates.between(rels, None, "0.2.0")] == ["0.2.0"]
    assert updates.between(rels, "0.3.0", "0.3.0") == []


def test_the_app_ships_its_own_changelog():
    """From the wheel (scripts/test_wheel.sh) or the checkout: this version has notes."""
    versions = [r.version for r in updates.releases()]
    assert "0.1.0" in versions and "Unreleased" not in versions


def test_render_text(changelog):
    text = updates.render_text(updates.between(updates.releases(), "0.2.0", "0.3.0"))
    assert text.startswith("Wirenote 0.3.0 (2026-11-20)\n  Added\n  - The `update` command")


# What's new -----------------------------------------------------------------------------

def test_new_install_sees_nothing_and_is_recorded(tmp_path, changelog):
    assert updates.whats_new(tmp_path, "0.3.0", has_data=False) is None
    assert (tmp_path / updates.SEEN).read_text().strip() == "0.3.0"


def test_upgrade_without_a_record_shows_the_current_release(tmp_path, changelog):
    w = updates.whats_new(tmp_path, "0.3.0", has_data=True)
    assert w["from"] is None and [r["version"] for r in w["releases"]] == ["0.3.0"]


def test_upgrade_shows_every_release_since_the_seen_one_until_marked(tmp_path, changelog):
    updates.mark_seen(tmp_path, "0.1.0")
    w = updates.whats_new(tmp_path, "0.3.0", has_data=True)
    assert w["from"] == "0.1.0" and [r["version"] for r in w["releases"]] == ["0.3.0", "0.2.0"]
    updates.mark_seen(tmp_path, "0.3.0")
    assert updates.whats_new(tmp_path, "0.3.0", has_data=True) is None


def test_rollback_shows_nothing_and_resets_the_record(tmp_path, changelog):
    updates.mark_seen(tmp_path, "0.3.0")
    assert updates.whats_new(tmp_path, "0.2.0", has_data=True) is None
    assert (tmp_path / updates.SEEN).read_text().strip() == "0.2.0"


# The check -----------------------------------------------------------------------------

def test_latest_skips_yanked_empty_and_prereleases():
    get = fake_get({updates.PYPI_URL: pypi("0.2.0", "0.2.1", "0.3.0rc1", "0.2.2",
                                           yanked=("0.2.2",), empty=("0.2.1",))})
    assert updates.latest_on_pypi("0.2.0", get) == "0.2.0"
    assert updates.latest_on_pypi("0.3.0a1", get) == "0.3.0rc1"


def test_check_caches_the_newer_version_with_its_notes(tmp_path):
    notes_url = updates.NOTES_URL.format(version="0.3.0")
    get = fake_get({updates.PYPI_URL: pypi("0.1.0", "0.2.0", "0.3.0"),
                    notes_url: CHANGELOG.encode()})
    found = updates.check(tmp_path, "0.1.0", get=get, now=1000.0)
    assert found["latest"] == "0.3.0" and found["error"] is None
    assert [r["version"] for r in found["notes"]] == ["0.3.0", "0.2.0"]
    avail = updates.available(tmp_path, "0.1.0")
    assert avail["version"] == "0.3.0" and avail["url"].endswith("/v0.3.0")
    assert updates.available(tmp_path, "0.3.0") is None
    assert not updates.check_due(tmp_path, now=1000.0 + 3600)
    assert updates.check_due(tmp_path, now=1000.0 + updates.CHECK_EVERY_S)


def test_check_sends_nothing_about_the_install(tmp_path, monkeypatch):
    seen = []

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, n):
            return pypi("0.1.0")

    def urlopen(req, timeout):
        seen.append((req.full_url, dict(req.header_items())))
        return Resp()

    monkeypatch.setattr(updates.urllib.request, "urlopen", urlopen)
    updates.check(tmp_path, "0.1.0")
    (url, headers), = seen
    assert url == updates.PYPI_URL and "?" not in url
    assert headers == {"User-agent": "wirenote"}


def test_check_without_network_is_an_error_not_a_crash(tmp_path):
    found = updates.check(tmp_path, "0.1.0", get=fake_get({}))
    assert found["latest"] is None and "unreachable" in found["error"]
    assert updates.available(tmp_path, "0.1.0") is None


def test_missing_notes_still_offer_the_update(tmp_path):
    get = fake_get({updates.PYPI_URL: pypi("0.1.0", "0.3.0")})
    updates.check(tmp_path, "0.1.0", get=get)
    assert updates.available(tmp_path, "0.1.0")["notes"] == []


def test_notified_survives_the_next_check(tmp_path):
    get = fake_get({updates.PYPI_URL: pypi("0.1.0", "0.3.0")})
    updates.check(tmp_path, "0.1.0", get=get)
    updates.mark_notified(tmp_path, "0.3.0")
    updates.check(tmp_path, "0.1.0", get=get)
    assert updates.last_check(tmp_path)["notified"] == "0.3.0"


# How it was installed --------------------------------------------------------------------

class Dist:
    def __init__(self, direct):
        self.direct = direct

    def read_text(self, name):
        return json.dumps(self.direct) if self.direct is not None else None


@pytest.mark.parametrize("direct", [
    {"url": "file:///src/wirenote", "dir_info": {"editable": True}},
    {"url": "https://github.com/x/y", "vcs_info": {"vcs": "git"}},
    {"url": "file:///tmp/dist/wirenote-0.2.0-py3-none-any.whl", "archive_info": {}},
])
def test_source_and_local_installs_are_not_updated_from_pypi(tmp_path, direct):
    (tmp_path / "uv-receipt.toml").write_text("")
    assert updates.install_kind(tmp_path, Dist(direct)).kind == "source"


def test_uv_pipx_and_other(tmp_path):
    assert updates.install_kind(tmp_path, Dist(None)).kind == "other"
    (tmp_path / "pipx_metadata.json").write_text("{}")
    assert updates.install_kind(tmp_path, Dist(None)).kind == "pipx"
    (tmp_path / "uv-receipt.toml").write_text("")
    assert updates.install_kind(tmp_path, Dist(None)).kind == "uv"


def test_upgrade_argv_drops_a_pin_for_latest_and_sets_one_for_a_version(monkeypatch, tmp_path):
    monkeypatch.setattr(updates.shutil, "which", lambda n: f"/bin/{n}")
    uv = updates.Install("uv", tmp_path)
    assert updates.upgrade_argv(uv, None) == ["/bin/uv", "tool", "install", "--upgrade", "wirenote"]
    assert updates.upgrade_argv(uv, "0.2.0") == ["/bin/uv", "tool", "install", "wirenote==0.2.0"]
    with pytest.raises(updates.UpdateError, match="git pull"):
        updates.upgrade_argv(updates.Install("source", tmp_path), None)


# wirenote update -------------------------------------------------------------------------

@pytest.fixture
def env(tmp_path, monkeypatch, changelog):
    """A uv install of version 0.1.0 with PyPI offering 0.3.0; every command is recorded."""
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setattr(updates, "__version__", "0.1.0")
    monkeypatch.setattr(updates, "install_kind", lambda: updates.Install("uv", tmp_path))
    monkeypatch.setattr(updates, "find_tool", lambda n: f"/bin/{n}")
    monkeypatch.setattr(updates, "_units_installed", lambda: True)
    monkeypatch.setattr(updates, "_services_active", lambda timeout_s=30.0: True)
    get = fake_get({updates.PYPI_URL: pypi("0.1.0", "0.2.0", "0.3.0"),
                    updates.NOTES_URL.format(version="0.3.0"): CHANGELOG.encode()})
    monkeypatch.setattr(updates, "_get", get)
    ran, rc = [], {}

    def run(argv, capture=False):
        ran.append(argv)
        return subprocess.CompletedProcess(argv, rc.get(argv[1], 0), "Wirenote 0.3.0\n  notes", "")

    monkeypatch.setattr(updates, "_run", run)
    out = []
    return state, ran, rc, out


def status(state: Path, recording: str | None) -> None:
    (state / "status.json").write_text(json.dumps({"running": True, "pid": os.getpid(),
                                                   "recording": recording}))


def test_update_installs_restarts_and_shows_the_notes(env):
    state, ran, _, out = env
    assert updates.update(state, out=out.append) == 0
    assert ran == [["/bin/uv", "tool", "install", "--upgrade", "wirenote"],
                   ["/bin/wirenote", "setup", "install"],
                   ["/bin/wirenote", "doctor"],
                   ["/bin/wirenote", "changelog", "--since", "0.1.0"]]
    assert updates.run_state(state)["state"] == "done"
    assert (state / updates.SEEN).read_text().strip() == "0.1.0"   # the card shows 0.2.0 and 0.3.0
    assert "To go back: wirenote update --to 0.1.0" in out[-1]
    assert "\nWhat's new:\nWirenote 0.3.0\n  notes" in out


def test_update_to_a_version_without_changelog_shows_the_checked_notes(env):
    state, ran, rc, out = env
    rc["changelog"] = 2
    assert updates.update(state, out=out.append) == 0
    (shown,) = [o for o in out if o.startswith("\nWhat's new:")]
    assert "Wirenote 0.3.0 (2026-11-20)" in shown and "Wirenote 0.2.0" in shown


def test_update_does_nothing_when_up_to_date(env, monkeypatch):
    state, ran, _, out = env
    monkeypatch.setattr(updates, "__version__", "0.3.0")
    assert updates.update(state, out=out.append) == 0
    assert ran == [] and "newest version" in out[-1]


def test_update_never_ends_a_recording(env):
    state, ran, _, out = env
    status(state, "discord")
    assert updates.update(state, out=out.append) == 1
    assert ran == [] and "--wait" in out[-1]


def test_update_wait_starts_once_the_recording_ends(env, monkeypatch):
    state, ran, _, out = env
    status(state, "discord")
    slept = []

    def sleep(s):
        slept.append(s)
        status(state, None)

    monkeypatch.setattr(updates.time, "sleep", sleep)
    assert updates.update(state, wait=True, out=out.append) == 0
    assert slept == [updates.WAIT_POLL_S] and ran[0][:3] == ["/bin/uv", "tool", "install"]


def test_failed_install_changes_nothing_else(env):
    state, ran, rc, out = env
    rc["tool"] = 1
    assert updates.update(state, out=out.append) == 1
    assert len(ran) == 1 and updates.run_state(state)["state"] == "failed"
    assert "0.1.0 is still installed" in out[-1]


def test_source_checkout_is_refused(env, monkeypatch, tmp_path):
    state, ran, _, out = env
    monkeypatch.setattr(updates, "install_kind", lambda: updates.Install("source", tmp_path))
    assert updates.update(state, out=out.append) == 1
    assert ran == [] and "git pull" in out[-1]


def test_going_back_asks_first(env, monkeypatch):
    state, ran, _, out = env
    monkeypatch.setattr(updates, "__version__", "0.3.0")
    monkeypatch.setattr(updates.sys.stdin, "isatty", lambda: True, raising=False)
    assert updates.update(state, to="0.2.0", confirm=lambda q: "n", out=out.append) == 1
    assert ran == []
    assert updates.update(state, to="0.2.0", yes=True, out=out.append) == 0
    assert ran[0] == ["/bin/uv", "tool", "install", "wirenote==0.2.0"]
    assert ["/bin/wirenote", "changelog", "--since", "0.3.0"] not in ran   # 0.2.0 has no such command


def test_a_dead_update_counts_as_failed(tmp_path):
    (tmp_path / updates.RUN).write_text(json.dumps({"state": "installing", "pid": 2 ** 22 + 1}))
    assert updates.run_state(tmp_path)["state"] == "failed"
