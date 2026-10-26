# M1 design note: detector and prompt, no recording

Status: **approved 2026-10-25 and implemented 2026-10-26.** Name `wirenote`; Record is a dry run in M1.

Target: this machine only, which is KDE Plasma 6.6 on Wayland, PipeWire 1.6.8 with `pipewire-pulse`, and Python 3.12. Portability is deferred to M5.
Every threshold cites docs/dev/PLAN.md › Defaults. Every behaviour about streams cites docs/dev/m0-findings.md.

## Scope

**In scope:**
- a daemon that watches PipeWire
- the per-app state machine
- `apps.toml` and `config.toml`
- desktop notifications with Record and Ignore
- a JSON log line for every candidate, prompt, dismiss and state change
- sleep and wake handling
- a CLI with `run`, `status` and `dump-streams`
- a systemd user unit, so the week of daily use can run from login
- replay tests built from scrubbed M0 traces

**Out of scope:**
- any audio capture (M2). **Clicking Record in M1 is a dry run:** the app enters `Recording`, the "Recording" notification shows, and auto-stop is detected and logged, but no audio is written. This tests stop detection for a week before M2 relies on it.
- calendar matching (M4)
- the pactl fallback (M5)
- window titles: KDE Wayland has no standard API; a KWin script is possible later

## Layout

```
pyproject.toml              # uv; deps: dbus-fast; dev: pytest
src/wirenote/
  cli.py                    # wirenote run | status | dump-streams
  config/settings.py        # config.toml, defaults from docs/dev/PLAN.md › Defaults
  config/apps.py            # apps.toml rules, exact matching
  detector/pwdump.py        # pw-dump --monitor child, batch framing, restart with backoff
  detector/graph.py         # mirror; replace on update, delete on {"id", info:null}
  detector/streams.py       # mirror -> running mic streams -> observations per app
  detector/identity.py      # PID choice, lookup order, pid check
  detector/tracker.py       # pure state machine, clock passed in
  detector/daemon.py        # asyncio glue: batches + 1 s tick -> tracker -> actions
  notify/dbus.py            # org.freedesktop.Notifications, logind sleep signal
contrib/wirenote.service    # systemd --user unit
scripts/make_replay.py      # redaction pass: monitor.jsonl + streams.json -> tests/replay/*.json
tests/                      # parser, identity, tracker (fake clock), replay
```

The name `wirenote` is a placeholder until you confirm it. It also sets the config path `~/.config/wirenote/` and the state path `~/.local/state/wirenote/`.

## Key decisions

1. **The tracker is level-triggered.** After every pw-dump batch, and on a 1 s tick, the daemon rebuilds `{app_key: observation}` from the mirror and calls `tracker.update(now, observations)`.
   - The tick is PLAN's 1 s reconcile, at no extra cost.
   - If pw-dump dies, it restarts, and its first batch (the full graph) restores the state.
   - Single events are never trusted, because one open and close produced 16 batches (findings §2).
2. **What counts as a mic stream:** `Stream/Input/Audio`, state `running`, with at least one link from an `Audio/Source*` node. Excluded: our own PID, and any `node.name` starting with `wirenote-`.
   - Streams are keyed by `object.serial`, because PipeWire reuses node ids (findings §1).
   - State is kept **per app, not per stream**. An app is "on the mic" while **any** of its streams runs. This covers Chrome's second stream during mute, and the 80 ms reopen at Join.
3. **PID rule, from findings §3:**
   - if `client.api == "pipewire-pulse"`, use `application.process.id`; otherwise use `pipewire.sec.pid`
   - **pid check:** the PID's exe basename or comm must equal `application.process.binary`
   - if the check fails (for example a Flatpak sandbox PID that points at an unrelated host process), the PID-based steps 1, 3 and 4 are skipped, only steps 2 and 5 are used, and the evidence is logged as `pid_mismatch`
   - identity is cached per client serial
4. **Lookup order** (PLAN › Identifying the app): cgroup Flatpak/Snap ID → portal app ID → exe → cmdline[0] (split on spaces, because Chrome rewrites its argv) → up to 5 parents → binary → application name. The first exact rule match in `apps.toml` wins.
   - **Unmatched apps never prompt.** They are logged once per client, with the full evidence, as `unmatched`, so a rule can be added.
