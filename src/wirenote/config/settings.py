# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Hrishikesh Jadhav
"""config.toml: every default comes from docs/dev/PLAN.md › Defaults."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, fields, replace
from pathlib import Path

APP = "wirenote"


def config_dir() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / APP


def state_dir() -> Path:
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / APP


def data_dir() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / APP


@dataclass(frozen=True)
class Settings:
    # docs/dev/PLAN.md › Defaults › Change detection: PipeWire events + 1 s reconcile
    reconcile_s: float = 1.0
    # › Disappearance grace
    grace_s: float = 4.0
    # › Dwell before prompt, meeting app
    dwell_meeting_s: float = 2.0
    # › Dwell before prompt, browser
    dwell_browser_s: float = 5.0
    # › After dismiss: silent until the app's stream ends, then 60 s
    cooldown_s: float = 60.0
    # › Min app runtime before auto-stop (a guess, tune from M1 logs)
    min_app_runtime_s: float = 20.0
    # › Min recording age before auto-stop
    min_recording_age_s: float = 30.0
    # › Max recording length
    max_recording_s: float = 4 * 3600.0
    # › Opt-out nudge: dismisses of one app before offering "ignore"
    nudge_after_dismisses: int = 5
    # docs/dev/PLAN.md › Capture: our own streams carry this node.name prefix
    own_node_prefix: str = "wirenote-"

    # Capture (M2) -----------------------------------------------------------
    # docs/dev/PLAN.md › Defaults › Capture heartbeat: 5 s per track
    heartbeat_s: float = 5.0
    # "" means ~/.local/share/wirenote/recordings
    recordings_dir: str = ""
    # Record this output's monitor instead of following the default sink ("" = default)
    system_target: str = ""
    # Copied to the clipboard on Record when set (docs/dev/PLAN.md › Prompt: consent line)
    consent_line: str = ""

    # Transcript (M3) --------------------------------------------------------
    # docs/dev/PLAN.md › Processing: faster-whisper; model chosen in docs/dev/m2-m4-design.md
    whisper_model: str = "large-v3-turbo"
    whisper_language: str = "auto"     # or "de", "en", ...
    # docs/dev/engines-design.md: auto | whisper (faster-whisper) | parakeet | whisper.cpp;
    # auto is Parakeet where it fits the language, else Whisper (PLAN.md › Defaults)
    asr_engine: str = "auto"
    whispercpp_bin: str = "whisper-cli"
    whispercpp_model: str = "ggml-large-v3-turbo-q5_0.bin"   # a file, or one from ggerganov/whisper.cpp
    # docs/dev/live-design.md: a live transcript during the call, on two CPU threads
    live_transcript: bool = False
    # › Flag window: a flag selects what was said this long before and after it
    flag_before_s: float = 30.0
    flag_after_s: float = 10.0
    # Consecutive lines of one speaker closer than this join into a paragraph
    merge_gap_s: float = 2.0
    # Echo dedupe (docs/dev/m2-m4-design.md › M3): overlap share and text similarity
    echo_overlap: float = 0.5
    echo_similarity: float = 0.8
    # Who spoke when on the Others track (docs/dev/features-2-design.md › Speakers).
    # Model and threshold picked on a 3-speaker test: 3 found, 87% of speech right.
    diarize: bool = True
    diarize_model: str = "nemo_en_titanet_small.onnx"
    diarize_threshold: float = 0.9
    # Cosine similarity at which a speaker counts as a remembered voice
    # (same voice measured 0.66-0.88, different voices <= 0.32).
    voice_match: float = 0.55
    notes_dir: str = "~/Meetings"
    # docs/dev/PLAN.md › Processing: delete audio after 30 days
    audio_retention_days: int = 30

    # Summary (M4) -----------------------------------------------------------
    summary_backend: str = "ollama"    # ollama | anthropic | openai | none
    ollama_url: str = "http://127.0.0.1:11434"
    ollama_model: str = "qwen3:4b"
    anthropic_model: str = "claude-opus-5-5"
    # docs/dev/PLAN.md › Defaults › OpenAI-compatible API
    openai_url: str = "https://openrouter.ai/api/v1"
    openai_model: str = "anthropic/claude-sonnet-5.5"
    # docs/dev/PLAN.md › Processing: long calls are summarized in chunks of about 15 min
    summary_chunk_s: float = 900.0
    summary_language: str = "auto"     # "auto": the transcript's language
    # docs/dev/PLAN.md › Defaults › Summary template: a template id, see `wirenote templates`
    default_template: str = "general"

    # Calendar (M4) ----------------------------------------------------------
    calendar_urls: tuple = ()          # ICS URLs or file paths
    # docs/dev/PLAN.md › Defaults › Calendar refresh: every 5 min
    calendar_refresh_s: float = 300.0
    # › Calendar window: starts in <=10 min, started <=5 min ago, or in progress
    calendar_before_s: float = 600.0
    calendar_after_start_s: float = 300.0

    # UI (docs/dev/ui-design.md): local web app on 127.0.0.1
    ui_port: int = 8765
    # Open the call in the web UI by itself when its recording stops
    open_call_after_recording: bool = True
    # docs/dev/PLAN.md › Defaults › Tray icon: on where the desktop hosts one
    tray_icon: bool = True
    # › Global shortcuts: off; `wirenote setup shortcuts` or Settings › Desktop turns them on
    global_shortcuts: bool = False
    # › Launcher search: off; `wirenote setup search` or Settings › Desktop turns it on
    launcher_search: bool = False

    # Updates (docs/dev/updates-design.md) -----------------------------------
    # docs/dev/PLAN.md › Defaults › Update check: off; when on, ask PyPI once a day
    update_check: bool = False

    def recordings_path(self) -> Path:
        return Path(self.recordings_dir).expanduser() if self.recordings_dir \
            else data_dir() / "recordings"

    def notes_path(self) -> Path:
        return Path(self.notes_dir).expanduser()


DEFAULT_CONFIG_TOML = """# wirenote settings. Defaults come from docs/dev/PLAN.md › Defaults; uncomment to change.
# reconcile_s = 1.0            # 1 s reconcile next to PipeWire events
# grace_s = 4.0                # app may drop the mic this long without counting as gone
# dwell_meeting_s = 2.0        # mic use before prompting, meeting apps
# dwell_browser_s = 5.0        # mic use before prompting, browsers
# cooldown_s = 60.0            # after Ignore: silent until the stream ends, then this long
# min_app_runtime_s = 20.0     # no auto-stop before the app has used the mic this long
# min_recording_age_s = 30.0   # no auto-stop for younger recordings
# max_recording_s = 14400.0    # hard stop after 4 h
# nudge_after_dismisses = 5    # offer "always ignore" after this many dismisses

