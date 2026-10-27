# Using Wirenote

## From call to note

1. A call starts (Jitsi in the browser, Discord, Rocket.Chat, or any app in `apps.toml`), and Wirenote shows a notification: **Record** or **Ignore**.
2. You click **Record**. Your mic ("Me") and the system audio (the other people) are recorded as two tracks, and a "Recording …" notification with **Stop** stays up.
3. You leave the call. The recording stops about 4 s later, and the call opens in the meetings app in your browser. To keep the browser closed, set `open_call_after_recording = false` in `config.toml`, then restart: `systemctl --user restart wirenote`.
4. The note is written in the background: transcript, speakers, summary, action items. The call page shows the progress and fills in when it is done. Transcription waits while another call is recording.
5. A "Meeting note ready" notification appears with an **Open note** button. The Markdown notes are in `~/Meetings/YYYY/MM/`.

**Consent.** You are responsible for telling people you record. In many countries (for example Germany, §201 StGB) recording without consent is a crime. When you click Record, Wirenote copies your consent line to the clipboard so you can paste it into the call chat. Set it in **Settings**, or with `wirenote setup consent "…"`; turn it off with `wirenote setup consent ""`.

**Check the setup any time:** `wirenote doctor` (or **Settings › Setup check**).

## The meetings app

Open **Wirenote** from the app menu, or run `wirenote open`. It is a local web page with your past calls, grouped by day:

