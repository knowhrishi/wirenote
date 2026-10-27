# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hrishikesh Jadhav
"""Meeting summary through a pluggable backend (docs/dev/PLAN.md › Processing, step 3).

Backends: Ollama on this machine (the default, nothing leaves the laptop), the
Claude API, or an OpenAI-compatible API: OpenRouter, OpenAI, or a server on this
machine such as LM Studio or llama.cpp. All return JSON for one schema: title, summary points,
decisions, action items with owners, open questions. Transcripts longer than
summary_chunk_s are summarized per chunk, then the chunk notes are combined.

A summary that fails for a passing reason (too little free memory to load the model,
the model server restarting, a rate limit) raises SummaryUnavailable; the worker then
writes the note and queues the summary again.
"""

from __future__ import annotations

import ipaddress
import json
import socket
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from dataclasses import asdict, dataclass, field
from typing import Protocol

from ..config.secrets import load_secrets, secrets_path
from ..config.settings import Settings
from ..net.peer import listener_uid, trusted_owner
from .merge import Line, as_text

_LIST = {"type": "array", "items": {"type": "string"}}
_ACTIONS = {"type": "array", "items": {
    "type": "object",
    "properties": {"owner": {"type": "string"}, "task": {"type": "string"},
                   "due": {"type": "string"}},
    "required": ["owner", "task", "due"], "additionalProperties": False}}
_NOTES = {"summary": _LIST, "decisions": _LIST, "action_items": _ACTIONS,
          "open_questions": _LIST}

CHUNK_SCHEMA = {"type": "object", "properties": _NOTES, "required": list(_NOTES),
                "additionalProperties": False}
FINAL_SCHEMA = {"type": "object", "properties": {"title": {"type": "string"}, **_NOTES},
                "required": ["title", *_NOTES], "additionalProperties": False}

SYSTEM = """You write meeting notes from a call transcript.
Speakers: "Me" is the person who recorded the call. Other people appear by name, \
as "Speaker N" when their name is unknown, or as "Others" when several people share \
one label. Use names as owners where you know them.
The transcript comes from speech recognition, so expect some misheard words; \
infer the meaning, do not quote errors.
Only state what the transcript supports. Do not invent names, dates or decisions.
Write every field, including the title, in {language}.

Fields:
- summary: 3 to 8 short bullet points covering the main topics and outcomes.
- decisions: things the participants agreed on. Empty if none.
- action_items: every task someone said they or another person will do, \
including tasks "Me" took on. One entry per task. owner is "Me", the name \
mentioned for it, or "Others" when unclear. due is the deadline said for that \
task, or "" if none was said for it; do not reuse another task's date.
- open_questions: questions raised but not resolved. Empty if none."""

TITLE_FIELD = "\n- title: a short title for the meeting, at most 8 words."

COMBINE = """These are notes for consecutive parts of one call, in order. Merge them \
into notes for the whole call: remove repeats, keep every distinct decision, \
action item and open question, and keep the summary to at most 8 points."""


class SummaryError(Exception):
    pass


class SummaryUnavailable(SummaryError):
    """Worth trying again later: not enough memory, the server restarting, a rate limit."""


# docs/dev/PLAN.md › Defaults › Summary memory
MEMORY_MARGIN = 1024 ** 3      # the context cache and Ollama's buffers, on top of the model file
MEMORY_WAIT_S = 300.0          # how long to wait for that much free memory
MEMORY_POLL_S = 15.0
RETRY_PAUSE_S = 30.0           # before asking again after a passing failure


def free_memory() -> int | None:
    """Bytes a new process can take without the kernel killing one: free RAM plus free swap."""
    try:
        info = dict(line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines())
        return sum(int(info[k].split()[0]) * 1024 for k in ("MemAvailable", "SwapFree"))
    except (OSError, KeyError, ValueError):
        return None


@dataclass
class ActionItem:
    owner: str
    task: str
    due: str = ""


@dataclass
class Summary:
    title: str = ""
    summary: list[str] = field(default_factory=list)
    decisions: list[str] = field(default_factory=list)
    action_items: list[ActionItem] = field(default_factory=list)
    open_questions: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


class Backend(Protocol):
    name: str

    def complete_json(self, system: str, user: str, schema: dict) -> dict: ...


# Backends --------------------------------------------------------------------

