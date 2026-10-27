# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hrishikesh Jadhav
"""wirenote command line: run | status | stop | record | flag | toggle | jobs | reprocess | templates | setup |
calendar | doctor | ui | open | update | changelog."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import sys
from pathlib import Path

from .config.apps import AppsError, load_apps
from .config.settings import config_dir, load_settings, state_dir


def _settings():
    try:
        return load_settings(config_dir() / "config.toml")
    except ValueError as e:
        raise SystemExit(f"wirenote: {e}")


def cmd_run(_args) -> int:
    from .detector.daemon import Daemon

    try:
        settings = load_settings(config_dir() / "config.toml")
        rules = load_apps(config_dir() / "apps.toml")
    except (AppsError, ValueError) as e:
        print(f"wirenote: {e}", file=sys.stderr)
        return 2
    asyncio.run(Daemon(settings, rules, state_dir()).run())
    return 0


def cmd_status(_args) -> int:
    path = state_dir() / "status.json"
    try:
        status = json.loads(path.read_text())
    except (OSError, ValueError):
        print(f"no status yet ({path}); is `wirenote run` running?")
        return 1
    print(f"updated {status['updated']}  pid {status['pid']}  "
          f"{'running' if status.get('running') else 'stopped'}"
          f"{'' if status.get('notifications') else '  NO NOTIFICATIONS'}")
    if status.get("recording"):
        print(f"recording: {status['recording']}  ({status.get('recording_id')})")
    jobs = status.get("jobs") or {}
    if any(jobs.get(s) for s in ("queued", "transcribing", "summarizing", "failed")):
        print("jobs: " + "  ".join(f"{k}={v}" for k, v in sorted(jobs.items())))
    apps = status.get("apps") or {}
    if not apps:
        print("no app on the mic")
    for key, a in apps.items():
        extra = "  ".join(f"{k}={v}" for k, v in a.items() if k not in ("state", "name"))
        print(f"  {a['name']:<12} {a['state']:<13} {extra}")
    return 0


def _control(command: str) -> int:
    from .detector.daemon import control_socket_path

    path = control_socket_path()
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(5)
            s.connect(str(path))
            s.sendall(f"{command}\n".encode())
            reply = json.loads(s.makefile().readline())
    except (OSError, ValueError) as e:
        print(f"wirenote: cannot reach the daemon at {path} ({e}); is it running?")
        return 1
    if reply.get("ok"):
        from .process.merge import stamp

        at = f" at {stamp(reply['t'])}" if "t" in reply else ""
        print(f"{command}: {reply.get('app')}{at}")
        return 0
    print(f"{command}: {reply.get('error')}")
    return 1


def cmd_stop(_args) -> int:
    return _control("stop")


def cmd_flag(_args) -> int:
    return _control("flag")


def cmd_toggle(_args) -> int:
    return _control("toggle")


def cmd_record(args) -> int:
    return _control("record-now" if args.now else "record")


def cmd_jobs(args) -> int:
    from .process.jobs import Jobs

    jobs = Jobs(state_dir() / "jobs.db")
    rows = jobs.recent(args.n)
    if not rows:
        print("no jobs yet")
    for j in rows:
        print(f"{j.id:>4}  {j.state:<12} {j.recording.name:<34} {j.updated}")
        if j.note:
            print(f"      note: {j.note}")
        if j.error and j.state != "done":
            print(f"      error (attempt {j.attempts}): {j.error.strip().splitlines()[-1]}")
    return 0


def cmd_import(args) -> int:
    import datetime as dt

    from .capture.importer import ImportFailed, import_audio
    from .process.jobs import Jobs
    from .tools.setup import _poke_daemon

    started = None
    if args.date:
        try:
            started = dt.datetime.fromisoformat(args.date)
        except ValueError:
            print(f"wirenote: --date: expected e.g. 2026-10-27 14:00, not {args.date!r}")
            return 2
    try:
        rec = import_audio(Path(args.file).expanduser(), _settings().recordings_path(),
                           title=args.title, started=started)
    except ImportFailed as e:
        print(f"wirenote: {e}")
        return 1
    job_id = Jobs(state_dir() / "jobs.db").add(rec)
    _poke_daemon("kick-jobs")
    print(f"imported as {rec.name}; queued job {job_id} (`wirenote jobs`, or `wirenote open {rec.name}`)")
    return 0


def cmd_export(args) -> int:
    from .ui.data import NotFound, Store
    from .ui.export import docx_bytes, export_name

    try:
        call = Store(_settings(), state_dir()).call(args.recording)
    except NotFound:
        print(f"wirenote: no recording {args.recording}")
        return 1
    out = Path(args.output).expanduser() if args.output else Path(export_name(call, "docx"))
    if out.is_dir():
        out = out / export_name(call, "docx")
    out.write_bytes(docx_bytes(call))
    print(out)
    return 0


def cmd_reprocess(args) -> int:
    from .process.jobs import Jobs

    rec = _settings().recordings_path() / args.recording
    if not (rec / "manifest.json").is_file():
        print(f"wirenote: no recording {args.recording} in {rec.parent}")
        return 1
    if not args.summary and not any(rec.glob("*.wav")):
        print("wirenote: the audio was deleted; only --summary can be redone")
        return 1
    from .capture.recorder import read_manifest, write_manifest

    if args.template:
        from .process.templates import scan

        if args.template not in scan()[0]:
            print(f"wirenote: no template {args.template!r}; `wirenote templates` lists them")
            return 2
    choices = {k: getattr(args, k) for k in ("engine", "model", "language") if getattr(args, k)}
    if choices:
        from .process.engines import check_overrides

        if args.summary:
            print("wirenote: --engine, --model and --language need a new transcript, not --summary")
            return 2
        try:
            choices = check_overrides(choices)
        except ValueError as e:
            print(f"wirenote: {e}")
            return 2
    # Everything is valid: only now change the recording.
    if args.template or choices:
        m = read_manifest(rec)
        if args.template:
            m["template"] = args.template
        if choices:
            m["transcribe"] = {**(m.get("transcribe") or {}), **choices}
        write_manifest(rec, m)
    job_id = Jobs(state_dir() / "jobs.db").add(rec, summary_only=args.summary)
    print(f"queued job {job_id}; the daemon picks it up within 30 s (`wirenote jobs`)")
    return 0


def cmd_templates(_args) -> int:
    from .process.templates import BUILTIN, scan, templates_dir

    found, skipped = scan()
    print(f"{'id':<14} {'name':<16} {'source':<9} match words")
    for t in found.values():
        source = "built-in" if t is BUILTIN.get(t.id) else "file"
        match = ", ".join([*t.titles, *(f"app:{a}" for a in t.apps)]) or "-"
        print(f"{t.id:<14} {t.name:<16} {source:<9} {match}")
    for name, why in skipped:
        print(f"skipped {name}: {why}")
    print(f"\nYour own: put a .toml file in {templates_dir()} (docs/usage.md › Summary templates).")
    return 0


def cmd_live(args) -> int:
    """Live transcript of one recording; the daemon starts and stops it (process/live.py)."""
    from .config.vocabulary import load_vocabulary
    from .process.live import run

    return run(Path(args.recording_dir), _settings(), load_vocabulary())


def cmd_process(args) -> int:
    """Run one job in this process. The daemon starts this; it exits 0 once the
    job's outcome is recorded, even a failed attempt."""
    from .process.jobs import Jobs
    from .process.worker import run_job

    job = run_job(args.job, _settings(), Jobs(state_dir() / "jobs.db"))
    print(f"job {job.id}: {job.state}", file=sys.stderr)
    return 0


