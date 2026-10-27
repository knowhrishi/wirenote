# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hrishikesh Jadhav
"""`wirenote setup`, `wirenote calendar` and `wirenote doctor`.

Secrets (API key, calendar links) are read from the terminal, never from the
command line, so they stay out of shell history, and go to secrets.toml.
"""

from __future__ import annotations

import datetime as dt
import getpass
import json
import os
import shutil
import subprocess
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from ..config.secrets import (calendar_sources, load_secrets, save_secrets, secrets_path,
                              set_config_value)
from ..net.peer import listener_uid, trusted_owner
from ..config.settings import Settings, config_dir, load_settings, state_dir

OK, BAD, WARN = "✓", "✗", "!"


def _settings() -> Settings:
    return load_settings(config_dir() / "config.toml")


def _poke_daemon(command: str) -> None:
    from ..detector.daemon import control_socket_path
    import socket

    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(3)
            s.connect(str(control_socket_path()))
            s.sendall(f"{command}\n".encode())
            s.makefile().readline()
    except OSError:
        pass


# setup -------------------------------------------------------------------------

def setup_claude() -> int:
    s = _settings()
    print("Summaries through the Claude API: transcripts are sent to Anthropic.")
    print("Check your employer's policy before using this for work calls.\n")
    print("Without a key, Wirenote uses ANTHROPIC_API_KEY or your `ant auth login` (Claude")
    print("Console). A Claude Pro or Max subscription cannot be used: Anthropic does not allow")
    print("other apps to use claude.ai logins.\n")
    key = getpass.getpass("Anthropic API key (input hidden, empty to keep the current one or "
                          "use your login): ")
    secrets = load_secrets()
    if key.strip():
        secrets["anthropic_api_key"] = key.strip()
    import anthropic

    try:
        client = anthropic.Anthropic(api_key=secrets.get("anthropic_api_key"))
        client.models.retrieve(s.anthropic_model)      # free; checks key and model
    except anthropic.AuthenticationError:
        print(f"{BAD} the key or login was rejected; nothing changed")
        return 1
    except anthropic.NotFoundError:
        print(f"{BAD} model {s.anthropic_model} not available to this key; nothing changed")
        return 1
    except anthropic.APIConnectionError as e:
        print(f"{BAD} cannot reach the API ({e}); nothing changed")
        return 1
    except TypeError:
        # The SDK found no key, token or login ("Could not resolve authentication method").
        print(f"{BAD} no key, and no ANTHROPIC_API_KEY or `ant auth login` found; nothing changed")
        return 1
    save_secrets(secrets)
    set_config_value(config_dir() / "config.toml", "summary_backend", '"anthropic"')
    print(f"{OK} key works with {s.anthropic_model}; saved to {secrets_path()} (mode 600)")
    print(f"{OK} summary_backend = \"anthropic\"; the next note uses it (no restart needed)")
    return 0


def setup_openai() -> int:
    """OpenRouter, OpenAI, or an OpenAI-compatible server on this computer."""
    from ..process.summarize import OpenAIBackend, SummaryError

    s = _settings()
    print("Summaries through an OpenAI-compatible API, such as OpenRouter (Claude, GPT,")
    print("Gemini, Qwen and more with one key), OpenAI, or LM Studio or llama.cpp on this")
    print("computer. Unless the server runs on this computer, transcripts are sent to it.\n")
    url = input(f"API address [{s.openai_url}]: ").strip() or s.openai_url
    model = input(f"Model [{s.openai_model}]: ").strip() or s.openai_model
    if not url.startswith(("https://", "http://")):
        print(f"{BAD} the address starts with https://; nothing changed")
        return 1
    key = getpass.getpass("API key (input hidden; empty to keep the current one, or for a "
                          "server without keys): ").strip()
    secrets = load_secrets()
    if key:
        secrets["openai_api_key"] = key
    backend = OpenAIBackend(url, model, secrets.get("openai_api_key"), timeout_s=60)
    if not backend.local:
        print(f"Transcripts will be sent to {backend.host}. Check your employer's policy first.")
    try:
        # A one-word request checks address, key and model; it costs a tiny fraction of a cent.
        backend.complete_json("Answer with JSON.", "Say ok.", {
            "type": "object", "properties": {"ok": {"type": "string"}}, "required": ["ok"]})
    except (SummaryError, ValueError) as e:
        print(f"{BAD} {e}; nothing changed")
        return 1
    save_secrets(secrets)
    cfg = config_dir() / "config.toml"
    set_config_value(cfg, "openai_url", json.dumps(url))
    set_config_value(cfg, "openai_model", json.dumps(model))
    set_config_value(cfg, "summary_backend", '"openai"')
    print(f"{OK} {model} at {backend.host} answers" + (f"; key saved to {secrets_path()} (mode 600)"
                                                         if key else ""))
    print(f"{OK} summary_backend = \"openai\"; the next note uses it (no restart needed)")
    return 0


