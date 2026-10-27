# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hrishikesh Jadhav
"""Updates: the changelog the app ships with, "What's new" after an update, the
opt-in check for a newer release, and `wirenote update` (docs/dev/updates-design.md).

Nothing here touches the network unless the user turned the check on, or asked
for it (`wirenote update`, Check now, Update).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from importlib import metadata
from importlib.resources import files
from pathlib import Path

from . import __version__

PACKAGE = "wirenote"
PYPI_URL = "https://pypi.org/pypi/wirenote/json"
NOTES_URL = "https://raw.githubusercontent.com/knowhrishi/wirenote/v{version}/CHANGELOG.md"
RELEASE_URL = "https://github.com/knowhrishi/wirenote/releases/tag/v{version}"
# No version and no id: the request says nothing about this install.
USER_AGENT = "wirenote"
# docs/dev/PLAN.md › Defaults › Update check: once a day when on
CHECK_EVERY_S = 24 * 3600.0
# › Waiting for a call before updating: at most the 4 h recording cap
WAIT_FOR_CALL_S = 4 * 3600.0
WAIT_POLL_S = 15.0
MAX_RESPONSE = 4 << 20

SEEN = "seen-version"
CHECK = "update-check.json"
RUN = "update.json"
ACTIVE = ("starting", "waiting", "installing", "restarting")


class UpdateError(Exception):
    pass


# Versions ------------------------------------------------------------------

_VERSION = re.compile(r"^v?(\d+)\.(\d+)(?:\.(\d+))?(?:(a|b|rc)(\d+))?(?:\.post(\d+))?"
                      r"(?:\.dev(\d+))?(?:\+[\w.]+)?$")
_STAGE = {"a": 0, "b": 1, "rc": 2}


def version_key(v: str) -> tuple:
    """Sortable form of a release number: 0.3.0.dev1 < 0.3.0a1 < 0.3.0rc1 < 0.3.0 < 0.3.0.post1."""
    m = _VERSION.match(str(v).strip())
    if not m:
        raise ValueError(f"not a version: {v!r}")
    major, minor, patch, pre, pre_n, post, dev = m.groups()
    if pre:
        stage = (_STAGE[pre], int(pre_n))
    elif dev is not None and post is None:
        stage = (-1, 0)
    else:
        stage = (3, 0)
    return (int(major), int(minor), int(patch or 0), stage, int(post or -1),
            int(dev) if dev is not None else float("inf"))


def is_prerelease(v: str) -> bool:
    m = _VERSION.match(str(v).strip())
    return bool(m and (m.group(4) or m.group(7) is not None))


def newer(a: str | None, b: str | None) -> bool:
    """a is a later release than b; anything unreadable is not."""
    try:
        return a is not None and b is not None and version_key(a) > version_key(b)
    except ValueError:
        return False


# Changelog -------------------------------------------------------------------

@dataclass(frozen=True)
class Release:
    version: str
    date: str
    intro: str
    groups: tuple[tuple[str, tuple[str, ...]], ...]   # ("Added", ("item", ...)), ...

    def as_dict(self) -> dict:
        return {"version": self.version, "date": self.date, "intro": self.intro,
                "groups": [{"title": t, "items": list(items)} for t, items in self.groups]}

    @classmethod
    def from_dict(cls, d: dict) -> Release:
        return cls(str(d["version"]), str(d.get("date", "")), str(d.get("intro", "")),
                   tuple((str(g["title"]), tuple(map(str, g["items"]))) for g in d.get("groups", [])))


_HEAD = re.compile(r"^## \[([^\]]+)\](?: - (\d{4}-\d{2}-\d{2}))?\s*$")
_LINK_REF = re.compile(r"^\[[^\]]+\]: \S+$")


def parse_changelog(text: str) -> list[Release]:
    """The released sections of a Keep a Changelog file, newest first; Unreleased is left out."""
    out: list[Release] = []
    cur: dict | None = None

    def flush():
        if cur and cur["version"].lower() != "unreleased":
            out.append(Release(cur["version"], cur["date"], " ".join(cur["intro"]),
                               tuple((t, tuple(items)) for t, items in cur["groups"] if items)))

    for line in text.splitlines():
        m = _HEAD.match(line)
        if m:
            flush()
            cur = {"version": m.group(1), "date": m.group(2) or "", "intro": [], "groups": []}
            continue
        if cur is None or _LINK_REF.match(line):
            continue
        if line.startswith("### "):
            cur["groups"].append((line[4:].strip(), []))
        elif line.startswith("- ") and cur["groups"]:
            cur["groups"][-1][1].append(line[2:].strip())
        elif line.startswith(" ") and line.strip() and cur["groups"] and cur["groups"][-1][1]:
            # A wrapped line or a nested point belongs to the item above.
            items = cur["groups"][-1][1]
            items[-1] += " " + line.strip().removeprefix("- ")
        elif line.strip() and not cur["groups"]:
            cur["intro"].append(line.strip())
    flush()
    return out


def changelog_text() -> str:
    """The changelog of the running version: shipped in the wheel, or the checkout's own."""
    packaged = files(PACKAGE) / "data" / "CHANGELOG.md"
    if packaged.is_file():
        return packaged.read_text()
    source = Path(__file__).resolve().parents[2] / "CHANGELOG.md"
    return source.read_text() if source.is_file() else ""


