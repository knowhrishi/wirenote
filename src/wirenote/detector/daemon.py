# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hrishikesh Jadhav
"""asyncio shell around Detector: pw-dump child, 1 s tick, notifications, logs,
the recorder, the processing worker, the calendar and the control socket.

Clicking Record starts a two-track recording (capture/recorder.py). When the
recording stops, a job is queued and a worker process turns it into a note
(process/worker.py). See docs/dev/m2-m4-design.md.
"""

from __future__ import annotations

import asyncio
import ctypes
import dataclasses
import datetime as dt
import fcntl
import hashlib
import json
import os
import signal
import sys
import time
import traceback
from pathlib import Path

from ..calendar import ics
from ..capture.recorder import (Recording, RecordingMeta, read_manifest, recover,
                                spawn_pw_record, write_manifest)
from ..config.apps import AppRules
from ..config.secrets import calendar_sources
from ..config.settings import Settings, config_dir, load_settings
from ..notify import clipboard
from ..notify.dbus import CLOSED_BY_CALL, Notifier, watch_sleep
from ..process.jobs import ACTIVE, Job, Jobs
from .core import Detector, DetectorEvent, Identified
from .graph import BatchFramer
from .identity import LiveProc
from .tracker import (MANUAL_KEY, Note, ShowNudge, ShowPrompt, StartRecording, State,
                      StopRecording, Transition, WithdrawPrompt)

PW_DUMP = ("pw-dump", "--monitor", "--no-colors")
STATUS_EVERY_S = 5.0
HOUSEKEEPING_EVERY_S = 6 * 3600.0
JOB_RETRY_DELAY_S = 60.0


def now_iso() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="milliseconds")


def room_hash(room: str | None) -> str | None:
    """Enough to group a call's events in the log without writing its name."""
    return hashlib.sha256(room.encode()).hexdigest()[:8] if room else None


def control_socket_path() -> Path:
    if os.environ.get("WIRENOTE_SOCKET"):       # a second instance, e.g. for tests
        return Path(os.environ["WIRENOTE_SOCKET"])
    return Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")) / "wirenote.sock"


def _worker_setup() -> None:
    """Worker child: low priority, and it dies with the daemon so a restart never
    runs two workers on one job."""
    os.nice(10)
    try:
        ctypes.CDLL("libc.so.6", use_errno=True).prctl(1, signal.SIGTERM)  # PR_SET_PDEATHSIG
    except OSError:
        pass