def setup_ollama() -> int:
    s = _settings()
    ok, msg = _check_ollama(s)
    print(f"{OK if ok else BAD} {msg}")
    if not ok:
        return 1
    set_config_value(config_dir() / "config.toml", "summary_backend", '"ollama"')
    print(f"{OK} summary_backend = \"ollama\"; the next note uses it (no restart needed)")
    return 0


def setup_consent(text: str | None) -> int:
    if text is None:
        text = input("Consent line (empty to turn it off): ").strip()
    set_config_value(config_dir() / "config.toml", "consent_line", json.dumps(text, ensure_ascii=False))
    print(f"{OK} consent line {'set' if text else 'turned off'}; restart to apply: "
          "systemctl --user restart wirenote")
    return 0


# calendar ----------------------------------------------------------------------

def calendar_list() -> int:
    from ..calendar import ics

    s = _settings()
    sources = calendar_sources(s.calendar_urls)
    if not sources:
        print("no calendar yet; add one with: wirenote calendar add")
        return 1
    now = dt.datetime.now().astimezone()
    events, errors = ics.load(sources, now)
    for err in errors:
        print(f"{BAD} {err}")
    window = set(id(e) for e in ics.in_window(events, now, s.calendar_before_s,
                                               s.calendar_after_start_s))
    upcoming = sorted((e for e in events if e.end > now), key=lambda e: e.start)
    print(f"{len(sources)} calendar(s), {len(upcoming)} event(s) in the next 24 h:")
    for e in upcoming:
        mark = "→" if id(e) in window else " "
        call = "  [call link]" if e.service_links(None) else ""
        print(f" {mark} {e.start.astimezone():%a %H:%M}-{e.end.astimezone():%H:%M}  {e.title}{call}")
    if window:
        print("→ in the prompt window now")
    return 1 if errors and not events else 0


def calendar_add() -> int:
    from ..calendar import ics

    print("Paste the calendar's ICS link (Outlook: Settings > Calendar > Shared calendars >")
    print("Publish a calendar > ICS; Google: Settings > your calendar > Secret address in iCal).")
    print("It is stored in secrets.toml, not in config.toml or the logs.\n")
    url = getpass.getpass("ICS link (input hidden): ").strip()
    if not url:
        print("nothing added")
        return 1
    now = dt.datetime.now().astimezone()
    try:
        events = ics.parse(ics.fetch(url), now)
    except Exception as e:  # noqa: BLE001
        print(f"{BAD} could not read that calendar: {ics.describe_error(url, e)}")
        return 1
    secrets = load_secrets()
    urls = list(dict.fromkeys([*secrets.get("calendar_urls", []), url]))
    secrets["calendar_urls"] = urls
    save_secrets(secrets)
    _poke_daemon("refresh-calendar")
    print(f"{OK} calendar added ({len(events)} timed events in the next 24 h); "
          "the daemon picks it up now")
    return 0