def cmd_setup(args) -> int:
    from .tools import install, setup

    if args.what == "install":
        return install.install(dry_run=args.dry_run)
    if args.what == "uninstall":
        return install.uninstall(purge=args.purge, dry_run=args.dry_run)
    if args.what == "claude":
        return setup.setup_claude()
    if args.what == "openai":
        return setup.setup_openai()
    if args.what == "ollama":
        return setup.setup_ollama()
    return setup.setup_consent(args.text)


def cmd_calendar(args) -> int:
    from .tools import setup

    return {"list": setup.calendar_list, "add": setup.calendar_add,
            "clear": setup.calendar_clear}[args.action]()


def cmd_ui(args) -> int:
    from .ui.server import serve

    s = _settings()
    serve(s, state_dir(), args.port or s.ui_port, open_browser=args.open)
    return 0


def cmd_open(args) -> int:
    """Open the web UI through a one-time login link, starting it if needed."""
    import subprocess
    import time

    from .ui import launch

    port = _settings().ui_port
    if launch.listener_uid(port) is None:
        subprocess.Popen([sys.executable, "-m", "wirenote.cli", "ui"], start_new_session=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
        for _ in range(50):
            if launch.listener_uid(port) is not None:
                break
            time.sleep(0.1)
    if launch.listener_uid(port) != os.getuid():
        print(f"wirenote: port {port} belongs to another user or nothing answers there; "
              "not opening it. Set ui_port in config.toml to a free port.", file=sys.stderr)
        return 1
    launch.open_ui(port, f"#/call/{args.call}" if args.call else "#/")
    return 0


def cmd_doctor(args) -> int:
    from .tools import setup

    return setup.doctor(fix=args.fix)


def cmd_update(args) -> int:
    from . import updates

    if args.check:
        found = updates.check(state_dir())
        if found["error"]:
            print(f"wirenote: cannot reach PyPI ({found['error']})")
            return 1
        avail = updates.available(state_dir())
        if not avail:
            print(f"Wirenote {found['current']} is the newest version.")
            return 0
        print(f"Wirenote {avail['version']} is available (you have {found['current']}). "
              "Install it with: wirenote update\n")
        rels = [updates.Release.from_dict(r) for r in avail["notes"]]
        print(updates.render_text(rels) if rels else f"Release notes: {avail['url']}")
        return 0
    return updates.update(state_dir(), to=args.to, wait=args.wait, yes=args.yes)


def cmd_changelog(args) -> int:
    from . import __version__, updates

    rels = updates.releases()
    if not args.all:
        rels = updates.between(rels, args.since, __version__)
    if not rels:
        print(f"no release notes for {__version__}" + (f" since {args.since}" if args.since else ""))
        return 1
    print(updates.render_text(rels))
    return 0


def cmd_dump_streams(args) -> int:
    from .tools.dump_streams import main as dump_main
    return dump_main(args.rest)


def main(argv: list[str] | None = None) -> int:
    if not sys.platform.startswith("linux"):
        print("wirenote: runs on Linux only; it needs PipeWire, systemd and D-Bus.",
              file=sys.stderr)
        return 2
    os.umask(0o077)   # recordings, transcripts and notes are private
    from .config.migrate import MigrationError, migrate_if_needed
    try:
        moved = migrate_if_needed()
    except MigrationError as e:
        print(f"wirenote: {e}", file=sys.stderr)
        return 2
    if moved:
        print(moved, file=sys.stderr)
    from . import __version__

    ap = argparse.ArgumentParser(prog="wirenote", description=__doc__)
    ap.add_argument("--version", action="version", version=f"wirenote {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run", help="run the daemon").set_defaults(fn=cmd_run)
    sub.add_parser("status", help="show what the daemon sees").set_defaults(fn=cmd_status)
    sub.add_parser("stop", help="stop the current recording").set_defaults(fn=cmd_stop)
    sub.add_parser("flag", help="flag this moment of the recording as important").set_defaults(fn=cmd_flag)
    sub.add_parser("toggle", help="stop the recording, or start one (the open prompt, else a "
                   "meeting without a call app)").set_defaults(fn=cmd_toggle)
    rec = sub.add_parser("record", help="answer the open prompt with Record")
    rec.add_argument("--now", action="store_true",
                     help="record a meeting without a call app (in person, phone on speaker)")
    rec.set_defaults(fn=cmd_record)
    jobs = sub.add_parser("jobs", help="list recent processing jobs")
    jobs.add_argument("-n", type=int, default=10)
    jobs.set_defaults(fn=cmd_jobs)
    imp = sub.add_parser("import", help="make a note from an audio or video file")
    imp.add_argument("file")
    imp.add_argument("--title", help="the call's title; otherwise the summary's")
    imp.add_argument("--date", help="when it started, e.g. 2026-10-27 14:00; default: the file's time")
    imp.set_defaults(fn=cmd_import)
    exp = sub.add_parser("export", help="save a call's notes and transcript as a Word file")
    exp.add_argument("recording", help="recording id, e.g. 20261027-100000-chrome")
    exp.add_argument("-o", "--output", help="file or folder; default: here, named after the call")
    exp.set_defaults(fn=cmd_export)
    rep = sub.add_parser("reprocess", help="queue a recording again")
    rep.add_argument("recording", help="recording id, e.g. 20261027-100000-chrome")
    rep.add_argument("--summary", action="store_true",
                     help="keep the transcript, redo only the summary and the note")
    rep.add_argument("--template", metavar="ID",
                     help="summary template for this call (`wirenote templates` lists them)")
    rep.add_argument("--engine", choices=["auto", "whisper", "parakeet", "whisper.cpp"],
                     help="speech engine for this call from now on")
    rep.add_argument("--model", help="Whisper model for this call, e.g. large-v3, medium")
    rep.add_argument("--language", help="language of this call, e.g. de, en, or auto")
    rep.set_defaults(fn=cmd_reprocess)
    sub.add_parser("templates", help="list the summary templates").set_defaults(fn=cmd_templates)
    st = sub.add_parser("setup", help="install the services, choose the summary backend, "
                                      "or set the consent line")
    st.add_argument("what", choices=["install", "uninstall", "ollama", "claude", "openai", "consent"])
    st.add_argument("text", nargs="?", help="consent line (for `setup consent`)")
    st.add_argument("--dry-run", action="store_true",
                    help="install/uninstall: show the files and commands, change nothing")
    st.add_argument("--purge", action="store_true",
                    help="uninstall: also delete recordings, transcripts and settings")
    st.set_defaults(fn=cmd_setup)
    cal = sub.add_parser("calendar", help="list upcoming events, add or remove ICS links")
    cal.add_argument("action", nargs="?", default="list", choices=["list", "add", "clear"])
    cal.set_defaults(fn=cmd_calendar)
    doc = sub.add_parser("doctor", help="check that everything is set up")
    doc.add_argument("--fix", action="store_true",
                     help="make recordings, notes and settings private to you (chmod)")
    doc.set_defaults(fn=cmd_doctor)
    ui = sub.add_parser("ui", help="serve the meetings web app on 127.0.0.1")
    ui.add_argument("--open", action="store_true", help="open it in the browser")
    ui.add_argument("--port", type=int, default=0)
    ui.set_defaults(fn=cmd_ui)
    op = sub.add_parser("open", help="open the meetings web app in the browser")
    op.add_argument("call", nargs="?", help="recording id to open")
    op.set_defaults(fn=cmd_open)
    upd = sub.add_parser("update", help="install the newest version and restart the services")
    upd.add_argument("--check", action="store_true", help="only say whether a newer version exists")
    upd.add_argument("--to", metavar="VERSION", help="install this version instead, e.g. to go back")
    upd.add_argument("--wait", action="store_true",
                     help="if a call is recording, update when it ends instead of stopping")
    upd.add_argument("--yes", action="store_true", help="do not ask before going back to an older version")
    upd.set_defaults(fn=cmd_update)
    chg = sub.add_parser("changelog", help="what changed in this version")
    chg.add_argument("--since", metavar="VERSION", help="every release after this one")
    chg.add_argument("--all", action="store_true", help="every release")
    chg.set_defaults(fn=cmd_changelog)
    live = sub.add_parser("live", help=argparse.SUPPRESS)
    live.add_argument("recording_dir")
    live.set_defaults(fn=cmd_live)
    proc = sub.add_parser("process", help=argparse.SUPPRESS)
    proc.add_argument("job", type=int)
    proc.set_defaults(fn=cmd_process)
    dump = sub.add_parser("dump-streams", help="probe PipeWire capture streams (test fixtures)",
                          add_help=False)
    dump.add_argument("rest", nargs=argparse.REMAINDER)
    dump.set_defaults(fn=cmd_dump_streams)
    args = ap.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