def pin_local(url: str) -> tuple[str, str, int] | None:
    """For a URL on this machine: (the URL with its host replaced by the numeric
    loopback address, that address, the port); None for another machine.

    The owner check and the request must use one address: "localhost" may
    resolve to ::1, where someone else can listen while Ollama has 127.0.0.1."""
    u = urlsplit(url)
    host = u.hostname or ""
    port = u.port or (443 if u.scheme == "https" else 80)
    if host == "localhost":
        ip = "127.0.0.1"
    else:
        try:
            ip = str(ipaddress.ip_address(host))
        except ValueError:
            try:
                ip = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)[0][4][0]
            except (OSError, IndexError):
                return None
    if not ipaddress.ip_address(ip).is_loopback:
        return None
    netloc = f"[{ip}]:{port}" if ":" in ip else f"{ip}:{port}"
    return urlunsplit((u.scheme, netloc, u.path, u.query, u.fragment)).rstrip("/"), ip, port


class OllamaBackend:
    """Ollama's /api/chat with a JSON schema as the format."""

    def __init__(self, url: str, model: str, timeout_s: float = 900.0) -> None:
        self.url = url.rstrip("/")
        self.local = pin_local(self.url)
        self.model = model
        self.timeout_s = timeout_s
        self.name = f"ollama:{model}"

    def complete_json(self, system: str, user: str, schema: dict) -> dict:
        # Other accounts can listen on loopback too: never hand a transcript to
        # their server. Nothing listening is fine; the request fails below.
        url = self.url
        if self.local:
            url, ip, port = self.local
            owner = listener_uid(port, ip)
            if owner is not None and not trusted_owner(owner):
                raise SummaryError(f"Ollama on port {port} belongs to another user; "
                                   "not sending the transcript")
            self._wait_for_memory(url)
        body = {"model": self.model, "stream": False, "format": schema, "think": False,
                "messages": [{"role": "system", "content": system},
                             {"role": "user", "content": user}],
                # 8k tokens holds a 15 min chunk; the model unloads soon after, so
                # its RAM is free again for the next call or transcription.
                "keep_alive": "60s", "options": {"num_ctx": 8192, "temperature": 0.2}}
        req = urllib.request.Request(f"{url}/api/chat", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                data = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300]
            # 5xx: the model failed to load or its runner died, often for lack of memory.
            error = SummaryUnavailable if e.code >= 500 else SummaryError
            raise error(f"Ollama returned {e.code}: {detail}") from e
        except TimeoutError as e:
            raise SummaryError(f"Ollama did not answer within {self.timeout_s:.0f} s") from e
        except (urllib.error.URLError, OSError) as e:
            # Refused or cut off: not running, or killed and being restarted.
            raise SummaryUnavailable(f"Ollama not reachable at {self.url} ({e}); is it installed "
                                     f"and running, and is the model pulled "
                                     f"(ollama pull {self.model})?") from e
        return _parse_json(data.get("message", {}).get("content", ""))

    def _get(self, url: str, path: str) -> dict:
        try:
            with urllib.request.urlopen(urllib.request.Request(f"{url}{path}"), timeout=10) as resp:
                return json.loads(resp.read())
        except (OSError, ValueError):
            return {}

    def _mine(self, m: dict) -> bool:
        names = {m.get("name"), m.get("model")}
        return self.model in names or f"{self.model}:latest" in names

    def _need(self, url: str) -> int | None:
        """Bytes that loading the model takes: 0 if it is loaded, None if Ollama does not say."""
        if any(self._mine(m) for m in self._get(url, "/api/ps").get("models") or []):
            return 0
        for m in self._get(url, "/api/tags").get("models") or []:
            if self._mine(m) and isinstance(m.get("size"), int):
                return m["size"] + MEMORY_MARGIN
        return None

    def _wait_for_memory(self, url: str) -> None:
        """Loading a model without room for it gets Ollama, or another program, killed by
        the kernel. Wait for room instead, at most MEMORY_WAIT_S."""
        waited = 0.0
        while True:
            need, free = self._need(url), free_memory()
            if not need or free is None or free >= need:
                return
            if waited >= MEMORY_WAIT_S:
                raise SummaryUnavailable(
                    f"not enough free memory for {self.model}: it needs about {need / 1e9:.1f} GB, "
                    f"{free / 1e9:.1f} GB are free. Closing some programs helps")
            time.sleep(MEMORY_POLL_S)
            waited += MEMORY_POLL_S