def calendar_clear() -> int:
    secrets = load_secrets()
    n = len(secrets.pop("calendar_urls", []))
    save_secrets(secrets)
    _poke_daemon("refresh-calendar")
    print(f"{OK} removed {n} calendar link(s) from {secrets_path()}")
    return 0


# doctor ------------------------------------------------------------------------

def _check_ollama(s: Settings) -> tuple[bool, str]:
    from ..process.summarize import pin_local

    url = s.ollama_url.rstrip("/")
    local = pin_local(url)
    if local:
        url, ip, port = local
        owner = listener_uid(port, ip)
        if owner is not None and not trusted_owner(owner):
            return False, (f"port {port} belongs to another user (uid {owner}), so Wirenote "
                           "will not send transcripts there; start your own Ollama or set "
                           "ollama_url")
    try:
        with urllib.request.urlopen(f"{url}/api/tags", timeout=5) as r:
            models = [m["name"] for m in json.loads(r.read()).get("models", [])]
    except OSError as e:
        return False, (f"Ollama not reachable at {s.ollama_url} ({e}); "
                       "start it: systemctl --user start ollama")
    want = s.ollama_model if ":" in s.ollama_model else f"{s.ollama_model}:latest"
    if want not in models:
        return False, f"Ollama runs, but {s.ollama_model} is not pulled: ollama pull {s.ollama_model}"
    return True, f"Ollama runs with {s.ollama_model}"


@dataclass(frozen=True)
class Check:
    status: str              # "ok" | "warn" | "bad"
    message: str
    fix: str | None = None


def _ok(cond: bool, ok_msg: str, bad_msg: str, fix: str | None = None,
        warn: bool = False) -> Check:
    if cond:
        return Check("ok", ok_msg)
    return Check("warn" if warn else "bad", bad_msg, fix)


def _daemon_checks() -> list[Check]:
    try:
        status = json.loads((state_dir() / "status.json").read_text())
    except (OSError, ValueError):
        return [Check("bad", "the background service has never run",
                      "wirenote setup install")]
    alive = bool(status.get("running") and Path(f"/proc/{status.get('pid')}").exists())
    out = [_ok(alive, "background service running", "background service not running",
               "systemctl --user start wirenote")]
    out.append(_ok(bool(status.get("notifications")), "desktop notifications work",
                   "desktop notifications unavailable"))
    if status.get("notifications") and status.get("notification_buttons") is False:
        out.append(Check("warn", "notifications here have no buttons: record from the Wirenote "
                                 "window or with `wirenote record`"))
    failed = (status.get("jobs") or {}).get("failed", 0)
    out.append(_ok(not failed, "no failed note jobs", f"{failed} failed note job(s)",
                   "wirenote jobs", warn=True))
    return out


def _pipewire_checks() -> list[Check]:
    try:
        dump = json.loads(subprocess.run(["pw-dump"], capture_output=True, timeout=10,
                                         check=True).stdout)
        from ..detector.graph import Graph
        g = Graph()
        g.apply(dump)
        out = [Check("ok", f"PipeWire: mic {g.default_node('default.audio.source')}, "
                           f"output {g.default_node('default.audio.sink')}")]
    except (OSError, subprocess.SubprocessError, ValueError) as e:
        out = [Check("bad", f"PipeWire not readable: {e}")]
    out.append(_ok(shutil.which("pw-record") is not None, "pw-record installed",
                   "pw-record missing", "install your distribution's pipewire tools"))
    out.append(_ok(shutil.which("ffmpeg") is not None, "ffmpeg installed",
                   "ffmpeg missing: the web app cannot play recordings",
                   "install ffmpeg", warn=True))
    return out


