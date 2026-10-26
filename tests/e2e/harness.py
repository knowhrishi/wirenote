# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hrishikesh Jadhav
"""A private little world for end-to-end runs of the real `wirenote` commands.

Only what lies outside Wirenote is faked: PipeWire (pw-dump, pw-record), Ollama
(an HTTP server on loopback) and, unless `models=True`, the Whisper model. The
daemon, the worker and the web app run as their own processes, exactly as
installed, with HOME and the XDG folders inside a temporary directory and no
D-Bus, so a run on a developer's desktop cannot touch their real Wirenote, show
notifications or reach the clipboard.
"""

from __future__ import annotations

import http.client
import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

HERE = Path(__file__).parent
TRACE = HERE.parent / "replay" / "jitsi-firefox.json"
# jitsi-firefox: the mic stream runs from 22.35 s; Firefox leaves the call at 190.93 s.
HOLD_AT, STOP_AT = 30.0, 200.0

SUMMARY = {"title": "Release planning",
           "summary": ["The release ships on Friday.", "Me sends the changelog first."],
           "decisions": ["Ship the release on Friday"],
           "action_items": [{"owner": "Me", "task": "Send the changelog", "due": "Thursday"}],
           "open_questions": []}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def minimal(schema: dict):
    """The smallest value that satisfies a JSON schema of the kinds Wirenote sends."""
    t = schema.get("type")
    if t == "object":
        return {k: minimal(v) for k, v in schema.get("properties", {}).items()}
    return {"array": [], "string": "", "number": 0, "integer": 0, "boolean": False}.get(t)


class FakeOllama:
    """Answers /api/chat like Ollama with a JSON format, and keeps every request."""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.requests.append(body)
                props = body.get("format", {}).get("properties", {})
                if "summary" in props:
                    answer = {k: v for k, v in SUMMARY.items() if k in props}
                else:
                    answer = minimal(body.get("format", {}))
                data = json.dumps({"model": body.get("model"), "done": True,
                                   "message": {"role": "assistant",
                                               "content": json.dumps(answer)}}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *_):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


