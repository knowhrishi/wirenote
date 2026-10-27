# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hrishikesh Jadhav
"""`wirenote ui`: the local web app on 127.0.0.1 (docs/dev/ui-design.md).

Stdlib only. Localhost only, Host header checked (DNS rebinding). Every
connection must come from this user (other accounts on the machine reach
127.0.0.1 too), and every request needs the session cookie, which only a
one-time login link from a launcher hands out (launch.py). Writes also need the
token embedded in the page, so another site open in the browser
cannot change or delete anything.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import mimetypes
import os
import re
import secrets
import socket
import subprocess
import sys
from http import HTTPStatus
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from .. import __version__
from ..config.settings import Settings
from ..net.peer import tcp_owner
from .data import Invalid, NotFound, Store
from .launch import redeem_login_code

STATIC = Path(__file__).parent / "static"
mimetypes.add_type("font/woff2", ".woff2")
mimetypes.add_type("text/javascript", ".js")

ROUTE_CALL = re.compile(r"^/api/calls/([^/]+)(/[a-z-]+)?(?:/(\d+))?$")
NEXT_ROUTE = re.compile(r"^#/[\w/?=.&%-]*$")
COOKIE = "wirenote_session"


def _session_key(state_dir: Path) -> str:
    """One key per install, kept across restarts so an open tab stays logged in."""
    path = state_dir / "ui.key"
    try:
        key = path.read_text().strip()
        if key:
            return key
    except FileNotFoundError:
        pass
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    key = secrets.token_urlsafe(32)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(key + "\n")
    tmp.replace(path)
    return key


class App:
    def __init__(self, settings: Settings, state_dir: Path, port: int) -> None:
        self.store = Store(settings, state_dir)
        self.session_key = _session_key(state_dir)
        # Derived from the persistent key, so an open tab's writes still work after a
        # restart; a page can only learn it from index.html, which needs the cookie.
        self.token = hmac.new(self.session_key.encode(), b"wirenote-write-token",
                              hashlib.sha256).hexdigest()[:32]
        self.port = port
        self.hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}

    def index_html(self) -> bytes:
        html = (STATIC / "index.html").read_text()
        return html.replace("{{TOKEN}}", self.token).replace("{{VERSION}}", __version__).encode()


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        server_version = "wirenote"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # quiet; errors still go to stderr
            pass

        # Plumbing ----------------------------------------------------------

        def _send(self, status: int, body: bytes, ctype: str, extra: dict | None = None):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            # Other sites must not embed or probe our responses (<audio src=…>).
            self.send_header("Cross-Origin-Resource-Policy", "same-origin")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            if self.close_connection:
                self.send_header("Connection", "close")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, data, status: int = 200):
            self._send(status, json.dumps(data, ensure_ascii=False).encode(),
                       "application/json; charset=utf-8")

        def _error(self, status: int, message: str):
            self._json({"error": message}, status)

        def _drain(self) -> None:
            """Read a refused request's body so keep-alive stays in step; a body too big
            (or a bad length) cannot be skipped, so that connection ends here."""
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = -1
            if 0 <= n <= 1 << 20:
                if n:
                    self.rfile.read(n)
            else:
                self.close_connection = True

        def _refuse(self, status: int, message: str) -> bool:
            self._drain()
            self._error(status, message)
            return False

        def _from_me(self) -> bool:
            """The client socket belongs to this user; unknown counts as someone else."""
            return tcp_owner(app.port, self.client_address[1]) == os.getuid()

        def _session_ok(self) -> bool:
            try:
                jar = SimpleCookie(self.headers.get("Cookie", ""))
            except CookieError:
                return False
            morsel = jar.get(COOKIE)
            return bool(morsel) and secrets.compare_digest(morsel.value, app.session_key)

        def _allowed(self, write: bool) -> bool:
            if not self._from_me():
                self.close_connection = True
                return self._refuse(HTTPStatus.FORBIDDEN, "not your server")
            if self.headers.get("Host") not in app.hosts:
                return self._refuse(HTTPStatus.FORBIDDEN, "bad host")
            if self.headers.get("Sec-Fetch-Site") == "cross-site":
                return self._refuse(HTTPStatus.FORBIDDEN, "cross-site request")
            if write and not self._session_ok():
                return self._refuse(HTTPStatus.UNAUTHORIZED, "not logged in; open Wirenote from the app menu")
            if write:
                origin = self.headers.get("Origin")
                if origin and origin not in {f"http://{h}" for h in app.hosts}:
                    self._drain()
                    self._error(HTTPStatus.FORBIDDEN, "bad origin")
                    return False
                if not secrets.compare_digest(self.headers.get("X-Wirenote-Token", ""), app.token):
                    self._drain()
                    self._error(HTTPStatus.FORBIDDEN, "missing token; reload the page")
                    return False
            return True

        def _body(self) -> dict:
            if hasattr(self, "_payload"):
                return self._payload
            n = int(self.headers.get("Content-Length") or 0)
            if n > 1 << 20:
                raise Invalid("request too large")
            raw = self.rfile.read(n) if n else b"{}"
            try:
                data = json.loads(raw or b"{}")
            except ValueError as e:
                raise Invalid("bad JSON") from e
            if not isinstance(data, dict):
                raise Invalid("expected an object")
            self._payload = data
            return data

        def _dispatch(self, fn):
            try:
                fn()
            except NotFound as e:
                self._error(HTTPStatus.NOT_FOUND, f"not found: {e}")
            except Invalid as e:
                self._error(HTTPStatus.BAD_REQUEST, str(e))
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as e:  # noqa: BLE001 - keep serving; report the error
                # The query can hold search words: log the path only.
                print(f"ui: {self.command} {urlsplit(self.path).path}: {e!r}",
                      file=sys.stderr, flush=True)
                self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "internal error")

        # GET ---------------------------------------------------------------

        def do_HEAD(self):
            self.do_GET()

        def do_GET(self):
            if not self._allowed(write=False):
                return
            self._dispatch(self._get)

        def _login_page(self, status: int):
            self._send(status, (STATIC / "login.html").read_bytes(), "text/html; charset=utf-8",
                       {"Content-Security-Policy": "default-src 'none'; style-src 'self'; "
                                                   "img-src 'self'; frame-ancestors 'none'"})

        def _login(self, query: str):
            q = parse_qs(query)
            if not redeem_login_code(q.get("code", [""])[0]):
                return self._login_page(HTTPStatus.FORBIDDEN)
            nxt = q.get("next", ["#/"])[0]
            self._send(HTTPStatus.SEE_OTHER, b"", "text/plain", {
                "Location": "/" + (nxt if NEXT_ROUTE.match(nxt) else "#/"),
                "Set-Cookie": f"{COOKIE}={app.session_key}; HttpOnly; SameSite=Strict; Path=/"})

        def _get(self):
            url = urlsplit(self.path)
            path = url.path
            if path == "/login":
                return self._login(url.query)
            if path.startswith("/static/"):
                return self._static(path.removeprefix("/static/"))
            if not self._session_ok():
                if path in ("/", "/index.html"):
                    return self._login_page(HTTPStatus.UNAUTHORIZED)
                return self._error(HTTPStatus.UNAUTHORIZED, "not logged in; open Wirenote from the app menu")
            if path in ("/", "/index.html"):
                return self._send(200, app.index_html(), "text/html; charset=utf-8", {
                    "Content-Security-Policy":
                        "default-src 'self'; media-src 'self' blob:; img-src 'self' data:; "
                        "style-src 'self'; script-src 'self'; frame-ancestors 'none'; "
                        "base-uri 'none'; form-action 'self'"})
            if path == "/api/status":
                return self._json(app.store.status())
            if path == "/api/calls":
                q = parse_qs(url.query).get("q", [""])[0]
                return self._json({"calls": app.store.calls(q)})
            if path == "/api/upcoming":
                return self._json({"events": app.store.upcoming()})
            if path == "/api/actions":
                return self._json({"items": app.store.action_items()})
            if path == "/api/asks":
                return self._json({"asks": app.store.asks()})
            if path == "/api/settings":
                return self._json(app.store.settings_info())
            if path == "/api/doctor":
                return self._json(app.store.doctor())
            if path == "/api/updates":
                return self._json(app.store.updates_info())
            if path == "/api/changelog":
                return self._json(app.store.changelog())
            m = ROUTE_CALL.match(path)
            if m and not m.group(2):
                return self._json(app.store.call(m.group(1)))
            if m and m.group(2) == "/audio":
                return self._file(app.store.mixed_audio(m.group(1)), "audio/ogg")
            raise NotFound(path)

        def _static(self, rel: str):
            target = (STATIC / rel).resolve()
            if STATIC.resolve() not in target.parents or not target.is_file():
                raise NotFound(rel)
            ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype == "text/javascript":
                ctype += "; charset=utf-8"
            self._send(200, target.read_bytes(), ctype)

        def _file(self, path: Path, ctype: str):
            """Serve with HTTP Range, so the audio player can seek."""
            size = path.stat().st_size
            rng = re.match(r"bytes=(\d*)-(\d*)$", self.headers.get("Range", ""))
            if not rng:
                return self._send(200, path.read_bytes(), ctype, {"Accept-Ranges": "bytes"})
            start = int(rng.group(1)) if rng.group(1) else max(0, size - int(rng.group(2) or 0))
            end = int(rng.group(2)) if rng.group(1) and rng.group(2) else size - 1
            end = min(end, size - 1)
            if start > end:
                return self._send(416, b"", ctype, {"Content-Range": f"bytes */{size}"})
            with path.open("rb") as f:
                f.seek(start)
                body = f.read(end - start + 1)
            self._send(206, body, ctype, {"Accept-Ranges": "bytes",
                                          "Content-Range": f"bytes {start}-{end}/{size}"})

        # POST / DELETE -----------------------------------------------------

        def _read_body_first(self, handler):
            # Every route must consume its body: on a kept-alive connection unread
            # bytes would be taken as the start of the next request.
            def run():
                # One handler serves every request on a connection: drop the last body.
                self.__dict__.pop("_payload", None)
                self._body()
                handler()
            return run

        def do_POST(self):
            if self._allowed(write=True):
                self._dispatch(self._read_body_first(self._post))

        def do_PUT(self):
            if self._allowed(write=True):
                self._dispatch(self._read_body_first(self._put))

        def do_DELETE(self):
            if self._allowed(write=True):
                self._dispatch(self._read_body_first(self._delete))

        def _put(self):
            if urlsplit(self.path).path != "/api/settings":
                raise NotFound(self.path)
            body = self._body()
            return self._json(app.store.update_settings(body.get("changes"),
                                                        body.get("confirm_cloud") is True))

        def _post(self):
            path = urlsplit(self.path).path
            store = app.store
            if path == "/api/stop":
                return self._json(_control("stop-in-app"))
            if path == "/api/record":
                return self._json(_control("record-now"))
            if path == "/api/ask":
                return self._json(store.ask_all(str(self._body().get("question", ""))))
            if path == "/api/vocabulary":
                terms = self._body().get("terms", [])
                if not isinstance(terms, list):
                    raise Invalid("terms must be a list")
                return self._json({"terms": store.set_vocabulary(terms)})
            if path == "/api/settings/anthropic-key":
                return self._json(store.set_api_key(str(self._body().get("key", ""))))
            if path == "/api/calendars":
                return self._json({"calendars": store.add_calendar(str(self._body().get("url", "")))})
            if path == "/api/updates/seen":
                store.mark_seen()
                return self._json({"ok": True})
            if path == "/api/updates/check":
                return self._json(store.check_updates())
            if path == "/api/updates/setting":
                return self._json(store.set_update_check(self._body().get("check") is True))
            if path == "/api/updates/install":
                return self._json(store.start_update())
            m = ROUTE_CALL.match(path)
            if not m:
                raise NotFound(path)
            rec_id, action, index = m.group(1), m.group(2), m.group(3)
            body = self._body()
            if action == "/resummarize":
                return self._json({"job": store.requeue(rec_id, summary_only=True)})
            if action == "/retranscribe":
                return self._json({"job": store.requeue(rec_id, summary_only=False)})
            if action == "/ask":
                return self._json(store.ask(rec_id, str(body.get("question", ""))))
            if action == "/notes":
                base = body.get("base")
                store.save_my_notes(rec_id, str(body.get("text", "")),
                                    None if base is None else str(base))
                return self._json({"ok": True})
            if action == "/speaker":
                store.rename_speaker(rec_id, str(body.get("label", "")), str(body.get("name", "")),
                                     bool(body.get("remember")), str(body.get("merge_into", "")))
                return self._json({"ok": True})
            if action == "/followup":
                return self._json(store.followup(rec_id))
            if action == "/rename":
                store.rename(rec_id, str(body.get("title", "")))
                return self._json({"ok": True})
            if action == "/action" and index is not None:
                store.set_action_done(rec_id, int(index), bool(body.get("done")))
                return self._json({"ok": True})
            if action == "/open":
                subprocess.Popen(["xdg-open", str(store.note_path(rec_id))],
                                 start_new_session=True, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
                return self._json({"ok": True})
            if action == "/folder":
                subprocess.Popen(["xdg-open", str(store.rec_dir(rec_id))],
                                 start_new_session=True, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
                return self._json({"ok": True})
            raise NotFound(path)

        def _delete(self):
            path = urlsplit(self.path).path
            if path.startswith("/api/voices/"):
                app.store.forget_voice(unquote(path.removeprefix("/api/voices/")))
                return self._json({"ok": True})
            if path.startswith("/api/calendars/"):
                cal_id = path.removeprefix("/api/calendars/")
                return self._json({"calendars": app.store.remove_calendar(cal_id)})
            m = ROUTE_CALL.match(path)
            if not m:
                raise NotFound(self.path)
            if m.group(2) == "/audio":
                app.store.delete_audio(m.group(1))
            elif not m.group(2):
                app.store.delete_call(m.group(1))
            else:
                raise NotFound(self.path)
            self._json({"ok": True})

    return Handler


def _control(command: str) -> dict:
    from ..detector.daemon import control_socket_path

    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(5)
            s.connect(str(control_socket_path()))
            s.sendall(f"{command}\n".encode())
            return json.loads(s.makefile().readline())
    except (OSError, ValueError) as e:
        return {"ok": False, "error": f"daemon not reachable: {e}"}


def serve(settings: Settings, state_dir: Path, port: int, open_browser: bool = False) -> None:
    app = App(settings, state_dir, port)
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(app))
    httpd.daemon_threads = True
    print(f"wirenote ui on http://127.0.0.1:{port}/ (open it with `wirenote open`)",
          file=sys.stderr, flush=True)
    if open_browser:
        from .launch import open_ui
        open_ui(port)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
