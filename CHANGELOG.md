# Changelog

All notable changes are listed here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- "What's new": after an update, the meetings app shows what changed since the version you last used, once. Click the version number at the bottom of the sidebar to read every release's notes again, or run `wirenote changelog`.
- `wirenote update` installs the newest version and restarts Wirenote. It never ends a recording: it refuses during a call, or with `--wait` updates once the call ends. `wirenote update --check` only checks, and `wirenote update --to 0.2.0` goes back to an earlier version. It works for installs made with uv or pipx.
- Update checks, off unless you turn them on. Once a day, Wirenote asks pypi.org whether a new version is out. If one is, you get one notification, never during a call, and a banner in the app with its notes and an Update button. `wirenote setup install` asks whether you want them, and Settings › Updates has the switch. See PRIVACY.md for what this sends.
- `wirenote --version`.
- Two more speech engines. **Parakeet** turns speech into text several times faster on a normal CPU and knows 25 European languages. **whisper.cpp** uses a `whisper-cli` you build yourself, for example to transcribe on an AMD or Intel GPU. Choose in Settings › Setup; see "Speech engines" in docs/usage.md.
- Live transcript, off unless you turn it on in Settings › Setup: see what is said while you record, a few seconds behind, under your notes on the call page. It uses two CPU threads during the call. If its model is not downloaded yet, it starts from the call after your next note.
- Transcribe one call again with another engine, model or language: "Transcribe again…" in the call page's menu, or `wirenote reprocess ID --engine parakeet --language de`. Your settings stay as they are.
- The note's footer names the speech engine that made the transcript.
- Import a recording made elsewhere, such as a phone recording, a meeting app's own recording or a video: click Import in the meetings app, drop the file on its window, or run `wirenote import FILE`. It becomes a note like a call. Needs ffmpeg (`sudo apt install ffmpeg`).
- Export a call: "Download as Word (.docx)" and "Print or save as PDF" in the call page's menu, or `wirenote export ID`.
- End-to-end tests: a recorded Jitsi call runs through the real daemon, worker and web app, with PipeWire and Ollama faked; a nightly run uses the real speech models.
- Releases test the built wheel on Python 3.11 to 3.14 before publishing, and data written by every earlier release is checked to still read.

### Changed

- A title you give a call in the app, or with `wirenote import --title`, now also names its Markdown note when the note is written again.
- Notes are ready sooner: the speech engine is now chosen for you. Wirenote uses Parakeet when it knows your language and you have no vocabulary set, and Whisper otherwise. The first note after the update downloads Parakeet's model (about 670 MB) once. If a call comes out worse, transcribe it again with Whisper from the call page's menu. What you need to do: nothing; to keep Whisper for every call, choose Whisper in Settings › Setup.
- The job queue (`jobs.db`) records its schema version, so later releases can change it without breaking existing queues. Nothing to do on upgrade.

## [0.2.0] - 2026-10-27

### Added

- When a recording stops, the call opens in the meetings app in your default browser, where the note fills in once it is written. Turn it off with `open_call_after_recording = false` in `config.toml` and `systemctl --user restart wirenote`.

### Changed

- Stop in the meetings app opens no second tab, and "Recording saved" then says "stopped in the app".

### Fixed

- Your notes for a call open in two tabs: a save from the older tab no longer overwrites what the newer one saved; it asks you to reload instead.

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