def releases() -> list[Release]:
    return parse_changelog(changelog_text())


def between(rels: list[Release], after: str | None, upto: str) -> list[Release]:
    """Releases later than after and no later than upto, newest first; after=None
    means upto alone."""
    out = []
    for r in rels:
        try:
            k = version_key(r.version)
            if k > version_key(upto):
                continue
            if after is None and k != version_key(upto):
                continue
            if after is not None and k <= version_key(after):
                continue
        except ValueError:
            continue
        out.append(r)
    return out


def render_text(rels: list[Release]) -> str:
    """Release notes for the terminal."""
    blocks = []
    for r in rels:
        lines = [f"Wirenote {r.version}" + (f" ({r.date})" if r.date else "")]
        if r.intro:
            lines.append(f"  {r.intro}")
        for title, items in r.groups:
            lines.append(f"  {title}")
            lines += [f"  - {item}" for item in items]
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


# State files -------------------------------------------------------------------

def _read(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(path)


def mark_seen(state_dir: Path, version: str | None = None) -> None:
    version = version or __version__
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    (state_dir / SEEN).write_text(version + "\n")


def whats_new(state_dir: Path, current: str | None = None, has_data: bool = False) -> dict | None:
    """The releases since the version the user last saw, if they have not seen them.

    Without a record (the first version with this feature) existing recordings
    mean an upgrade, so the current release is shown; without any it is a new
    install, which has nothing to catch up on."""
    current = current or __version__
    try:
        seen = (state_dir / SEEN).read_text().strip()
    except OSError:
        seen = ""
    if not seen:
        if not has_data:
            mark_seen(state_dir, current)
            return None
        rels = between(releases(), None, current)
    elif newer(current, seen):
        rels = between(releases(), seen, current)
    else:
        if seen != current:      # rolled back, or an unreadable record
            mark_seen(state_dir, current)
        return None
    if not rels:
        return None
    return {"from": seen or None, "to": current, "releases": [r.as_dict() for r in rels]}


# The check -----------------------------------------------------------------------

def _get(url: str, timeout: float = 10.0) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(MAX_RESPONSE)


def latest_on_pypi(current: str, get: Callable[[str], bytes] = _get) -> str:
    """The newest release with files that are not yanked. Pre-releases count only
    for someone already running one."""
    data = json.loads(get(PYPI_URL))
    allow_pre = is_prerelease(current)
    best: tuple | None = None
    for v, dists in (data.get("releases") or {}).items():
        if not dists or all(d.get("yanked") for d in dists):
            continue
        if is_prerelease(v) and not allow_pre:
            continue
        try:
            k = version_key(v)
        except ValueError:
            continue
        if best is None or k > best[0]:
            best = (k, v)
    if best is None:
        raise ValueError("PyPI lists no release")
    return best[1]


def notes_for(latest: str, current: str, get: Callable[[str], bytes] = _get) -> list[dict]:
    """The new releases' notes from the changelog at latest's tag; none if unreachable."""
    try:
        text = get(NOTES_URL.format(version=latest)).decode()
    except (OSError, ValueError):
        return []
    return [r.as_dict() for r in between(parse_changelog(text), current, latest)]


def _describe(e: Exception) -> str:
    reason = getattr(e, "reason", None)
    return str(reason or e)[:200] or type(e).__name__


def check(state_dir: Path, current: str | None = None, get: Callable[[str], bytes] | None = None,
          now: float | None = None) -> dict:
    """Ask PyPI for the newest version now; the result is cached for the app and doctor."""
    current = current or __version__
    get = get or _get
    result = {"checked": time.time() if now is None else now, "current": current,
              "latest": None, "notes": [], "error": None}
    try:
        latest = latest_on_pypi(current, get)
        result["latest"] = latest
        if newer(latest, current):
            result["notes"] = notes_for(latest, current, get)
    except (OSError, ValueError) as e:
        result["error"] = _describe(e)
    result["notified"] = (_read(state_dir / CHECK) or {}).get("notified")
    _write(state_dir / CHECK, result)
    return result


def last_check(state_dir: Path) -> dict:
    return _read(state_dir / CHECK) or {}


def check_due(state_dir: Path, now: float | None = None) -> bool:
    now = time.time() if now is None else now
    checked = last_check(state_dir).get("checked") or 0
    return not 0 <= now - checked < CHECK_EVERY_S


def available(state_dir: Path, current: str | None = None) -> dict | None:
    """The newer version the last check found, if there is one."""
    current = current or __version__
    c = last_check(state_dir)
    latest = c.get("latest")
    if not newer(latest, current):
        return None
    return {"version": latest, "notes": c.get("notes") or [],
            "url": RELEASE_URL.format(version=latest), "checked": c.get("checked")}


def mark_notified(state_dir: Path, version: str) -> None:
    c = last_check(state_dir)
    c["notified"] = version
    _write(state_dir / CHECK, c)


# How it was installed ----------------------------------------------------------------

@dataclass(frozen=True)
class Install:
    kind: str        # "uv" | "pipx" | "source" | "other"
    prefix: Path


def install_kind(prefix: Path | None = None, dist=None) -> Install:
    prefix = Path(prefix or sys.prefix)
    try:
        raw = (dist or metadata.distribution(PACKAGE)).read_text("direct_url.json")
        direct = json.loads(raw) if raw else None
    except (metadata.PackageNotFoundError, ValueError):
        direct = None
    if isinstance(direct, dict) and ((direct.get("dir_info") or {}).get("editable")
                                     or "vcs_info" in direct
                                     or str(direct.get("url", "")).startswith("file:")):
        return Install("source", prefix)
    if (prefix / "uv-receipt.toml").is_file():
        return Install("uv", prefix)
    if (prefix / "pipx_metadata.json").is_file():
        return Install("pipx", prefix)
    return Install("other", prefix)


def how_to_update(inst: Install) -> str:
    if inst.kind in ("uv", "pipx"):
        return "wirenote update"
    if inst.kind == "source":
        return ("Wirenote runs from a source checkout or a local file: update it the way you "
                "installed it, for example `git pull` and `uv tool install --force .`, then "
                "`wirenote setup install`.")
    return ("Wirenote was not installed with uv or pipx: run `pip install --upgrade wirenote` "
            "in its environment, then `wirenote setup install`.")


def find_tool(name: str) -> str | None:
    """On PATH, or where uv and pipx put programs: systemd's PATH often lacks ~/.local/bin."""
    found = shutil.which(name)
    if found:
        return found
    for d in (Path.home() / ".local" / "bin", Path.home() / ".cargo" / "bin"):
        p = d / name
        if p.is_file() and os.access(p, os.X_OK):
            return str(p)
    return None


def upgrade_argv(inst: Install, to: str | None) -> list[str]:
    """The installer command: the newest version, or exactly `to`."""
    if inst.kind == "uv":
        uv = find_tool("uv")
        if not uv:
            raise UpdateError("uv is not on PATH; install it or run `uv tool upgrade wirenote` yourself")
        # `uv tool upgrade` keeps an earlier pin (a rollback); `install --upgrade` drops it.
        return [uv, "tool", "install", f"{PACKAGE}=={to}"] if to \
            else [uv, "tool", "install", "--upgrade", PACKAGE]
    if inst.kind == "pipx":
        pipx = find_tool("pipx")
        if not pipx:
            raise UpdateError("pipx is not on PATH")
        return [pipx, "install", "--force", f"{PACKAGE}=={to}" if to else PACKAGE]
    raise UpdateError(how_to_update(inst))


# Updating --------------------------------------------------------------------------

def recording_now(state_dir: Path) -> str | None:
    st = _read(state_dir / "status.json") or {}
    pid = st.get("pid")
    if st.get("running") and st.get("recording") and pid and Path(f"/proc/{pid}").exists():
        return str(st["recording"])
    return None


def progress(state_dir: Path, state: str, **extra) -> None:
    _write(state_dir / RUN, {"state": state, "updated": time.time(), "pid": os.getpid(), **extra})


def run_state(state_dir: Path) -> dict | None:
    """The last update's progress; one whose process died counts as failed."""
    run = _read(state_dir / RUN)
    if run and run.get("state") in ACTIVE and run.get("pid") \
            and not Path(f"/proc/{run['pid']}").exists():
        run = {**run, "state": "failed", "message": "the update stopped unexpectedly"}
    return run


def _units_installed() -> bool:
    from .tools.install import UNITS, _units_dir

    return (_units_dir() / UNITS[0]).is_file()


def _services_active(timeout_s: float = 30.0) -> bool:
    from .tools.install import UNITS

    deadline = time.monotonic() + timeout_s
    while True:
        if all(_run(["systemctl", "--user", "is-active", "--quiet", u]).returncode == 0
               for u in UNITS):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(1.0)


def _run(argv: list[str], capture: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(argv, check=False, capture_output=capture, text=capture)


def _say(text: str) -> None:
    print(text, flush=True)     # in order with the installer's output, also when piped


def wirenote_exe() -> str:
    return find_tool("wirenote") or str(Path(sys.argv[0]).resolve())


def update(state_dir: Path, to: str | None = None, wait: bool = False, yes: bool = False,
           confirm: Callable[[str], str] = input, out: Callable[[str], None] = _say) -> int:
    current = __version__
    inst = install_kind()
    if inst.kind not in ("uv", "pipx"):
        out(how_to_update(inst))
        return 1
    if to:
        try:
            version_key(to)
        except ValueError:
            out(f"wirenote: {to!r} is not a version number")
            return 2
        if to == current:
            out(f"Wirenote {to} is already installed.")
            return 0
        if newer(current, to):
            out(f"{to} is older than {current}. An older version may not read everything a newer "
                "one saved, such as settings it did not know yet.")
            if not yes and (not sys.stdin.isatty()
                            or confirm("Install it anyway? [y/N] ").strip().lower() not in ("y", "yes")):
                out("Nothing changed.")
                return 1
        target = to
    else:
        out("Looking for a new version on PyPI…")
        found = check(state_dir, current)
        if found["error"]:
            out(f"wirenote: cannot reach PyPI ({found['error']}); nothing changed")
            return 1
        if not newer(found["latest"], current):
            out(f"Wirenote {current} is the newest version.")
            return 0
        target = found["latest"]

    if recording_now(state_dir):
        if not wait:
            out("A call is being recorded; updating restarts Wirenote and would end it. Run "
                "`wirenote update` after the call, or `wirenote update --wait` to update when it ends.")
            return 1
        progress(state_dir, "waiting", **{"from": current, "to": target})
        out("Waiting for the recording to end…")
        deadline = time.monotonic() + WAIT_FOR_CALL_S
        while recording_now(state_dir):
            if time.monotonic() >= deadline:
                progress(state_dir, "failed", **{"from": current, "to": target,
                                                 "message": "the call was still recording after 4 h"})
                out("wirenote: the call is still recording; nothing changed")
                return 1
            time.sleep(WAIT_POLL_S)

    if not (state_dir / SEEN).exists():
        mark_seen(state_dir, current)    # so "What's new" lists everything since this version
    progress(state_dir, "installing", **{"from": current, "to": target})
    out(f"Installing Wirenote {target} (you have {current})…")
    try:
        argv = upgrade_argv(inst, to)
    except UpdateError as e:
        progress(state_dir, "failed", **{"from": current, "to": target, "message": str(e)})
        out(f"wirenote: {e}")
        return 1
    if _run(argv).returncode != 0:
        progress(state_dir, "failed", **{"from": current, "to": target,
                                         "message": "the installer failed; nothing changed"})
        out(f"wirenote: {' '.join(argv)} failed; Wirenote {current} is still installed")
        return 1

    exe = wirenote_exe()
    if _units_installed():
        progress(state_dir, "restarting", **{"from": current, "to": target})
        out("Restarting the background services…")
        if _run([exe, "setup", "install"]).returncode != 0 or not _services_active():
            out("! the services did not come back; see: journalctl --user -u wirenote -u wirenote-ui")
    _run([exe, "doctor"])
    if newer(target, current):
        # The new version's own notes; a version without `changelog` gets the checked ones.
        shown = _run([exe, "changelog", "--since", current], capture=True)
        notes = shown.stdout.strip() if shown.returncode == 0 else render_text(
            [Release.from_dict(r) for r in last_check(state_dir).get("notes") or []])
        if notes:
            out("\nWhat's new:\n" + notes)
    progress(state_dir, "done", **{"from": current, "to": target})
    out(f"\n✓ Updated Wirenote {current} → {target}. To go back: wirenote update --to {current}")
    return 0