- **Notes tab:** summary, action items (tick them off, and the Markdown note updates too), decisions and open questions, with "jump to moment" links.
- **Transcript tab:** searchable, with timestamps that play the audio from there.
- **Ask:** questions about one call, answered from its transcript.
- **Record a meeting:** records without a call app (a meeting room, a phone on speaker). A call page opens where you can take notes while you talk.
- **Who said what:** the other people are told apart and named from the call ("This is Anna…"), the calendar invite, or a voice you taught it. Click a speaker to name them, tick "Recognize this voice" so later calls know them, or merge two speakers that are one person. Someone who speaks less than about 8 seconds in the whole call is counted with the person who spoke just before or after.
- **Flag this moment:** press `F` or click "Flag this moment" while you record. The summary pays attention to what was said around each flag (30 seconds before and 10 after), and the note lists them under "Flagged moments". After a call, hover a transcript line and click its flag to add one. See "Flag a moment" below.
- **Your notes:** typed during or after the call. They shape the summary, and "Update summary" rebuilds it with them.
- **Follow-up email:** a recap from the notes, to copy or open in your mail app.
- **Action items:** everything agreed across all meetings, yours first.
- **Ask your meetings:** questions across all past calls, with links to the moments.
- **Import:** make a note from a recording made elsewhere (a phone, Zoom's own recording, a video). See "Import and export" below.
- **Settings:** summaries, transcription, the consent line, the Claude key, calendars, a vocabulary of names and terms, remembered voices, the setup check, and updates.
- **Updates:** click the version number at the bottom of the sidebar for what changed in each release. After an update, the app shows what is new once.

Keys: `Ctrl K` or `/` to search, `Space` to play or pause, `←` / `→` to skip 15 s, `F` to flag a moment while recording.

**Only you can open it.** The app listens on 127.0.0.1 only. The app menu and `wirenote open` log you in with a one-time link; other users on the same computer are refused, and other web pages cannot use it. If the page says "not logged in", open Wirenote from the app menu again.

## Flag a moment

Something important was just said? While a call records, press `F` in the meetings app, click **Flag this moment**, or run `wirenote flag`. Wirenote remembers the time. Pressing it twice within 2 seconds counts once.

- The summary gets the lines said from 30 seconds before to 10 seconds after each flag, with a request to cover them. Each flag gets its own share of the room, the words closest to it first, so every flag counts however many you set. In a call cut into parts, each part hears about the flags in it.
- The note gets a "Flagged moments" list: the line closest to each flag. When one person talks for a long time, the line is cut to those 40 seconds and stamped with the time they start. The call page shows the same list with links to the transcript.
- After the call, hover a line in the Transcript tab and click its flag to add one. This works for imported recordings too. "Update summary" then takes it into account.

`wirenote toggle` does one thing for the whole flow: it stops the recording; or, with a call prompt showing, records it; or else records a meeting without a call app. It fails if several prompts are showing: use `wirenote record`.

**Tip:** bind `wirenote flag` and `wirenote toggle` to a keyboard shortcut in your desktop's settings (System Settings › Keyboard › Shortcuts › Custom, in GNOME and KDE) to flag from any window, even while a call app has the focus.

Change the window with `flag_before_s` and `flag_after_s` in `config.toml`.

## Summaries

**Local, the default.** [Ollama](https://ollama.com) runs on your computer and listens on 127.0.0.1; nothing leaves the laptop. The default model is `qwen3:4b`:

```sh
ollama pull qwen3:4b
```

To use another model, `ollama pull <model>` and set it in **Settings**. `contrib/ollama.service` is a systemd user unit for an Ollama installed without root in `~/.local/opt/ollama`.

**Little free memory.** `qwen3:4b` needs about 3.5 GB of free memory. If there is not enough, Wirenote waits up to 5 minutes for it instead of letting the kernel stop Ollama or another program. If the summary still fails, or Ollama stops in the middle, you get the note with the transcript, and Wirenote tries the summary again a few minutes later, up to three times. `contrib/ollama.service` also halves the memory Ollama needs to hold the transcript while it reads it (`OLLAMA_KV_CACHE_TYPE=q8_0`). On a computer with 8 GB of memory or less, `qwen3:1.7b` needs about 2 GB; its summaries are a little weaker.

**Claude (optional).** Summaries are better, but call data is sent to Anthropic; see [PRIVACY.md](../PRIVACY.md) for the exact list. Check your employer's policy before using it for work calls. Choose Claude in **Settings** (it asks you to confirm), and paste your API key there, or:

```sh
wirenote setup claude     # asks for the key (hidden), checks it, switches the backend
wirenote setup ollama     # back to local
```

Instead of a key, you can log in to your Claude Console account with Anthropic's `ant` tool (`ant auth login`), or set `ANTHROPIC_API_KEY`; leave the key empty and Wirenote uses that. Usage is billed to your API account. A Claude Pro or Max subscription cannot be used: Anthropic does not allow other apps to use claude.ai logins.

**OpenRouter, OpenAI or another compatible API (optional).** [OpenRouter](https://openrouter.ai) gives you Claude, GPT, Gemini, Qwen and many more models with one key. Call data is sent to OpenRouter and the model's provider, as with Claude. Choose "OpenRouter, OpenAI or another compatible API" in **Settings**, set the address and model, and paste your key, or:

```sh
wirenote setup openai     # asks for the address, model and key (hidden); checks them with a one-word request
```

| Service | API address | Model, for example |
| --- | --- | --- |
| OpenRouter (the default) | `https://openrouter.ai/api/v1` | `anthropic/claude-sonnet-5.5`, `qwen/qwen3.8-flash` |
| OpenAI | `https://api.openai.com/v1` | `gpt-5.4-mini` |
| LM Studio on this computer | `http://127.0.0.1:1234/v1` | the model you loaded |
| llama.cpp `llama-server` on this computer | `http://127.0.0.1:8080/v1` | any name |

A server on your own computer counts as local, like Ollama: nothing leaves the computer, it needs no key, and Wirenote checks that it runs as your user. Any other address must start with `https://`.

Keys are kept in `~/.config/wirenote/secrets.toml` (mode 600) and are never shown again. Every call shows a badge, "Local", "Sent to Claude" or "Sent to openrouter.ai", so you can see where its summary was made.

A backend change applies to the next note. To redo an older note's summary:

```sh
wirenote reprocess 20261027-100000-chrome --summary
```

### Summary templates

A template tells the summary what kind of meeting it is and adds sections to the note. Five come with Wirenote:

| Template | Extra sections | Picked when the title has |
| --- | --- | --- |
| `general` | none | nothing else matched |
| `one-on-one` | Feedback, Topics for next time | `1:1`, `1-1`, `1on1`, `one on one` |
| `standup` | Updates by person, Blockers | `standup`, `stand-up`, `daily`, `scrum` |
| `interview` | What the candidate said about their experience, Questions the candidate asked, Follow-ups | `interview`, `screening`, `candidate` |
| `lecture` | Key concepts, Terms and definitions, Books, papers and links mentioned | `lecture`, `seminar`, `class`, `webinar` |

The interview template records only what was said. It does not rate, rank or judge the candidate.

Wirenote picks the first of these that applies:

1. The template you chose for the call: **Summary template…** in the call page's menu, or `wirenote reprocess ID --summary --template standup`. `--template auto` goes back to the automatic choice, so `auto` is not a name for a template file.
2. The template you chose for the call's calendar series ("Use it for every meeting in this calendar series" in the same dialog). To stop, choose **Automatic** in that dialog and tick "Stop using a template for this series".
3. A template with one of its words in the calendar event's title (for an import, in its title). Whole words only, any capitals: "Daily sync" matches `daily`, "Dailymotion review" does not. Your own templates are checked before the built-in ones, so a file with `titles = ["customer interview"]` wins over the built-in `interview`.
4. A template whose `apps` lists the call app, again your own templates first.
5. Your default, **Settings › Setup › Default summary template** (`default_template` in `config.toml`).
6. `general`.

The title only selects a template. It is never given to the model as an instruction. Redoing a summary with another template keeps the transcript; the new sections appear in the note as `## Section title` after the summary, and the note's front matter gets `template:`. `wirenote templates` lists every template, where it comes from and its match words. Calls made before this release keep their notes as they are.

**Your own templates** are TOML files in `~/.config/wirenote/templates/`. The file name, without `.toml`, is the template's id. A file named like a built-in (`standup.toml`) replaces it.

```toml
# ~/.config/wirenote/templates/sales-call.toml
name = "Sales call"
instructions = "A call with a customer or prospect. Keep to what each side said."
titles = ["demo", "discovery call"]     # whole words in the meeting title that pick this template
apps = ["zoom"]                         # optional: call apps that pick it

[[section]]
key = "needs"                           # a-z, 0-9 and _, starting with a letter, 32 characters at most
title = "What the customer needs"       # the heading in the note
hint = "needs and problems the customer described, one point each"   # tells the model what goes in

[[section]]
key = "objections"
title = "Objections"
hint = "concerns the customer raised, and how each was answered"
```

A template has at most 6 sections and 2000 characters of instructions. A file that breaks a rule is skipped, and `wirenote doctor` and `wirenote templates` say which one and why.

## People

**People** in the sidebar lists everyone who spoke in one of your calls or was invited to one. Open a person to see the calls you had with them, how long they spoke, their email addresses from the calendar, and the action items assigned to them. Tick items off there or in the call.

- A person is a name. Speakers count once you have named them (a "Speaker 2" has no page), and so does everyone on a calendar invitation. "Me" and "Others" never do. Wirenote does not know which invitee is you, so you appear as well if your name is on the invitations.
- The same person under two names, such as "Anna" and "Anna Berg"? Open one of them and click **Same person as…**. Their calls and action items come together. The recordings and notes are not changed. The choice is kept in `people.json` in `~/.local/state/wirenote/`; delete a line there to undo it.
- In a call, the name chips under the title open that person's page too.
- Nothing is sent anywhere: the pages are built from your own recordings each time you open them. Calls saved before this version have names but no email addresses, and show up the same way.

## Import and export

**Import a recording.** Click **Import** in the meetings app, or drop an audio or video file on its window. On the command line:

```sh
wirenote import ~/Downloads/team-sync.m4a --title "Team sync" --date "2026-10-27 14:00"
```

Wirenote converts the file with ffmpeg (`sudo apt install ffmpeg` if it is missing) and makes a note from it like from a call. Every voice in the file is told apart as Speaker 1, 2, …; none is taken for you, because there is no separate mic track. Without `--date`, the call is dated by the file's time. Your file is not changed, and no copy of it is kept: Wirenote keeps only the converted audio, which is deleted with the other recordings after `audio_retention_days`. Files up to 4 GB can be imported; only the first 4 hours are used.

**Export a call.** In the call page's menu (⋯):

- **Download as Word (.docx):** the summary, decisions, action items, your notes and the transcript, for Word, LibreOffice or Google Docs. On the command line: `wirenote export <id>`, or `-o ~/Documents` to choose where it goes.
- **Print or save as PDF:** opens your browser's print dialog with the notes and transcript. Choose "Save as PDF" as the printer to get a PDF.

## Speech engines

Wirenote picks the fastest engine that suits your computer and your calls, unless you choose one in **Settings › Setup**, or with `asr_engine` in `config.toml`:

- **Automatic**, the default. Parakeet if it knows your language (the call language, or your desktop's language when that is `auto`), you have no vocabulary of names and terms, and you have not picked a Whisper model. Otherwise Whisper. If Parakeet cannot be downloaded, Whisper writes the note.
- **Whisper** (`asr_engine = "whisper"`). 99 languages, and uses your vocabulary of names and terms. The model (about 1.6 GB) downloads on the first note.
- **Parakeet** (`asr_engine = "parakeet"`). Several times faster on a CPU, and good on English and 24 other European languages, which it detects by itself. It does not use the vocabulary. The model (about 670 MB) downloads on the first note.
- **whisper.cpp** (`asr_engine = "whisper.cpp"`). Runs a `whisper-cli` you build yourself, for example with Vulkan to use an AMD or Intel GPU. On Debian or Ubuntu:

  ```sh
  sudo apt install build-essential cmake git libvulkan-dev glslc
  git clone https://github.com/ggml-org/whisper.cpp
  cd whisper.cpp
  cmake -B build -DGGML_VULKAN=1
  cmake --build build -j --config Release --target whisper-cli
  ```

  Then set the path in `config.toml` and restart (`systemctl --user restart wirenote`):

  ```toml
  asr_engine = "whisper.cpp"
  whispercpp_bin = "/home/you/whisper.cpp/build/bin/whisper-cli"
  # whispercpp_model = "ggml-large-v3-turbo-q5_0.bin"   # or a full path to a ggml model
  ```

  The model downloads from Hugging Face on the first note. `wirenote doctor` tells you whether `whisper-cli` and the model are found.

Every engine runs on your computer. The note's footer says which one made the transcript, and `wirenote doctor` says which one Automatic picks.

**Live transcript.** Turn on **Live transcript during calls** in **Settings › Setup** (or `live_transcript = true` in `config.toml`) to see what is said while you record, under your notes on the call page. Lines appear a few seconds after they are said, as "Me" and "Others"; the note's transcript afterwards is made from the whole call and names the speakers. It uses two CPU threads during the call. It never downloads a model during a call. It uses Parakeet's model, which is usually there already from your notes; if no model is there yet, the call page says "the live model downloads after your next note", and the calls after that show the transcript.

**Transcribe one call again** with another engine, model or language: **Transcribe again…** in the call page's menu, or

```sh
wirenote reprocess 20261027-100000-chrome --engine parakeet
wirenote reprocess 20261027-100000-chrome --model large-v3 --language de
```

These choices apply to that call only, also when it is transcribed again later; your settings stay as they are.

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
wirenote flag            # flag this moment of the recording as important
wirenote toggle          # stop the recording, or start one (the open prompt, else a meeting)
wirenote jobs            # recent notes and failures
wirenote reprocess <id>  # transcribe again (--engine, --model, --language); --summary keeps the transcript; --template ID (or auto)
wirenote templates       # the summary templates and the words that pick them
wirenote import <file>   # make a note from an audio or video file (--title, --date)
wirenote export <id>     # save a call as a Word file (-o file or folder)
wirenote doctor          # check every part of the setup; --fix makes your files private again
wirenote setup claude    # summaries with Claude (your key or `ant auth login`)
wirenote setup openai    # summaries with OpenRouter, OpenAI or a compatible server
wirenote setup install   # install or update the services and the app-menu entry
wirenote setup uninstall # remove them; --purge also deletes recordings and settings
wirenote update          # install the newest version and restart; --check only checks
wirenote changelog       # what changed in this version; --since 0.1.0 for more
```

## Files

| Path | What |
| --- | --- |
| `~/Meetings/YYYY/MM/*.md` | notes; deleted only when you delete the call in the app, never by the audio clean-up |
| `~/.local/share/wirenote/recordings/<id>/` | `mic-000.wav`, `system-000.wav`, `manifest.json`, `transcript.json`, `summary.json`, `flags.json` (the moments you flagged); the audio is deleted 30 days after the note (`audio_retention_days`) |
| `~/.config/wirenote/config.toml` | settings; every option is listed there, commented out |
| `~/.config/wirenote/apps.toml` | which apps count as calls |
| `~/.config/wirenote/templates/*.toml` | your own summary templates |
| `~/.config/wirenote/secrets.toml` | Claude API key and calendar links (mode 600) |
| `~/.local/state/wirenote/detector.jsonl` | every event, without room names or speaker names |
| `~/.local/state/wirenote/jobs.db` | processing queue |
| `~/.local/state/wirenote/series.json` | the summary template you kept for each calendar series |
| `~/.local/state/wirenote/people.json` | which names you put together as one person |

Everything Wirenote writes is private to your user (mode 600, folders 700).

## What to watch in the log

- `capture_start` shows the `mic_target` (`default`, or the device the app uses), and `capture_stop` shows the number of segments.
- `track_stalled`: a track got no audio for 5 s, and a notification says which one.
- `track_restarted` / `mic_moved`: a new segment started. That is fine; processing stitches the segments together.
- `job_end state=failed`: `wirenote jobs` shows the error. The audio is kept, and `wirenote reprocess <id>` retries.
- `recovered`: Wirenote was stopped during a recording; that recording was closed and queued at the next start.

## Updating

```sh
wirenote update
```

This finds the newest version on PyPI, installs it the way you installed Wirenote (uv or pipx), restarts the background services, runs the setup check and shows what is new. In the meetings app, **Update** in the banner or on the Updates page does the same.

- **During a call** it refuses, because a restart would end the recording. `wirenote update --wait` updates as soon as the call ends; the app's Update button always waits.
- **Only check:** `wirenote update --check`.
- **Go back** to an earlier version: `wirenote update --to 0.2.0`. An older version may not know settings that a newer one added.
- **Know when a version is out:** turn on **Settings › Updates › Check for new versions once a day** (`update_check = true` in `config.toml`). Wirenote then asks pypi.org once a day, sends one notification per new version, never during a call, and shows a banner in the app. It is off by default; see [PRIVACY.md](../PRIVACY.md).

By hand, the same as `wirenote update`: `uv tool install --upgrade wirenote`, then `wirenote setup install`. From a git checkout: `git pull` and `uv tool install --force .` in the repository, then `wirenote setup install`; `wirenote update` refuses checkouts.
