# UI design note: local web app for past calls

Status: **requested 2026-10-26 ("option 1"), built straight away.** This note records the design.

## Look and feel

Patterns from public material on other meeting-note tools. Only the patterns are used, no branding.

- **Home:** "Coming up" (calendar) at the top, then past calls grouped by day (Today, Yesterday, This week, Earlier), with search across all calls.
- **Call page:**
  - Title, then a meta line (date, time, length, app or service, attendees).
  - Tabs: **Notes** (summary, action items with owner and due, decisions, open questions) and **Transcript**.
  - Each summary point and action item has a "jump to moment" link into the transcript. These apps use citations for this; wirenote has none, so the UI matches the point to the best transcript paragraph by word overlap.
- **Ask:** a question box on the call page. The configured summary backend answers from the transcript and cites timestamps, which link to the moment. Wispr Flow Notetaker answers back to transcript moments; Granola has chat.
- **Retry summary,** re-transcribe, open the Markdown, copy the summary as Markdown, delete audio, delete call.
- **Audio player** pinned to the bottom: play/pause, ±15 s, speed, scrubber. Clicking a timestamp seeks.
- **Live status:** a "Recording Chrome · 12:03" banner with Stop, and "Transcribing…" or "Summarizing…" badges on calls being processed.
- **Visual style:** quiet, warm off-white (or dark), Inter (bundled, OFL), generous spacing, one green accent, "Me" in the accent and "Others" in neutral.

## Architecture

- `wirenote ui` runs a stdlib `ThreadingHTTPServer` on **127.0.0.1:8765** (`ui_port`), as its own user service (`wirenote-ui.service`), so a UI bug never touches detection or recording.
- **Data:** the recordings directory (`manifest.json`, `transcript.json`, `summary.json`), the jobs database and `status.json`. There is no new store.
  - The Markdown note stays the user's file. Ticking an action item updates both `summary.json` and the `- [ ]` line in the note.
- **Audio:** the two tracks are mixed on first play with ffmpeg (each segment delayed by its offset) into `mix.ogg` (Opus, 32 kbit/s) and cached. Served with HTTP Range so seeking works.
- **Safety:** the server binds to localhost only and checks the Host header (against DNS rebinding). Every write needs a per-start token embedded in the page, so another website open in the browser cannot trigger deletes.
- **Frontend:** vanilla JS, no build step, shipped in the package (`ui/static/`).
- **Launching:** a desktop entry (`wirenote` in the app launcher) opens the page. The "Meeting note ready" notification opens the call in the UI when it runs, else the Markdown file.

## Build notes (2026-10-26)

- **Checked in headless Chrome** against a scratch demo set (a German call with a real local summary, an English call with audio, one call being processed, one with a failed summary). Covered: light and dark mode, notes, transcript, Ask, the player, and no console errors on any page.
- **Ask with qwen3:4b** answered "Wann ist das Release und wer schreibt die Release Notes?" correctly ("Am 10. November, Anna") in 13 s, citing the two right transcript lines.
- **Audio:** mixing an 11 s two-track call to Opus took 0.2 s; Range requests return 206.
- **Bugs found while building:**
  - Icons drawn through `<use>` into the external sprite do not inherit the sprite's `fill`/`stroke`; they are set in CSS on `.icon`.
  - The jobs SQLite connection is shared across server threads (`check_same_thread=False`), behind the store's lock.