# Capture
# heartbeat_s = 5.0            # a track whose file stops growing this long is reported
# recordings_dir = ""          # default ~/.local/share/wirenote/recordings
# system_target = ""           # record this output instead of the default one
# consent_line = ""            # copied to the clipboard on Record, e.g.
#                              # "Heads-up: I'm recording this call for my notes. OK for everyone?"

# Transcript
# whisper_model = "large-v3-turbo"   # or "small", "medium", ...
# whisper_language = "auto"          # or "de", "en"
# asr_engine = "auto"                # auto | whisper | parakeet (faster on a CPU) | whisper.cpp (GPU)
# whispercpp_bin = "whisper-cli"     # for asr_engine = "whisper.cpp"
# whispercpp_model = "ggml-large-v3-turbo-q5_0.bin"
# live_transcript = false            # show what is said during the call (two CPU threads)
# diarize = true                     # tell the other speakers apart
# diarize_threshold = 0.9            # higher: fewer speakers; lower: more
# voice_match = 0.55                 # how close a voice must be to a remembered one
# flag_before_s = 30.0               # a flagged moment covers this much speech before it...
# flag_after_s = 10.0                # ...and this much after
# merge_gap_s = 2.0
# echo_overlap = 0.5
# echo_similarity = 0.8
# notes_dir = "~/Meetings"
# audio_retention_days = 30          # audio is deleted this long after the note

# Summary
# summary_backend = "ollama"         # ollama | anthropic | openai | none
# ollama_url = "http://127.0.0.1:11434"
# ollama_model = "qwen3:4b"
# anthropic_model = "claude-opus-5-5"  # API key: ~/.config/wirenote/secrets.toml, or `ant auth login`
# openai_url = "https://openrouter.ai/api/v1"  # any OpenAI-compatible API; http:// only on this computer
# openai_model = "anthropic/claude-sonnet-5.5"  # API key: ~/.config/wirenote/secrets.toml
# summary_chunk_s = 900.0
# summary_language = "auto"          # or "German", "English"
# default_template = "general"       # for calls no title or app rule matches; see `wirenote templates`

# Calendar
# calendar_urls = []                 # ICS URLs or files; none means no calendar matching
# calendar_refresh_s = 300.0
# calendar_before_s = 600.0
# calendar_after_start_s = 300.0

# UI
# ui_port = 8765                     # wirenote ui serves http://127.0.0.1:<port>/
# open_call_after_recording = true   # open the call in the browser when its recording stops
# tray_icon = true                   # an icon in the panel: recording state, Stop, Flag
# global_shortcuts = false           # Ctrl+Alt+R starts or stops, Ctrl+Alt+F flags, in any app
# launcher_search = false            # find meetings from KRunner or GNOME search: wirenote setup search

# Updates
# update_check = false               # once a day, ask pypi.org whether a newer Wirenote exists
"""


def parse_settings(text: str) -> Settings:
    data = tomllib.loads(text)
    known = {f.name: f.type for f in fields(Settings)}
    unknown = set(data) - set(known)
    if unknown:
        raise ValueError(f"config.toml: unknown setting(s) {sorted(unknown)}")
    base = Settings()
    coerced = {}
    for k, v in data.items():
        default = getattr(base, k)
        coerced[k] = v if isinstance(default, bool) else type(default)(v)
    return replace(base, **coerced)


def load_settings(path: Path) -> Settings:
    if not path.exists():
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.write_text(DEFAULT_CONFIG_TOML)
    return parse_settings(path.read_text())