5. **Browser path first.** A rule can have `title_patterns`, regexes on `media.name`:
   - **Firefox:** a stream whose `media.name` matches `\| Jitsi Meet$` is treated as a meeting. It gets the 2 s dwell, and the prompt reads "Jitsi call in Firefox: <room>" (findings › Firefox).
   - **Chrome:** `media.name` is always `RecordStream`, so Chrome gets the browser dwell (5 s), and the prompt reads "Chrome is using your mic".
   - The Jitsi pre-join screen in Chrome (about 3.5 s of mic) plus Join counts as one continuous use, because of the dwell plus the 4 s grace.
6. **State machine:** the Mermaid diagram in docs/dev/PLAN.md, implemented exactly.
   - Timers, all from Defaults: dwell 2 s / 5 s, grace 4 s, minimum recording age 30 s, minimum app runtime 20 s, cooldown 60 s after the stream ends, 4 h cap.
   - **Only one recording at a time:** while one app records, other apps' candidates are logged but not prompted.
   - 5 dismisses of one app bring an "Always ignore this app" nudge. It writes to `~/.local/state/wirenote/ignored.json`, not to `apps.toml`.
7. **Notifications** through dbus-fast. Plasma reports `actions` and `persistence` as supported.
   - The prompt has Record and Ignore buttons and timeout 0, so it never expires on its own.
   - **Closing without an action counts as Ignore.** The exception is our own CloseNotification call when the app leaves the mic (close reason 3), which withdraws the prompt and does not count as a dismiss.
   - While "recording", a persistent notification shows with a Stop button.
8. **Sleep and wake:** listen for logind's `PrepareForSleep` signal.
   - On sleep: withdraw prompts and end any recording.
   - On resume: mark all current mic users as already seen, so nothing prompts.
9. **Logs** go to `~/.local/state/wirenote/detector.jsonl`, one JSON object per event, with the evidence. A short line also goes to stderr, which ends up in the journal.
   - `status` prints `status.json`, which the daemon rewrites on every change.
   - These logs replace the missing Discord and Rocket.Chat captures: the first real call in each app shows its identity and the pid check in the log.
10. **Redaction pass.** `scripts/make_replay.py` turns a fixture into a compact trace that keeps only what the detector reads:
    - timestamps, node and client serials, states, links, `client.api`, `application.process.binary`, and identity evidence
    - room names become `Room`, while the `| Jitsi Meet` suffix stays
    - hostnames, user names, cmdlines and device names are dropped
    - Only these traces go into git (`tests/replay/`); `tests/fixtures/` stays ignored.

## Seed `apps.toml`

| key | kind | match | source |
| --- | --- | --- | --- |
| chrome | browser | exe `chrome` | verified (jitsi-chrome) |
| firefox | browser | exe `firefox-bin`; title pattern `\| Jitsi Meet$` → meeting | verified (jitsi-firefox) |
| discord | meeting | flatpak `com.discordapp.Discord` | **unverified**; confirm from M1 logs |
| rocketchat | meeting | flatpak `chat.rocket.RocketChat` | **unverified**; confirm from M1 logs |

## Tests

- **Parser:** framing, and removal without `type`.
- **Graph:** mirror replace and delete, id reuse.
- **Identity:** a native client, a pulse client, Chrome's audio-service process, Firefox's main process, and a forced pid mismatch.
- **Tracker** with a fake clock: dwell met and not met, withdraw on leave, suppress plus cooldown, the stop guards (30 s age, 20 s runtime, 4 s grace), two streams in one app, a reopen within the grace, the 4 h cap, and resume from sleep.
- **Replay:** the four Jitsi traces must give exactly one prompt per call, and no prompt for the 47 ms blip or for an abandoned pre-join screen.

## Changes to docs/dev/PLAN.md, made together with M1

- The Detection section gets the PID rule (decision 3) and the per-app, serial-keyed tracking (decision 2).
- The browser path is described first, with `title_patterns`.
- The app list shrinks to Chrome, Firefox, Discord and Rocket.Chat.
- The machine is noted as KDE Wayland, so there are no window titles.

## Questions

1. Is the name `wirenote` OK?
2. Is M0 closed with Discord and Rocket.Chat unverified, to be confirmed from the M1 logs on the first real call?
3. Is the Record dry run in M1 OK (stop detection is logged, no audio)?