class AnthropicBackend:
    """Claude API with structured output. Transcripts leave this machine."""

    def __init__(self, model: str, api_key: str | None = None) -> None:
        import anthropic

        # No key in secrets.toml: the SDK falls back to ANTHROPIC_API_KEY or an
        # `ant auth login` profile.
        self.client = anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()
        self.model = model
        self.name = f"anthropic:{model}"

    def complete_json(self, system: str, user: str, schema: dict) -> dict:
        import anthropic

        try:
            resp = self.client.beta.messages.create(
                model=self.model,
                max_tokens=16000,
                system=system,
                messages=[{"role": "user", "content": user}],
                output_config={"effort": "medium",
                               "format": {"type": "json_schema", "schema": schema}},
                # On a policy decline, re-run on Anthropic's recommended fallback model.
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
        except anthropic.AuthenticationError as e:
            raise SummaryError("Claude API: no valid API key (run `wirenote setup claude`, "
                               f"or set anthropic_api_key in {secrets_path()})") from e
        except anthropic.RateLimitError as e:
            raise SummaryUnavailable("Claude API: rate limited") from e
        except anthropic.APIStatusError as e:
            error = SummaryUnavailable if e.status_code >= 500 else SummaryError   # 529: overloaded
            raise error(f"Claude API error {e.status_code}: {e.message}") from e
        except anthropic.APIConnectionError as e:
            raise SummaryUnavailable(f"Claude API not reachable: {e}") from e
        if resp.stop_reason == "refusal":
            raise SummaryError("Claude API declined to summarize this transcript")
        if resp.stop_reason == "max_tokens":
            raise SummaryError("Claude API output was cut off (max_tokens)")
        text = next((b.text for b in resp.content if b.type == "text"), "")
        return _parse_json(text)


class _Rejected(Exception):
    """400 Bad Request: often a server or model that takes no JSON schema."""


class OpenAIBackend:
    """An OpenAI-compatible /chat/completions API (OpenRouter, OpenAI, LM Studio, llama.cpp,
    vLLM). A server on this machine gets Ollama's owner check and counts as local; any
    other gets the transcript, like Claude, and only over https."""

    def __init__(self, url: str, model: str, api_key: str | None = None,
                 timeout_s: float = 900.0) -> None:
        self.url = url.rstrip("/")
        self.local = pin_local(self.url)
        self.host = urlsplit(self.url).hostname or self.url
        self.model = model
        self.api_key = api_key
        self.timeout_s = timeout_s
        # "local:" or the host: the app marks the summary "Local" or "Sent to <host>".
        self.name = f"{'local' if self.local else self.host}:{model}"

    def complete_json(self, system: str, user: str, schema: dict) -> dict:
        url = self.url
        if self.local:
            url, ip, port = self.local
            owner = listener_uid(port, ip)
            if owner is not None and not trusted_owner(owner):
                raise SummaryError(f"the server on port {port} belongs to another user; "
                                   "not sending the transcript")
        elif urlsplit(self.url).scheme != "https":
            raise SummaryError(f"{self.host}: an API on another computer needs https://")
        body = {"model": self.model, "temperature": 0.2,
                "messages": [{"role": "system", "content": system},
                             {"role": "user", "content": user}],
                "response_format": {"type": "json_schema",
                                    "json_schema": {"name": "answer", "schema": schema}}}
        try:
            data = self._post(url, body)
        except _Rejected:
            # Ask again with the schema in the prompt; _ask checks what comes back.
            del body["response_format"]
            body["messages"][0]["content"] = (f"{system}\n\nAnswer with only a JSON object "
                                              f"that matches this JSON schema:\n{json.dumps(schema)}")
            try:
                data = self._post(url, body)
            except _Rejected as e:
                raise SummaryError(f"{self.host} returned 400: {e}") from e
        try:
            text = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as e:
            raise ValueError("no answer in the response") from e
        return _parse_json(text)

    def _post(self, url: str, body: dict) -> dict:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        req = urllib.request.Request(f"{url}/chat/completions", data=json.dumps(body).encode(),
                                     headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300]
            if e.code == 400:
                raise _Rejected(detail) from e
            if e.code in (401, 403):
                raise SummaryError(f"{self.host} rejected the API key ({e.code}); run "
                                   "`wirenote setup openai` or paste a key in Settings") from e
            raise SummaryError(f"{self.host} returned {e.code}: {detail}") from e
        except (urllib.error.URLError, OSError) as e:
            raise SummaryError(f"{self.url} not reachable ({e})") from e
        except ValueError as e:
            raise SummaryError(f"{self.host} did not answer with JSON") from e


def make_backend(settings: Settings) -> Backend | None:
    if settings.summary_backend == "none":
        return None
    if settings.summary_backend == "ollama":
        return OllamaBackend(settings.ollama_url, settings.ollama_model)
    if settings.summary_backend == "anthropic":
        return AnthropicBackend(settings.anthropic_model, load_secrets().get("anthropic_api_key"))
    if settings.summary_backend == "openai":
        key = load_secrets().get("openai_api_key")
        backend = OpenAIBackend(settings.openai_url, settings.openai_model, key)
        if not key and not backend.local:
            raise SummaryError(f"no API key for {backend.host}: run `wirenote setup openai` "
                               "or paste one in Settings")
        return backend
    raise SummaryError(f"unknown summary_backend {settings.summary_backend!r} "
                       "(ollama, anthropic, openai or none)")


# Summarizing -----------------------------------------------------------------

def chunks(lines: list[Line], chunk_s: float) -> list[list[Line]]:
    """Split at paragraph boundaries into windows of about chunk_s seconds."""
    out: list[list[Line]] = []
    for line in lines:
        if not out or line.start - out[-1][0].start >= chunk_s:
            out.append([])
        out[-1].append(line)
    return out


def summarize(lines: list[Line], backend: Backend, chunk_s: float, language: str,
              context: str = "", names: dict[str, str] | None = None) -> Summary:
    if not lines:
        raise SummaryError("empty transcript")
    lang = "the same language as the transcript" if language == "auto" else language
    system = SYSTEM.format(language=lang)
    head = f"{context}\n\n" if context else ""
    parts = chunks(lines, chunk_s)
    if len(parts) == 1:
        data = _ask(backend, system + TITLE_FIELD,
                    f"{head}Transcript:\n{as_text(lines, names)}", FINAL_SCHEMA)
        return _to_summary(data)
    notes = []
    for i, part in enumerate(parts, 1):
        user = f"{head}Transcript, part {i} of {len(parts)}:\n{as_text(part, names)}"
        notes.append(_ask(backend, system, user, CHUNK_SCHEMA))
    user = f"{head}{COMBINE}\n\n" + "\n\n".join(
        f"Part {i}:\n{json.dumps(n, ensure_ascii=False)}" for i, n in enumerate(notes, 1))
    return _to_summary(_ask(backend, system + TITLE_FIELD, user, FINAL_SCHEMA))


def _ask(backend: Backend, system: str, user: str, schema: dict) -> dict:
    """One call. Asked again once if the JSON does not match the schema, and once after a
    pause if it failed for a passing reason, so one chunk does not cost the whole summary."""
    last, bad, paused = None, 0, False
    while bad < 2:
        try:
            data = backend.complete_json(system, user, schema)
            _check(data, schema)
            return data
        except SummaryUnavailable:
            if paused:
                raise
            paused = True
            time.sleep(RETRY_PAUSE_S)
        except (ValueError, KeyError, TypeError) as e:
            last, bad = e, bad + 1
    raise SummaryError(f"{backend.name} returned unusable JSON twice: {last}")


def _parse_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):          # a model that wraps its JSON in a code block
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0]
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("expected a JSON object")
    return data


def _check(data: dict, schema: dict) -> None:
    for key in schema["required"]:
        want = schema["properties"][key]["type"]
        value = data[key]
        if want == "string" and not isinstance(value, str):
            raise TypeError(f"{key}: expected a string")
        if want == "array" and not isinstance(value, list):
            raise TypeError(f"{key}: expected a list")


def _to_summary(data: dict) -> Summary:
    strs = lambda xs: [str(x).strip() for x in xs if str(x).strip()]  # noqa: E731
    items = [ActionItem(str(a.get("owner") or "Unassigned").strip(), str(a.get("task", "")).strip(),
                        str(a.get("due") or "").strip())
             for a in data.get("action_items", []) if isinstance(a, dict) and a.get("task")]
    return Summary(str(data.get("title", "")).strip(), strs(data.get("summary", [])),
                   strs(data.get("decisions", [])), items, strs(data.get("open_questions", [])))