class EventLog:
    """detector.jsonl: one JSON object per event. A short line goes to stderr (journal)."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._f = path.open("a", buffering=1)

    def write(self, event: str, **data) -> None:
        rec = {"t": now_iso(), "event": event, **data}
        self._f.write(json.dumps(rec, default=str) + "\n")
        short = " ".join(f"{k}={v}" for k, v in data.items() if k not in ("evidence", "traceback"))
        print(f"{event} {short}", file=sys.stderr, flush=True)


class PersistentState:
    """state.json: dismiss counts and apps the user chose to always ignore."""

    def __init__(self, path: Path) -> None:
        self.path = path
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            data = {}
        self.dismisses: dict[str, int] = data.get("dismisses", {})
        self.ignored: set[str] = set(data.get("ignored", []))

    def save(self, dismisses: dict[str, int], ignored: set[str]) -> None:
        self.dismisses, self.ignored = dict(dismisses), set(ignored)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"dismisses": self.dismisses,
                                   "ignored": sorted(self.ignored)}, indent=2))
        tmp.replace(self.path)


@dataclasses.dataclass
class Shown:
    """A notification we put up: what it is for, and whether it was answered."""
    kind: str        # "prompt", "recording", "nudge" or "note"
    key: str
    handled: bool = False
    path: str | None = None


class Daemon:
    def __init__(self, settings: Settings, rules: AppRules, state_dir: Path) -> None:
        self.settings = settings
        self.state_dir = state_dir
        state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.log = EventLog(state_dir / "detector.jsonl")
        self.state = PersistentState(state_dir / "state.json")
        self.detector = Detector(settings, rules, LiveProc(), own_pids={os.getpid()},
                                 ignored=self.state.ignored, dismisses=self.state.dismisses)
        self.detector.calendar_hint = self._calendar_hint
        self.notifier = Notifier()
        self.notify_ok = False
        self.jobs = Jobs(state_dir / "jobs.db")
        self.recording: Recording | None = None
        self.spawn = spawn_pw_record                # tests swap in fake pw-record children
        self.calendar_events: list[ics.Event] = []
        self._prompt_events: dict[str, ics.Event] = {}   # app key -> event shown in its prompt
        self._shown: dict[int, Shown] = {}          # notification id -> purpose
        self._prompt_ids: dict[str, int] = {}       # app key -> prompt notification id
        self._recording_id: int | None = None
        self._ops: asyncio.Queue = asyncio.Queue()  # notification calls, run in order
        self._rec_ops: asyncio.Queue = asyncio.Queue()  # recorder calls, run in order
        self._capture_tick_queued = False
        self._worker: asyncio.subprocess.Process | None = None
        self._worker_kick = asyncio.Event()
        self._calendar_kick = asyncio.Event()
        self._last_status = 0.0
        self._stopping = asyncio.Event()

    # Lifecycle -------------------------------------------------------------

    async def run(self) -> None:
        lock = self._acquire_lock()
        self.log.write("start", pid=os.getpid(),
                       rules=[r.key for r in self.detector.rules.rules],
                       ignored=sorted(self.detector.ignored),
                       summary_backend=self.settings.summary_backend,
                       calendars=len(calendar_sources(self.settings.calendar_urls)))
        try:
            self.notifier.on_action = self._on_action
            self.notifier.on_closed = self._on_closed
            await self.notifier.connect()
            self.notify_ok = True
            self.log.write("notifications", server=self.notifier.server,
                           actions=self.notifier.has_actions)
            if not self.notifier.has_actions:
                self.log.write("warning", message="notification server has no action buttons; "
                                                  "use `wirenote record` / `wirenote stop`")
        except Exception as e:  # noqa: BLE001 - keep detecting without notifications
            self.log.write("error", message=f"notifications unavailable: {e!r}")
        try:
            await watch_sleep(self._on_sleep)
        except Exception as e:  # noqa: BLE001
            self.log.write("warning", message=f"sleep/wake signals unavailable: {e!r}")
        self._recover()

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self._stopping.set)
        self._write_status(force=True)   # `status` works before anything uses the mic

        loops = [self._pw_loop(), self._tick_loop(), self._op_loop(), self._rec_loop(),
                 self._worker_loop(), self._housekeeping_loop(), self._control_loop(),
                 self._calendar_loop()]
        tasks = [asyncio.create_task(c) for c in loops]
        stop = asyncio.create_task(self._stopping.wait())
        done, _ = await asyncio.wait([*tasks, stop], return_when=asyncio.FIRST_COMPLETED)
        # The loops never return; one that finished crashed. Exit non-zero so
        # systemd restarts us instead of running on without detection.
        crashed = [t for t in done if t is not stop and not t.cancelled() and t.exception()]
        for t in crashed:
            exc = t.exception()
            self.log.write("error", message=f"internal error: {exc!r}",
                           traceback="".join(traceback.format_exception(exc)))
        self.log.write("stop", reason="crash" if crashed else "signal")
        for task in [*tasks, stop]:
            task.cancel()
        await asyncio.gather(*tasks, stop, return_exceptions=True)
        if self.recording is not None:
            await self._finish_recording("wirenote stopped")
        for nid in list(self._shown):
            await self._safe_close(nid)
        self._write_status(force=True, running=False)
        try:
            control_socket_path().unlink()
        except OSError:
            pass
        lock.close()
        if crashed:
            raise SystemExit(1)

    def _acquire_lock(self):
        f = (self.state_dir / "daemon.lock").open("w")
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("wirenote is already running (lock held in "
                             f"{self.state_dir / 'daemon.lock'})")
        f.write(str(os.getpid()))
        f.flush()
        return f

    def _recover(self) -> None:
        """Queue recordings a crash left open, and jobs a dead worker left mid-way."""
        for rec_dir in recover(self.settings.recordings_path()):
            job = self.jobs.add(rec_dir)
            self.log.write("recovered", recording=rec_dir.name, job=job)
        for job in self.jobs.requeue_interrupted():
            self.log.write("job_interrupted", job=job.id, state=job.state, attempts=job.attempts)

    # Inputs ----------------------------------------------------------------

    async def _pw_loop(self) -> None:
        backoff = 1.0
        while True:
            try:
                proc = await asyncio.create_subprocess_exec(
                    *PW_DUMP, stdout=asyncio.subprocess.PIPE, limit=1 << 22)
            except OSError as e:
                self.log.write("error", message=f"cannot start pw-dump: {e!r}",
                               restart_in_s=backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
                continue
            framer = BatchFramer()
            try:
                async for raw in proc.stdout:
                    batch = framer.feed(raw.decode("utf-8", "replace"))
                    if batch is not None:
                        self._handle(self.detector.apply_batch(batch, time.monotonic()))
                        backoff = 1.0
            finally:
                if proc.returncode is None:
                    proc.terminate()
                rc = await proc.wait()
            self.log.write("pw_dump_exit", returncode=rc, restart_in_s=backoff)
            self.detector.reset_graph()
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)

    async def _tick_loop(self) -> None:
        while True:
            await asyncio.sleep(self.settings.reconcile_s)
            self._handle(self.detector.tick(time.monotonic()))
            if self.recording is not None and not self._capture_tick_queued:
                self._capture_tick_queued = True
                self._rec_ops.put_nowait((self._capture_tick, None))

    def _on_sleep(self, going_to_sleep: bool) -> None:
        now = time.monotonic()
        if going_to_sleep:
            self.log.write("sleep")
            self._handle(self.detector.tracker.sleep(now))
        else:
            self.log.write("resume")
            self._handle(self.detector.tracker.resume(now, self.detector.observations()))

    def _on_action(self, nid: int, action: str) -> None:
        shown = self._shown.get(nid)
        if shown is None or shown.handled:
            return
        if shown.kind == "note" and action in ("open", "default"):
            shown.handled = True
            self._open_note(shown.key, shown.path)
            return
        shown.handled = True
        now = time.monotonic()
        self.log.write("click", notification=shown.kind, app=shown.key, action=action)
        if shown.kind == "prompt" and action == "record":
            self._handle(self.detector.tracker.record(shown.key, now))
        elif shown.kind == "prompt" and action == "ignore":
            self._handle(self.detector.tracker.ignore(shown.key, now))
        elif shown.kind == "recording" and action == "stop":
            self._handle(self.detector.tracker.stop(now))
        elif shown.kind == "nudge" and action == "always-ignore":
            self.detector.ignored.add(shown.key)
            self.log.write("always_ignore", app=shown.key)
        elif shown.kind == "nudge" and action == "keep":
            pass
        else:
            shown.handled = False   # e.g. a click on the body ("default"): wait for a button
            return
        self._save_state()

    def _on_closed(self, nid: int, reason: int) -> None:
        shown = self._shown.pop(nid, None)
        if shown is None:
            return
        if shown.kind == "prompt":
            self._prompt_ids.pop(shown.key, None)
        if shown.kind == "recording" and self._recording_id == nid:
            self._recording_id = None
        if shown.handled or reason == CLOSED_BY_CALL or shown.kind == "note":
            return
        self.log.write("notification_closed", notification=shown.kind, app=shown.key,
                       reason=reason)
        if shown.kind == "prompt":
            # docs/dev/PLAN.md › Prompt: a close without an action counts as Ignore.
            self._handle(self.detector.tracker.ignore(shown.key, time.monotonic()))
            self._save_state()

    # Outputs ---------------------------------------------------------------

    def _handle(self, events: list[DetectorEvent]) -> None:
        for e in events:
            if isinstance(e, Identified):
                if e.key is None:
                    self.log.write("unmatched", client=e.client_serial, evidence=e.evidence)
                else:
                    self.log.write("identified", app=e.key, kind=e.kind, by=e.matched_by,
                                   client=e.client_serial, evidence=e.evidence)
                if not e.evidence.get("pid_ok"):
                    self.log.write("pid_mismatch", client=e.client_serial, evidence=e.evidence)
            elif isinstance(e, Transition):
                self.log.write("transition", app=e.key, old=str(e.old), new=str(e.new),
                               reason=e.reason)
            elif isinstance(e, Note):
                self.log.write("note", app=e.key, message=e.message)
            elif isinstance(e, ShowPrompt):
                event = self._calendar_event(e.service)
                if event:
                    self._prompt_events[e.key] = event
                self.log.write("prompt", app=e.key, kind=e.kind, service=e.service,
                               room_hash=room_hash(e.room), calendar=event is not None)
                self._ops.put_nowait((self._show_prompt, (e, event)))
            elif isinstance(e, WithdrawPrompt):
                self._prompt_events.pop(e.key, None)
                self.log.write("withdraw", app=e.key)
                self._ops.put_nowait((self._withdraw, e))
            elif isinstance(e, StartRecording):
                self.log.write("record_start", app=e.key)
                self._rec_ops.put_nowait((self._start_capture, e))
            elif isinstance(e, StopRecording):
                self.log.write("record_stop", app=e.key, reason=e.reason,
                               duration_s=e.duration_s)
                self._rec_ops.put_nowait((self._stop_capture, e))
            elif isinstance(e, ShowNudge):
                self.log.write("nudge", app=e.key, dismisses=e.dismisses)
                self._ops.put_nowait((self._show_nudge, e))
        self._write_status(force=bool(events))

    async def _op_loop(self) -> None:
        while True:
            fn, arg = await self._ops.get()
            if not self.notify_ok:
                continue
            try:
                await fn(arg)
            except Exception as e:  # noqa: BLE001 - one failed call must not stop the daemon
                self.log.write("error", message=f"notification call failed: {e!r}")

    async def _show_prompt(self, arg: tuple[ShowPrompt, ics.Event | None]) -> None:
        e, event = arg
        if event:
            summary = event.title
            body = f"{e.name} is using your mic. Record this call?"
        elif e.service:
            summary = f"{e.service} call in {e.name}"
            body = e.room or ""
        else:
            summary = f"{e.name} is using your mic"
            body = "Record this call?"
        if not self.notifier.has_actions:
            # No buttons on this desktop's notifications.
            body = (body + " " if body else "") + \
                "Record from the Wirenote window or run `wirenote record`."
        nid = await self.notifier.notify(summary, body, [("record", "Record"), ("ignore", "Ignore")])
        self._shown[nid] = Shown("prompt", e.key)
        self._prompt_ids[e.key] = nid

    async def _withdraw(self, e: WithdrawPrompt) -> None:
        nid = self._prompt_ids.pop(e.key, None)
        if nid is not None:
            await self._safe_close(nid)

    async def _show_recording(self, rec: Recording) -> None:
        m = rec.meta
        what = (m.calendar or {}).get("title") or (f"{m.service} call" if m.service else m.name)
        nid = await self.notifier.notify(f"Recording {what}",
                                         "Mic and system audio. Stop ends the recording.",
                                         [("stop", "Stop")])
        self._shown[nid] = Shown("recording", m.app)
        self._recording_id = nid

    async def _show_stopped(self, arg: tuple[StopRecording, int | None]) -> None:
        e, job_id = arg
        if self._recording_id is not None:
            await self._safe_close(self._recording_id)
            self._recording_id = None
        body = f"{e.reason} after {e.duration_s / 60:.1f} min."
        if job_id is not None:
            body += " Writing the note now." if self._worker is None else " Note queued."
        await self.notifier.notify("Recording saved", body, timeout_ms=8000)

    async def _show_nudge(self, e: ShowNudge) -> None:
        nid = await self.notifier.notify(
            f"Ignored {e.name} {e.dismisses} times",
            f"Stop asking when {e.name} uses the mic?",
            [("always-ignore", f"Always ignore {e.name}"), ("keep", "Keep asking")])
        self._shown[nid] = Shown("nudge", e.key)

    async def _show_message(self, arg: tuple[str, str]) -> None:
        summary, body = arg
        await self.notifier.notify(summary, body, timeout_ms=10000, urgency=2)

    async def _show_note_ready(self, job: Job) -> None:
        title = Path(job.note).stem.split(" ", 2)[-1] if job.note else ""
        nid = await self.notifier.notify("Meeting note ready", title, [("open", "Open note")],
                                         timeout_ms=0)
        self._shown[nid] = Shown("note", job.recording.name, path=job.note)

    async def _safe_close(self, nid: int) -> None:
        self._shown.pop(nid, None)
        try:
            await self.notifier.close(nid)
        except Exception:  # noqa: BLE001 - already gone
            pass

    def _open_note(self, rec_id: str, path: str | None) -> None:
        """The call in the web UI if this user's UI runs (docs/dev/ui-design.md), else the
        Markdown file. Never a UI on the port that belongs to someone else."""
        from ..ui.launch import open_ui

        try:
            if not open_ui(self.settings.ui_port, f"#/call/{rec_id}", fallback=path):
                self.log.write("warning", message="web UI not running (or not ours); "
                                                  "opened the Markdown note")
        except OSError as e:
            self.log.write("error", message=f"cannot open note: {e!r}")

    # Recording -------------------------------------------------------------

    async def _rec_loop(self) -> None:
        while True:
            fn, arg = await self._rec_ops.get()
            try:
                await fn(arg)
            except Exception as e:  # noqa: BLE001 - keep detecting; the error is logged
                self.log.write("error", message=f"recorder call failed: {e!r}",
                               traceback=traceback.format_exc())

    def _mic_target(self, key: str) -> str | None:
        """The source the app records from; None (follow the default) if that is the default."""
        sources = self.detector.app_sources.get(key, ())
        default = self.detector.graph.default_node("default.audio.source")
        if not sources or default in sources:
            return None
        return sources[0]

    async def _start_capture(self, e: StartRecording) -> None:
        if self.recording is not None:
            self.log.write("warning", message=f"already recording {self.recording.id}")
            return
        t = self.detector.tracker.apps.get(e.key)
        obs = t.obs if t else None
        event = self._prompt_events.pop(e.key, None)
        if event is None and e.key == MANUAL_KEY:
            event = self._calendar_event(None)
        meta = RecordingMeta(app=e.key, name=e.name, service=obs.service if obs else None,
                             room=obs.room if obs else None,
                             calendar=event.as_meta() if event else None)
        rec = Recording(self.settings.recordings_path(), meta,
                        node_prefix=self.settings.own_node_prefix,
                        heartbeat_s=self.settings.heartbeat_s, spawn=self.spawn,
                        system_target=self.settings.system_target)
        target = self._mic_target(e.key)
        try:
            await rec.start(target)
        except Exception as err:  # noqa: BLE001
            self.log.write("error", message=f"recording failed to start: {err!r}")
            self._ops.put_nowait((self._show_message, ("Recording failed", str(err))))
            self._handle(self.detector.tracker.stop(time.monotonic(), "capture failed"))
            return
        self.recording = rec
        self.log.write("capture_start", recording=rec.id, mic_target=target or "default",
                       dir=str(rec.dir))
        if self._worker is not None and self._worker.returncode is None:
            self._worker.send_signal(signal.SIGSTOP)   # no transcription during a call
            self.log.write("job_paused")
        self._write_status(force=True)   # the app opens the live page from recording_id
        self._ops.put_nowait((self._show_recording, rec))
        if self.settings.consent_line:
            self._ops.put_nowait((self._copy_consent, self.settings.consent_line))

    async def _copy_consent(self, text: str) -> None:
        """Consent line to the clipboard (Klipper, wl-copy, xclip or xsel); if none
        works, the notification shows it so it can be copied by hand."""
        tool = await clipboard.copy(text, self.notifier.copy_to_clipboard)
        if tool is None:
            self.log.write("warning", message="no clipboard tool worked; showing the consent line")
            await self.notifier.notify("Copy this into the call chat:", text, timeout_ms=0)
            return
        self.log.write("consent_copied", tool=tool)
        await self.notifier.notify("Consent line copied",
                                   "Paste it into the call chat before you continue.",
                                   timeout_ms=10000)

    async def _stop_capture(self, e: StopRecording) -> None:
        if self.recording is None:
            self.log.write("note", app=e.key, message="stop: no recording was running")
            self._ops.put_nowait((self._show_stopped, (e, None)))
            return
        job_id = await self._finish_recording(e.reason)
        self._ops.put_nowait((self._show_stopped, (e, job_id)))

    async def _finish_recording(self, reason: str) -> int:
        rec, self.recording = self.recording, None
        await rec.stop(reason)
        job_id = self.jobs.add(rec.dir)
        self.log.write("capture_stop", recording=rec.id, reason=reason, job=job_id,
                       segments=len(rec.segments))
        if self._worker is not None and self._worker.returncode is None:
            self._worker.send_signal(signal.SIGCONT)
            self.log.write("job_resumed")
        self._worker_kick.set()
        self._write_status(force=True)
        return job_id

    async def _capture_tick(self, _arg) -> None:
        self._capture_tick_queued = False
        rec = self.recording
        if rec is None:
            return
        for ev in await rec.heartbeat():
            self.log.write(f"track_{ev.kind}", recording=rec.id, track=ev.track, detail=ev.detail)
            if ev.kind in ("stalled", "gave_up"):
                other = "system audio" if ev.track == "mic" else "mic"
                self._ops.put_nowait((self._show_message, (
                    f"{'Mic' if ev.track == 'mic' else 'System audio'} track stopped",
                    f"{ev.detail}. The {other} track keeps recording.")))
        if self.detector.app_sources.get(rec.meta.app):
            target = self._mic_target(rec.meta.app)
            if await rec.retarget_mic(target):
                self.log.write("mic_moved", recording=rec.id, target=target or "default")

    # Processing ------------------------------------------------------------

    async def _worker_loop(self) -> None:
        while True:
            job = self.jobs.next_queued()
            if job is None or self.recording is not None:
                self._worker_kick.clear()
                try:
                    await asyncio.wait_for(self._worker_kick.wait(), 30)
                except asyncio.TimeoutError:
                    pass
                continue
            self.log.write("job_start", job=job.id, recording=job.recording.name,
                           summary_only=job.summary_only)
            self._write_status(force=True)
            self._worker = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "wirenote.cli", "process", str(job.id),
                stdin=asyncio.subprocess.DEVNULL, preexec_fn=_worker_setup)
            rc = await self._worker.wait()
            self._worker = None
            job = self.jobs.get(job.id)
            # The worker records its own failures and exits 0; anything else crashed it.
            if job.state in ACTIVE or rc != 0:
                job = self.jobs.fail(job.id, f"worker exited with {rc}")
            self.log.write("job_end", job=job.id, state=job.state, attempts=job.attempts,
                           note=bool(job.note), error=(job.error or "")[-300:] or None)
            self._write_status(force=True)
            if job.state == "done":
                self._ops.put_nowait((self._show_note_ready, job))
            elif job.state == "failed":
                self._ops.put_nowait((self._show_message, (
                    "Meeting note failed",
                    f"{job.recording.name}: see `wirenote jobs`. Audio is kept.")))
            elif job.state == "queued":
                await asyncio.sleep(JOB_RETRY_DELAY_S)

    async def _housekeeping_loop(self) -> None:
        """docs/dev/PLAN.md › Processing: delete audio audio_retention_days after its note."""
        while True:
            try:
                self._expire_audio()
            except Exception as e:  # noqa: BLE001
                self.log.write("error", message=f"housekeeping failed: {e!r}")
            await asyncio.sleep(HOUSEKEEPING_EVERY_S)

    def _expire_audio(self) -> None:
        root = self.settings.recordings_path()
        if not root.is_dir() or self.settings.audio_retention_days <= 0:
            return
        cutoff = dt.datetime.now().astimezone() - dt.timedelta(
            days=self.settings.audio_retention_days)
        for rec_dir in root.iterdir():
            try:
                m = read_manifest(rec_dir)
            except (OSError, ValueError):
                continue
            written = m.get("note_written")
            if not written or m.get("audio_deleted") or dt.datetime.fromisoformat(written) > cutoff:
                continue
            for wav_file in rec_dir.glob("*.wav"):
                wav_file.unlink()
            m["audio_deleted"] = now_iso()
            write_manifest(rec_dir, m)
            self.log.write("audio_deleted", recording=rec_dir.name)

    # Calendar --------------------------------------------------------------

    async def _calendar_loop(self) -> None:
        """Sources are re-read every round, so `wirenote calendar add` needs no restart."""
        while True:
            sources = calendar_sources(self.settings.calendar_urls)
            if sources:
                now = dt.datetime.now().astimezone()
                events, errors = await asyncio.to_thread(ics.load, sources, now)
                for err in errors:
                    self.log.write("warning", message=err)
                if events or not errors:
                    self.calendar_events = events
                self.log.write("calendar", sources=len(sources), events=len(events),
                               errors=len(errors))
            else:
                self.calendar_events = []
            self._calendar_kick.clear()
            try:
                await asyncio.wait_for(self._calendar_kick.wait(), self.settings.calendar_refresh_s)
            except asyncio.TimeoutError:
                pass

    def _calendar_window(self) -> list[ics.Event]:
        return ics.in_window(self.calendar_events, dt.datetime.now().astimezone(),
                             self.settings.calendar_before_s, self.settings.calendar_after_start_s)

    def _calendar_hint(self) -> bool:
        return bool(self.calendar_events) and ics.has_call_link(self._calendar_window())

    def _calendar_event(self, service: str | None) -> ics.Event | None:
        return ics.pick(self._calendar_window(), service) if self.calendar_events else None

    # Control socket --------------------------------------------------------

    async def _control_loop(self) -> None:
        path = control_socket_path()
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        try:
            server = await asyncio.start_unix_server(self._on_control, path=str(path))
        except OSError as e:
            # Detection and recording work without it; only `wirenote stop/record` and
            # the app's Record/Stop buttons need the socket.
            self.log.write("error", message=f"control socket unavailable at {path}: {e!r}")
            await asyncio.Event().wait()
        os.chmod(path, 0o600)
        async with server:
            await server.serve_forever()

    async def _on_control(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            cmd = (await asyncio.wait_for(reader.readline(), 5)).decode().strip()
            reply = self._control(cmd)
            writer.write((json.dumps(reply) + "\n").encode())
            await writer.drain()
        except (asyncio.TimeoutError, ConnectionError, UnicodeDecodeError):
            pass
        finally:
            writer.close()

    def _control(self, cmd: str) -> dict:
        now = time.monotonic()
        tracker = self.detector.tracker
        if cmd == "stop":
            key = tracker.recording_key
            if key is None:
                return {"ok": False, "error": "not recording"}
            self.log.write("control", command="stop", app=key)
            self._handle(tracker.stop(now, "stopped by you"))
            return {"ok": True, "app": key}
        if cmd == "record":
            prompted = [k for k, t in tracker.apps.items() if t.state is State.PROMPTED]
            if len(prompted) != 1:
                return {"ok": False, "error": "no prompt showing" if not prompted
                        else f"several prompts: {', '.join(prompted)}"}
            key = prompted[0]
            self.log.write("control", command="record", app=key)
            nid = self._prompt_ids.pop(key, None)
            if nid is not None:
                self._shown[nid].handled = True
                self._ops.put_nowait((self._safe_close, nid))
            self._handle(tracker.record(key, now))
            return {"ok": True, "app": key}
        if cmd == "record-now":
            if tracker.recording_key is not None:
                return {"ok": False, "error": f"already recording {tracker.recording_key}"}
            self.log.write("control", command="record-now")
            self._handle(tracker.start_manual(now))
            return {"ok": True, "app": MANUAL_KEY}
        if cmd == "refresh-calendar":
            self._calendar_kick.set()
            return {"ok": True, "app": None}
        if cmd == "reload-settings":
            # The UI's Settings page changed config.toml; keep running on the old
            # settings if the file cannot be read.
            try:
                self.settings = load_settings(config_dir() / "config.toml")
            except (OSError, ValueError) as e:
                self.log.write("error", message=f"config.toml not reloaded: {type(e).__name__}")
                return {"ok": False, "error": "config.toml could not be read; kept the old settings"}
            self.log.write("control", command="reload-settings")
            return {"ok": True, "app": None}
        return {"ok": False, "error": f"unknown command {cmd!r}"}

    # State -----------------------------------------------------------------

    def _save_state(self) -> None:
        self.state.save(self.detector.tracker.dismisses, self.detector.ignored)

    def _write_status(self, force: bool = False, running: bool = True) -> None:
        now = time.monotonic()
        apps = self.detector.tracker.snapshot(now)
        if not force and not (apps and now - self._last_status >= STATUS_EVERY_S):
            return
        self._last_status = now
        status = {"updated": now_iso(), "pid": os.getpid(), "running": running,
                  "notifications": self.notify_ok,
                  "notification_buttons": self.notifier.has_actions if self.notify_ok else None,
                  "recording": self.detector.tracker.recording_key,
                  "recording_id": self.recording.id if self.recording else None,
                  "jobs": self.jobs.counts(), "apps": apps}
        path = self.state_dir / "status.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(status, indent=2))
        tmp.replace(path)
