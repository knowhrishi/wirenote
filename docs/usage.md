# Using Wirenote

## From call to note

1. A call starts (Jitsi in the browser, Discord, Rocket.Chat, or any app in `apps.toml`), and Wirenote shows a notification: **Record** or **Ignore**.
2. You click **Record**. Your mic ("Me") and the system audio (the other people) are recorded as two tracks, and a "Recording …" notification with **Stop** stays up.
3. You leave the call. The recording stops about 4 s later.
4. The note is written in the background: transcript, speakers, summary, action items. Transcription waits while another call is recording.
5. A "Meeting note ready" notification appears with an **Open note** button. The Markdown notes are in `~/Meetings/YYYY/MM/`.

**Consent.** You are responsible for telling people you record. In many countries (for example Germany, §201 StGB) recording without consent is a crime. When you click Record, Wirenote copies your consent line to the clipboard so you can paste it into the call chat. Set it in **Settings**, or with `wirenote setup consent "…"`; turn it off with `wirenote setup consent ""`.

**Check the setup any time:** `wirenote doctor` (or **Settings › Setup check**).

## The meetings app

Open **Wirenote** from the app menu, or run `wirenote open`. It is a local web page with your past calls, grouped by day:

- **Notes tab:** summary, action items (tick them off, and the Markdown note updates too), decisions and open questions, with "jump to moment" links.
- **Transcript tab:** searchable, with timestamps that play the audio from there.
- **Ask:** questions about one call, answered from its transcript.
- **Record a meeting:** records without a call app (a meeting room, a phone on speaker). A call page opens where you can take notes while you talk.
- **Who said what:** the other people are told apart and named from the call ("This is Anna…"), the calendar invite, or a voice you taught it. Click a speaker to name them, tick "Recognize this voice" so later calls know them, or merge two speakers that are one person.
- **Your notes:** typed during or after the call. They shape the summary, and "Update summary" rebuilds it with them.
- **Follow-up email:** a recap from the notes, to copy or open in your mail app.
- **Action items:** everything agreed across all meetings, yours first.
- **Ask your meetings:** questions across all past calls, with links to the moments.
- **Settings:** summaries, transcription, the consent line, the Claude key, calendars, a vocabulary of names and terms, remembered voices, and the setup check.

Keys: `Ctrl K` or `/` to search, `Space` to play or pause, `←` / `→` to skip 15 s.

**Only you can open it.** The app listens on 127.0.0.1 only. The app menu and `wirenote open` log you in with a one-time link; other users on the same computer are refused, and other web pages cannot use it. If the page says "not logged in", open Wirenote from the app menu again.

## Summaries

**Local, the default.** [Ollama](https://ollama.com) runs on your computer and listens on 127.0.0.1; nothing leaves the laptop. The default model is `qwen3:4b`:

```sh
ollama pull qwen3:4b
```

To use another model, `ollama pull <model>` and set it in **Settings**. `contrib/ollama.service` is a systemd user unit for an Ollama installed without root in `~/.local/opt/ollama`.

**Claude (optional).** Summaries are better, but call data is sent to Anthropic; see [PRIVACY.md](../PRIVACY.md) for the exact list. Check your employer's policy before using it for work calls. Choose Claude in **Settings** (it asks you to confirm), and paste your API key there, or:

```sh
wirenote setup claude     # asks for the key (hidden), checks it, switches the backend
wirenote setup ollama     # back to local
```

The key is kept in `~/.config/wirenote/secrets.toml` (mode 600) and is never shown again. Every call shows a badge, "Local" or "Sent to Claude", so you can see where its summary was made.

A backend change applies to the next note. To redo an older note's summary:

```sh
wirenote reprocess 20261027-100000-chrome --summary
```

## Calendar (optional)

With a calendar, the prompt and the note use the event title, and the note lists the invited people. Add the calendar's secret ICS link in **Settings › Calendars**, or:

```sh
wirenote calendar add     # paste an ICS link (hidden input); checked, then stored in secrets.toml
wirenote calendar         # events in the next 24 h; → marks the ones a prompt would use now
wirenote calendar clear   # remove the stored links
```

- **Outlook:** Settings > Calendar > Shared calendars > Publish a calendar > ICS.
- **Google:** Settings > your calendar > Secret address in iCal format.

Only `https://` and `webcal://` links are accepted. **Keep the link private:** anyone with it can read your calendar. Wirenote shows and logs only the host name.

## Commands

```sh
wirenote open            # the meetings app (starts it if needed)
wirenote status          # daemon, current recording, queued jobs
wirenote stop            # stop the recording (also works if the notification was dismissed)
wirenote record          # answer the open prompt with Record
wirenote record --now    # record a meeting without a call app
wirenote jobs            # recent notes and failures
wirenote reprocess <id>  # transcribe again; add --summary to keep the transcript
wirenote doctor          # check every part of the setup; --fix makes your files private again
wirenote setup install   # install or update the services and the app-menu entry
wirenote setup uninstall # remove them; --purge also deletes recordings and settings
```

## Files

| Path | What |
| --- | --- |
| `~/Meetings/YYYY/MM/*.md` | notes; deleted only when you delete the call in the app, never by the audio clean-up |
| `~/.local/share/wirenote/recordings/<id>/` | `mic-000.wav`, `system-000.wav`, `manifest.json`, `transcript.json`, `summary.json`; the audio is deleted 30 days after the note (`audio_retention_days`) |
| `~/.config/wirenote/config.toml` | settings; every option is listed there, commented out |
| `~/.config/wirenote/apps.toml` | which apps count as calls |
| `~/.config/wirenote/secrets.toml` | Claude API key and calendar links (mode 600) |
| `~/.local/state/wirenote/detector.jsonl` | every event, without room names or speaker names |
| `~/.local/state/wirenote/jobs.db` | processing queue |

Everything Wirenote writes is private to your user (mode 600, folders 700).

## What to watch in the log

- `capture_start` shows the `mic_target` (`default`, or the device the app uses), and `capture_stop` shows the number of segments.
- `track_stalled`: a track got no audio for 5 s, and a notification says which one.
- `track_restarted` / `mic_moved`: a new segment started. That is fine; processing stitches the segments together.
- `job_end state=failed`: `wirenote jobs` shows the error. The audio is kept, and `wirenote reprocess <id>` retries.
- `recovered`: Wirenote was stopped during a recording; that recording was closed and queued at the next start.

## Updating

```sh
uv tool upgrade wirenote
wirenote setup install    # refreshes the services and restarts them
```

From a git checkout: `uv tool install --force .` in the repository, then `wirenote setup install`. Restarting during a call ends that recording; it is closed and queued like any other.