def _model_checks(s: Settings) -> list[Check]:
    from ..config.vocabulary import load_vocabulary
    from ..process.engines import ENGINES, PARAKEET_FILES, PARAKEET_REPO, WHISPERCPP_REPO, choose
    from ..process.models import EMB_REPO, SEG_FILE, SEG_REPO, hub_file, whisper_path

    out = []
    engine = choose(s, load_vocabulary()) if s.asr_engine in ENGINES else s.asr_engine
    how = " (automatic)" if s.asr_engine == "auto" else ""
    if engine not in ENGINES:
        out.append(Check("bad", f"asr_engine = {s.asr_engine!r} is not one of {', '.join(ENGINES)}",
                         "fix it in config.toml"))
    elif engine == "parakeet":
        try:
            for name in PARAKEET_FILES:
                hub_file(PARAKEET_REPO, name, local_only=True)
            out.append(Check("ok", f"speech engine Parakeet{how}; its model is downloaded"))
        except Exception:  # noqa: BLE001
            out.append(Check("warn", f"speech engine Parakeet{how}; its model downloads on the "
                                     "first note (about 670 MB)"))
    elif engine == "whisper.cpp":
        exe = shutil.which(s.whispercpp_bin)
        out.append(_ok(bool(exe), f"speech engine whisper.cpp: {exe}",
                       f"speech engine whisper.cpp, but {s.whispercpp_bin} is not installed",
                       "see docs/usage.md › Speech engines, or set asr_engine = \"whisper\""))
        if not Path(s.whispercpp_model).expanduser().is_file():
            try:
                hub_file(WHISPERCPP_REPO, s.whispercpp_model, local_only=True)
                out.append(Check("ok", f"whisper.cpp model {s.whispercpp_model} is downloaded"))
            except Exception:  # noqa: BLE001
                out.append(Check("warn", f"whisper.cpp model {s.whispercpp_model} downloads on "
                                         "the first note"))
    else:
        try:
            whisper_path(s.whisper_model, local_only=True)
            out.append(Check("ok", f"speech engine Whisper{how}: {s.whisper_model} is downloaded"))
        except Exception:  # noqa: BLE001
            out.append(Check("warn", f"speech engine Whisper{how}: {s.whisper_model} downloads on "
                                     "the first note (about 1.6 GB for large-v3-turbo)"))
    if s.diarize:
        try:
            hub_file(SEG_REPO, SEG_FILE, local_only=True)
            hub_file(EMB_REPO, s.diarize_model, local_only=True)
            out.append(Check("ok", "speaker separation models are downloaded"))
        except Exception:  # noqa: BLE001
            out.append(Check("warn", "speaker separation models download on the first note "
                                     "(about 32 MB)"))
    return out


def _summary_checks(s: Settings) -> list[Check]:
    if s.summary_backend == "ollama":
        ok, msg = _check_ollama(s)
        return [Check("ok" if ok else "bad", msg)]
    if s.summary_backend == "anthropic":
        has_key = bool(load_secrets().get("anthropic_api_key") or os.environ.get("ANTHROPIC_API_KEY"))
        return [_ok(has_key, f"Claude API ({s.anthropic_model}): key set; transcripts are "
                             "sent to Anthropic",
                    f"Claude API ({s.anthropic_model}): no key saved; this works only if you "
                    "are logged in with `ant auth login`", "wirenote setup claude", warn=True)]
    if s.summary_backend == "openai":
        from ..process.summarize import pin_local

        has_key = bool(load_secrets().get("openai_api_key"))
        where = f"{s.openai_model} at {s.openai_url}"
        if pin_local(s.openai_url):
            return [Check("ok", f"{where}: on this computer; nothing leaves it")]
        return [_ok(has_key and s.openai_url.startswith("https://"),
                    f"{where}: key set; transcripts are sent there",
                    f"{where}: " + ("no key" if has_key is False else "needs https://"),
                    "wirenote setup openai")]
    return [Check("warn", "summaries are off (summary_backend = \"none\")")]


