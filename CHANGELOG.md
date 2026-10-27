# Changelog

All notable changes are listed here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning](https://semver.org/).

## [0.2.0] - 2026-10-27

### Added

- When a recording stops, the call opens in the meetings app in your default browser, where the note fills in once it is written. Turn it off with `open_call_after_recording = false` in `config.toml`.

### Changed

- Stop in the meetings app opens no second tab, and "Recording saved" then says "stopped in the app".

## [0.1.0] - 2026-10-26

First public release. Runs on Linux (x86_64 and ARM64) with PipeWire and Python 3.11 to 3.14.

### Added

- Call detection on PipeWire, with a Record / Ignore prompt; app rules in `apps.toml`.
- Two-track recording (your mic and the system audio), auto-stop, crash recovery.
- Transcription with faster-whisper, speakers told apart and named, local summaries through Ollama or, opt-in, Claude.
- Markdown notes and a local meetings app: search, playback, notes during the call, action items, Ask across meetings, follow-up email.
- Settings page: summaries, transcription, consent line, Claude key and calendars.
- `wirenote setup install` / `uninstall`, `wirenote doctor`, `wirenote open`.
- Works outside KDE: clipboard through `wl-copy`, `xclip` or `xsel`, and prompts without notification buttons.

### Security

- Recordings, notes and settings are private to your user (umask 077).
- The meetings app only serves your own user, through a one-time login.
- Calendar feeds are limited and must use https; untrusted text is escaped in notifications and notes.
- Logs leave out room names, note paths and speaker names; model downloads are pinned.

[0.2.0]: https://github.com/knowhrishi/wirenote/releases/tag/v0.2.0
[0.1.0]: https://github.com/knowhrishi/wirenote/releases/tag/v0.1.0
