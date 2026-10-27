# Changelog

All notable changes are listed here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- Summary templates. A standup now gets "Updates by person" and "Blockers", a 1:1 gets "Feedback" and "Topics for next time", an interview gets what the candidate said, their questions and follow-ups (it never rates the candidate), and a lecture gets key concepts, terms and references. Wirenote picks one from the calendar event's title, or you choose: "Summary template…" in the call page's menu, `wirenote reprocess ID --summary --template standup`, or a default in Settings › Setup. You can keep a choice for every meeting in a calendar series, and stop it again from the same dialog; `--template auto` goes back to the automatic choice. Write your own as a file in `~/.config/wirenote/templates/`; its title words win over the built-in ones. See "Summary templates" in docs/usage.md. `wirenote templates` lists them.
- Flag a moment while you record: press `F` in the meetings app, click "Flag this moment", or run `wirenote flag`. The summary pays attention to what was said around each flag, and the note lists them under "Flagged moments", each with the words said at that moment, even in a long talk by one person. The Word file, "Copy as Markdown" and printing include them too. After a call, click the flag on a transcript line to add one, also on imported recordings. `wirenote toggle` stops the recording or starts one; bind both commands to a keyboard shortcut in your desktop to use them from any window.
- People: a page per person in the meetings app (**People** in the sidebar). It lists the calls you had with them, how long they spoke, their email addresses from the calendar and the action items assigned to them. Names under a call's title and on its speakers open the page. If one person appears under two names, "Same person as…" puts them together; your recordings stay as they are. See "People" in docs/usage.md.
- Meeting prep: 5 minutes before a meeting you have had calls about, a banner shows the last summary's first point and how many action items are still open. "Open prep" opens a page with what was decided, the open items and who is invited; "Coming up" has a Prep link too. No banner while you record or for a meeting with no history. You are left out when calls are compared; set `my_emails` in Settings › Calendars if Wirenote guesses your address wrong. Turn it off with `meeting_prep = false`. See "Before a meeting" in docs/usage.md.

## [0.3.1] - 2026-10-27

### Fixed

- The meetings app showed nothing in 0.3.0: no calls, no settings, only an empty window. Your recordings and notes were never touched; they show again after the update. What you need to do: run `wirenote update`.

## [0.3.0] - 2026-10-27

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
- Summaries with OpenRouter, OpenAI or any OpenAI-compatible API: choose it in Settings, or run `wirenote setup openai`. With one OpenRouter key you can use Claude, GPT, Gemini, Qwen and many more. A server on your own computer, such as LM Studio or llama.cpp, counts as local: nothing leaves the computer. For any other address, the app asks you to confirm first, as for Claude.
- Claude without a saved key: Wirenote uses your `ant auth login` or `ANTHROPIC_API_KEY`. A Claude Pro or Max subscription cannot be used: Anthropic does not allow other apps to use claude.ai logins.
- End-to-end tests: a recorded Jitsi call runs through the real daemon, worker and web app, with PipeWire and Ollama faked; a nightly run uses the real speech models.
- Releases test the built wheel on Python 3.11 to 3.14 before publishing, and data written by every earlier release is checked to still read.

### Changed

- Notes are ready sooner: the speech engine is now chosen for you. Wirenote uses Parakeet when it knows your language and you have no vocabulary set, and Whisper otherwise. The first note after the update downloads Parakeet's model (about 670 MB) once. If a call comes out worse, transcribe it again with Whisper from the call page's menu. What you need to do: nothing; to keep Whisper for every call, choose Whisper in Settings › Setup.
- A title you give a call in the app, or with `wirenote import --title`, now also names its Markdown note when the note is written again.
- The job queue (`jobs.db`) records its schema version, so later releases can change it without breaking existing queues. Nothing to do on upgrade.

### Fixed

- Fewer phantom speakers. A laugh, a cough or a word through a bad line was sometimes counted as a new person, so a call with three people could show seven. Someone with less than about 8 seconds of speech in the whole call now counts with the person who spoke just before or after.
- A summary no longer fails because memory is short. Wirenote waits for enough free memory before Ollama loads its model, instead of the kernel stopping Ollama or another program. If the summary still fails, because Ollama restarts or an online API is busy or out of reach, the note is written with the transcript and the summary is tried again a few minutes later, up to three times.
- `contrib/ollama.service` halves the memory Ollama needs to hold the transcript while it reads it. What you need to do: if you use it, copy it to `~/.config/systemd/user/` again and run `systemctl --user daemon-reload && systemctl --user restart ollama`.

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

[0.3.1]: https://github.com/knowhrishi/wirenote/releases/tag/v0.3.1
[0.3.0]: https://github.com/knowhrishi/wirenote/releases/tag/v0.3.0
[0.2.0]: https://github.com/knowhrishi/wirenote/releases/tag/v0.2.0
[0.1.0]: https://github.com/knowhrishi/wirenote/releases/tag/v0.1.0