def _calendar_checks(s: Settings) -> list[Check]:
    sources = calendar_sources(s.calendar_urls)
    if not sources:
        return [Check("warn", "no calendar (optional): titles and invitees come from it",
                      "wirenote calendar add")]
    from ..calendar import ics
    events, errors = ics.load(sources, dt.datetime.now().astimezone())
    return [_ok(not errors, f"{len(sources)} calendar(s), {len(events)} events around now",
                f"{len(errors)} of {len(sources)} calendar(s) failed: {errors[0] if errors else ''}")]


def _desktop_checks(s: Settings) -> list[Check]:
    from ..notify.clipboard import available_tools

    desktop = os.environ.get("XDG_CURRENT_DESKTOP") or "unknown"
    out = [Check("ok", f"desktop: {desktop}")]
    if s.consent_line and "KDE" not in desktop.upper() and not available_tools():
        out.append(Check("warn", "no clipboard tool for the consent line",
                         "install wl-clipboard (Wayland) or xclip (X11)"))
    return out


def _file_checks(s: Settings) -> list[Check]:
    from ..calendar.ics import SCHEME
    from ..config.perms import loose_files, private_paths

    out = [Check("ok", "consent line set") if s.consent_line else
           Check("warn", "no consent line (optional)", 'wirenote setup consent "…"')]
    rec_root = s.recordings_path()
    rec_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    free_gb = shutil.disk_usage(rec_root).free / 1e9
    out.append(_ok(free_gb > 5, f"{free_gb:.0f} GB free for recordings (about 0.23 GB per hour)",
                   f"only {free_gb:.1f} GB free for recordings"))
    notes = s.notes_path()
    notes.mkdir(mode=0o700, parents=True, exist_ok=True)
    out.append(_ok(os.access(notes, os.W_OK), f"notes go to {notes}", f"cannot write to {notes}"))
    loose = loose_files(*private_paths(s))
    out.append(_ok(not loose, "recordings, notes and settings are private to you",
                   f"{len(loose)} file(s) or folder(s) readable by other users",
                   "wirenote doctor --fix"))
    if any(SCHEME.match(str(u)) for u in s.calendar_urls):
        out.append(Check("warn", "calendar link in config.toml: links are secret, keep them "
                                 "in secrets.toml",
                         "wirenote calendar add, then remove it from config.toml"))
    return out


def _update_checks(s: Settings) -> list[Check]:
    """From the last check's cache: doctor itself never goes online."""
    from .. import __version__, updates

    if not s.update_check:
        return [Check("ok", f"Wirenote {__version__}; update check off (check now: "
                            "wirenote update --check)")]
    avail = updates.available(state_dir())
    if avail:
        return [Check("warn", f"Wirenote {avail['version']} is available (you have {__version__})",
                      "wirenote update")]
    last = updates.last_check(state_dir())
    if last.get("error"):
        return [Check("warn", f"the last update check failed: {last['error']}")]
    if not last.get("checked"):
        return [Check("ok", f"Wirenote {__version__}; the first update check runs soon")]
    when = dt.datetime.fromtimestamp(last["checked"]).astimezone()
    return [Check("ok", f"Wirenote {__version__} is the newest version (checked {when:%d %b %H:%M})")]


def checks(s: Settings) -> list[Check]:
    return [*_daemon_checks(), *_pipewire_checks(), *_model_checks(s), *_summary_checks(s),
            *_calendar_checks(s), *_desktop_checks(s), *_file_checks(s), *_update_checks(s)]


def doctor(fix: bool = False) -> int:
    s = _settings()
    if fix:
        from ..config.perms import fix_modes, private_paths
        print(f"{OK} made {fix_modes(*private_paths(s))} file(s) and folder(s) private")
    found = checks(s)
    for c in found:
        mark = {"ok": OK, "warn": WARN, "bad": BAD}[c.status]
        print(f"{mark} {c.message}" + (f": {c.fix}" if c.fix else ""))
    problems = sum(c.status == "bad" for c in found)
    print("\nall good" if not problems else f"\n{problems} problem(s)")
    return 1 if problems else 0
