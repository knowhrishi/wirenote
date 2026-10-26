# M1 runbook: one week with the detector

M1 exit (docs/dev/PLAN.md › Milestones): **one week of daily use with zero missed calls and at most one false prompt a day.** Tune the defaults from the logs.

> Since 2026-10-26, Record really records and writes a note (M2–M4). For recording, notes, summaries and the calendar, see `docs/usage.md`. The detector checks below still apply.

## Install (once)

```sh
cd <repo>
uv tool install --editable .                  # `wirenote` in ~/.local/bin; code edits apply directly
mkdir -p ~/.config/systemd/user
cp contrib/wirenote.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now wirenote
```

The first run writes `~/.config/wirenote/config.toml` (all defaults commented out) and `~/.config/wirenote/apps.toml`.

After editing either file, apply the change with `systemctl --user restart wirenote`.

## First check (5 minutes)

1. `wirenote status` should show `running (dry run)` and `no app on the mic`.
2. Join a meet.jit.si room in **Chrome**. After about 5 s, "Chrome is using your mic" appears with Record and Ignore buttons.
3. Click **Record**. A "Recording Chrome (dry run)" notification appears with a Stop button.
4. Leave the call. Within about 5 s, "Recording stopped (dry run): app left the mic" appears.
5. Repeat in **Firefox**. The prompt should read "Jitsi call in Firefox" with the room name, after about 2 s.

## Daily use

Just use calls as normal. Whenever a call happens:

| What you notice | Write it down as |
| --- | --- |
| A call with no prompt | **missed call** (app, time) |
| A prompt with no call (voice search, dictation, a mic test page…) | **false prompt** (app, time, what it was) |
| A prompt that came too late or too early, or a stop that was wrong | **timing** (what happened) |

Keep the notes in `docs/dev/m1-week.md`, one line each.

## Where to look

```sh
wirenote status                                # what the daemon sees right now
journalctl --user -u wirenote -f               # live, short lines
~/.local/state/wirenote/detector.jsonl         # every event with full evidence
```

These detector.jsonl events matter:

- `identified`: an app was recognised, with the rule step that matched (`by`) and the evidence. **Discord and Rocket.Chat are unverified.** After their first call, check that `app=discord` / `app=rocketchat` appears here, and paste the line into the chat.
- `unmatched`: something used the mic but no rule matched, so there was no prompt. If it was a call app, that is a missed call. The evidence shows what to put into apps.toml.
- `pid_mismatch`: the PID check failed. Expected for a sandboxed app that reports a sandbox PID; send me the line.
- `prompt`, `withdraw`, `click`, `notification_closed`, `record_start`, `record_stop`: the timeline of each call.

Quick summary of the day:

```sh
grep -E '"event": "(identified|unmatched|pid_mismatch|prompt|click|record_stop)"' \
  ~/.local/state/wirenote/detector.jsonl | tail -50
```

## Tuning

Every default is in `~/.config/wirenote/config.toml`. Uncomment a line to change it, then restart the service. The most likely changes:

- **Chrome prompts on short non-call mic use:** raise `dwell_browser_s` (default 5).
- **Prompts come too late in calls:** lower `dwell_browser_s` or `dwell_meeting_s`.
- **Dry-run recording stops while the call continues:** raise `grace_s` (default 4), and send me the log lines around it.

To stop being asked about one app, use "Always ignore" on the nudge (after 5 Ignores), or set its kind to `ignore` in apps.toml.

## Uninstall

```sh
systemctl --user disable --now wirenote
rm ~/.config/systemd/user/wirenote.service
uv tool uninstall wirenote
```