class World:
    def __init__(self, root: Path, models: bool = False, mic_wav: Path | None = None,
                 system_wav: Path | None = None) -> None:
        self.root = root
        self.home = root / "home"
        self.hangup_file = root / "hangup"
        self.logs = root / "logs"
        self.logs.mkdir(parents=True)
        self.ollama = FakeOllama()
        self.ui_port = free_port()
        self.daemon: subprocess.Popen | None = None
        self._ui: subprocess.Popen | None = None
        self._runs = 0

        xdg = {"XDG_CONFIG_HOME": ".config", "XDG_STATE_HOME": ".local/state",
               "XDG_DATA_HOME": ".local/share", "XDG_CACHE_HOME": ".cache",
               "XDG_RUNTIME_DIR": "run"}
        for sub in xdg.values():
            (self.home / sub).mkdir(parents=True, mode=0o700)
        keep = ("PATH", "LANG", "LC_ALL", "TMPDIR", "SYSTEMROOT")
        self.env = {k: v for k, v in os.environ.items() if k in keep}
        self.env.update({k: str(self.home / v) for k, v in xdg.items()})
        bindir = self._fake_bin(mic_wav, system_wav)
        self.env.update(
            HOME=str(self.home),
            PATH=f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}",
            # No session or system bus: no notifications, no clipboard, no logind.
            DBUS_SESSION_BUS_ADDRESS="unix:path=/nonexistent/wirenote-e2e",
            DBUS_SYSTEM_BUS_ADDRESS="unix:path=/nonexistent/wirenote-e2e",
            PYTHONUNBUFFERED="1",
            WIRENOTE_E2E_TRACE=str(TRACE), WIRENOTE_E2E_HOLD_AT=str(HOLD_AT),
            WIRENOTE_E2E_STOP_AT=str(STOP_AT), WIRENOTE_E2E_HANGUP=str(self.hangup_file))
        if models:
            # CI sets HF_HOME to a cached folder; otherwise models land in this world.
            self.env["HF_HOME"] = os.environ.get("HF_HOME") or str(self.home / ".cache" / "hf")
        else:
            self.env.update(HF_HUB_OFFLINE="1", PYTHONPATH=str(HERE / "fake_models"))

        model_dir = root / "fake-whisper-model"
        model_dir.mkdir()
        config = {
            "dwell_meeting_s": 1.0, "grace_s": 1.0, "min_app_runtime_s": 1.0,
            "min_recording_age_s": 2.0,
            "recordings_dir": str(root / "recordings"), "notes_dir": str(root / "Meetings"),
            "whisper_model": "tiny.en" if models else str(model_dir),
            "whisper_language": "en", "diarize": models,
            "summary_backend": "ollama", "ollama_url": self.ollama.url,
            "ollama_model": "fake:latest", "ui_port": self.ui_port,
        }
        cfg = self.home / ".config" / "wirenote"
        cfg.mkdir(mode=0o700)
        (cfg / "config.toml").write_text("".join(f"{k} = {json.dumps(v)}\n"
                                                 for k, v in config.items()))

    def _fake_bin(self, mic_wav: Path | None, system_wav: Path | None) -> Path:
        bindir = self.root / "bin"
        bindir.mkdir()
        extra = {"WIRENOTE_E2E_MIC_WAV": mic_wav, "WIRENOTE_E2E_SYSTEM_WAV": system_wav}
        exports = "".join(f'export {k}="{v}"\n' for k, v in extra.items() if v)
        for name, script in (("pw-dump", "pw_dump.py"), ("pw-record", "pw_record.py")):
            path = bindir / name
            path.write_text(f'#!/bin/sh\nunset PYTHONPATH\n{exports}'
                            f'exec "{sys.executable}" "{HERE / "fakes" / script}" "$@"\n')
            path.chmod(0o755)
        return bindir

    # Paths ------------------------------------------------------------------

    @property
    def state(self) -> Path:
        return self.home / ".local" / "state" / "wirenote"

    @property
    def recordings(self) -> Path:
        return self.root / "recordings"

    @property
    def notes(self) -> Path:
        return self.root / "Meetings"

    def recording_dir(self) -> Path | None:
        dirs = sorted(p for p in self.recordings.glob("*") if p.is_dir())
        return dirs[-1] if dirs else None

    def wav_bytes(self, track: str) -> int:
        rec = self.recording_dir()
        files = sorted(rec.glob(f"{track}-*.wav")) if rec else []
        return files[-1].stat().st_size if files else 0

    def events(self) -> list[dict]:
        try:
            return [json.loads(line) for line in (self.state / "detector.jsonl").open()]
        except FileNotFoundError:
            return []

    def event_names(self) -> list[str]:
        return [e["event"] for e in self.events()]

    # Processes ----------------------------------------------------------------

    def cli(self, *args: str, timeout: float = 60) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, "-m", "wirenote.cli", *args], env=self.env,
                              cwd=self.root, capture_output=True, text=True, timeout=timeout)

    def start_daemon(self) -> None:
        self._runs += 1
        out = (self.logs / f"daemon-{self._runs}.log").open("w")
        self.daemon = subprocess.Popen([sys.executable, "-m", "wirenote.cli", "run"],
                                       env=self.env, cwd=self.root, stdin=subprocess.DEVNULL,
                                       stdout=out, stderr=subprocess.STDOUT)
        self.wait_for(lambda: (self.home / "run" / "wirenote.sock").exists(), 30,
                      "the daemon's control socket")

    def stop_daemon(self, sig: int = signal.SIGTERM) -> int:
        self.daemon.send_signal(sig)
        rc = self.daemon.wait(30)
        self.daemon = None
        return rc

    def hangup(self) -> None:
        self.hangup_file.touch()

    def start_ui(self) -> None:
        out = (self.logs / "ui.log").open("w")
        self._ui = subprocess.Popen([sys.executable, "-m", "wirenote.cli", "ui"], env=self.env,
                                    cwd=self.root, stdin=subprocess.DEVNULL, stdout=out,
                                    stderr=subprocess.STDOUT)

        def listening() -> bool:
            try:
                socket.create_connection(("127.0.0.1", self.ui_port), 0.2).close()
                return True
            except OSError:
                return False
        self.wait_for(listening, 30, "the web app")

    def close(self) -> None:
        for p in (self.daemon, self._ui):
            if p is not None and p.poll() is None:
                p.kill()
                p.wait(10)
        self.ollama.close()

    # Jobs -------------------------------------------------------------------

    def jobs(self) -> list[tuple[str, str | None]]:
        """(state, note path) of every job, newest first, read through `wirenote jobs`."""
        out, rows = self.cli("jobs").stdout, []
        for line in out.splitlines():
            m = re.match(r"\s*\d+\s+(\w+)\s", line)
            if m:
                rows.append([m.group(1), None])
            elif line.strip().startswith("note:") and rows:
                rows[-1][1] = line.split("note:", 1)[1].strip()
        return [tuple(r) for r in rows]

    # Waiting ------------------------------------------------------------------

    def wait_for(self, check, timeout: float, what: str) -> None:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if check():
                return
            if self.daemon is not None and self.daemon.poll() is not None:
                raise AssertionError(f"the daemon exited ({self.daemon.returncode}) while "
                                     f"waiting for {what}\n{self.diagnostics()}")
            time.sleep(0.2)
        raise AssertionError(f"timed out after {timeout:g} s waiting for {what}\n"
                             f"{self.diagnostics()}")

    def diagnostics(self) -> str:
        """What to read when a step times out in CI."""
        parts = [f"events: {self.event_names()}"]
        for log in sorted(self.logs.glob("*.log")):
            parts.append(f"--- {log.name} (last 40 lines)\n"
                         + "\n".join(log.read_text(errors="replace").splitlines()[-40:]))
        parts.append(f"--- wirenote jobs\n{self.cli('jobs').stdout}")
        return "\n".join(parts)

    # Web app ------------------------------------------------------------------

    def login(self) -> "UiClient":
        from wirenote.ui.launch import new_login_code

        with mock.patch.dict(os.environ, self.env):
            code = new_login_code()
        client = UiClient(self.ui_port)
        status, headers, _ = client.request("GET", f"/login?code={code}", session=False)
        assert status == 303, status
        client.cookie = headers["Set-Cookie"].split(";", 1)[0]
        status, _, page = client.request("GET", "/")
        assert status == 200
        client.token = re.search(rb'name="wirenote-token" content="([^"]+)"', page).group(1).decode()
        return client


class UiClient:
    def __init__(self, port: int) -> None:
        self.port = port
        self.cookie = ""
        self.token = ""

    def request(self, method: str, path: str, body: dict | None = None,
                headers: dict | None = None, session: bool = True):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        hdrs = {"Host": f"127.0.0.1:{self.port}", **(headers or {})}
        if session and self.cookie:
            hdrs["Cookie"] = self.cookie
        if body is not None:
            hdrs.update({"Content-Type": "application/json", "X-Wirenote-Token": self.token,
                         "Origin": f"http://127.0.0.1:{self.port}"})
        conn.request(method, path, body=json.dumps(body) if body is not None else None,
                     headers=hdrs)
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        return resp.status, dict(resp.getheaders()), data

    def json(self, method: str, path: str, body: dict | None = None):
        status, _, data = self.request(method, path, body)
        assert status == 200, (status, data[:300])
        return json.loads(data)
