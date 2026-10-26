# M0 findings: stream probe

Status: **closed on 2026-10-25 by decision ("enough testing, let's build").** Chrome and Firefox are verified with real Jitsi calls. **Discord and Rocket.Chat were not captured**, so the Flatpak PID question and the "two Flatpaks are distinct" exit check are still open. M1 has to confirm them from its own candidate logs (see docs/dev/m1-design.md).

Machine: PipeWire 1.6.8 (with `pipewire-pulse`), Linux 7.0, **KDE Plasma 6.6 on Wayland**. Clients carry `window.x11.display=:0`, but that only means XWayland is available; it is not an X11 session. The notification server (Plasma) supports `actions` and `persistence`.

**M0 exit (docs/dev/PLAN.md › Milestones › M0):** the Jitsi browser, the other browser, Discord and Rocket.Chat each resolve correctly, and the two Flatpaks resolve to distinct identities.

## 1. Per-app identity

_To fill from `tests/fixtures/<app>/streams.json` after each capture._

| App | Fixture | Install type | `client.api` | PID used / `pid_check` matches? | Resolved identity | Resolved by step | `media.name` on capture stream |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Jitsi in Chrome, daily (meet.jit.si) | `jitsi-chrome/`, `jitsi-chrome-marked2/` | deb | `pipewire-pulse` | `application.process.id` → Chrome **AudioService utility process** (`--type=utility --utility-sub-type=audio.mojom.AudioService`); exe `chrome` ✔ | `chrome` | 3 (exe) | `RecordStream`, no tab title |
| Jitsi in Firefox (meet.jit.si) | `jitsi-firefox/` | deb | `pipewire-pulse` | `application.process.id` → Firefox **main process** (`/usr/bin/firefox`, exe `firefox-bin`) ✔ | `firefox-bin` | 3 (exe) | **page title: `<Room Name> \| Jitsi Meet`** |
| Discord | `discord/` | Flatpak `com.discordapp.Discord` | | | | | |
| Rocket.Chat call | `rocketchat-call/` | Flatpak `chat.rocket.RocketChat` | | | | | |

Browsers: Chrome is the deb at `/opt/google/chrome`, Firefox the deb at `/usr/lib/firefox`.

Questions each capture must answer:

- **PID:** do the PID the probe used, the reported `application.process.binary`, and the real exe of that PID agree? Check `identity.pid_check` in the fixture.
- **`media.name` (browsers):** what text does the capture stream carry? If it holds the tab or page title, a Jitsi tab names itself, which would be a strong browser signal.
- **Rocket.Chat:** does the call open inside the app, or hand off to the browser?
- **Mute:** does muting close the stream, set it to `idle` or `suspended`, or leave it `running`?
- **After leaving:** does the stream stay open after leaving the call while the app is still open?
- **Stream count:** how many capture nodes does one call open, and are any reopened mid-call?

### Chrome details (`jitsi-chrome/`)

- **PID verified on a real pulse capture.** `pipewire.sec.pid` = 6148, the pipewire-pulse server, which is wrong. `application.process.id` = Chrome's audio service process, which matches the claimed binary `chrome`. Its parent (lookup step 4) is the Chrome browser process. Its cgroup unit is `app-com.google.Chrome-<pid>.scope`: the desktop launcher's systemd scope, which names the `.desktop` ID and makes a stable extra signal for non-sandboxed apps.
- **`media.name` gives no Jitsi signal in Chrome.** It is always `RecordStream`, and `node.name` and `application.name` are always `Google Chrome input`. No tab or page title appears anywhere on the stream or its client.
- **Each mic open is a new node.** In one capture, Chrome opened 4 capture nodes, all from the same client (id 75, the same audio service process). **PipeWire reuses node ids:** id 136 was two different streams (serial 8346, then 8353). M1 must key streams on `object.serial`, and track continuity per app (resolved identity), not per node.
- **Chrome also opens 1 to 3 `Stream/Output/Audio` "Playback" nodes around every mic open** (sound effects, remote audio). These are not mic streams, but they might later help confirm "in a call".
- **Lifecycle, from `jitsi-chrome-marked2`** (meet.jit.si: join unmuted, talk, mute, unmute, end call; timeline matched with your description because the markers were not captured):

  | rel. s | Mic streams | Meaning |
  | --- | --- | --- |
  | 5.26–5.33 | stream A (serial 8515) opens and goes `running` | join (the pre-join screen and the call used the same stream this time) |
  | 5.3–46.5 | A stays `running` for 41 s with **no state change at all** | in the call, **including while muted** |
  | 33.2–40.6 | a **second stream B** (serial 8528) runs in parallel for 7.4 s | the mute window; most likely Jitsi's talk-while-muted detection (inferred from timing, not confirmed) |
  | 46.48 | A removed; Chrome's call playback goes idle 0.1 s later | end call |

  **Mute does not close the mic stream** in Jitsi on Chrome, so "app gone > 4 s → stop" will not fire on mute.
- **In the first capture (`jitsi-chrome`)**, the pre-join screen held its own stream, closed it at Join, and opened a new one 80 ms later. The call ended at 26.3 s, and a stream briefly appeared again at 180 s. The 4 s grace covers reopens like this.
- **Consequences for M1:**
  - One app can hold **several capture streams at once** (A and B). The app counts as "on the mic" while any of its streams is `running`. A second stream must not cause a second prompt.
  - Streams come and go with new nodes and reused ids. Track state **per resolved app**, and use `object.serial` only to tell streams apart.
  - The Jitsi pre-join screen opens the mic before Join (about 3.5 s in the first capture). With the 5 s browser dwell and the 4 s grace, pre-join plus call counts as one continuous use. A pre-join screen that is abandoned after a few seconds does not prompt.
  - Leaving closes the stream immediately, so stop latency is grace (4 s) plus the stop checks.
- **Markers:** none recorded in either marked capture. Typed markers only work when the probe runs in a real terminal. The runbook now relies on a fixed timing schedule instead.

### Firefox details (`jitsi-firefox/`)

- **PID verified.** `application.process.id` is the Firefox main process, which matches the claimed binary `firefox-bin`. Firefox handles audio in its main process, so there is no helper to walk up from. Its parent is `systemd --user`, and its unit is `app-firefox@<uuid>.service`. The apps.toml rule must match the exe name **`firefox-bin`**; `firefox` is only the launcher name in cmdline.
- **`media.name` carries the page title.** The capture stream and the call audio streams are named `<Room> | Jitsi Meet`, which is the Jitsi room name plus `| Jitsi Meet`. Sound-effect streams opened before joining were named just `Jitsi Meet`. The name is set when the stream is created and did not change while it lived. **In Firefox, a Jitsi call identifies itself**, so Firefox needs no calendar or window title for Jitsi. Still open: whether self-hosted Jitsi servers keep the `| Jitsi Meet` suffix or is branded differently. This can only be checked in Firefox, because Chrome never exposes the title.
- **Privacy:** room names end up in `media.name`, in fixtures and in logs. The redaction pass must scrub `media.name` (and `node.description`) before any fixture is committed.
- **Lifecycle:** the capture held 2 calls. Firefox's PID changed between them (1309099, then 1313205), so Firefox was restarted in between.

  | rel. s | Mic stream | Meaning |
  | --- | --- | --- |
  | 12.4, 18.2 | (playback only, `Jitsi Meet`) | page load and pre-join sounds; **no mic stream on the pre-join screen**, unlike Chrome |
  | 22.33–190.93 | serial 8553 `running` for 168.6 s | call 1 |
  | 227.69–306.14 | serial 8581 `running` for **78.4 s**, no state change | call 2: 20 s unmuted, 20 s muted, 20 s unmuted, then end, matching the runbook schedule |

  - **Mute does not close the mic stream in Firefox either.** Unlike Chrome, there is no second parallel stream during mute.
  - Leaving removes the capture stream and the call audio in the same batch.

## 2. `pw-dump --monitor` output format

Confirmed live with `pw-record` as the test client (see `scripts/dump_streams.py --monitor`):

- **Framing.** stdout is a sequence of pretty-printed JSON arrays written back to back, with no separator and no newline-delimited JSON. Each top-level array ends with `]` alone on a line at column 0. The first array is the full graph (86 objects here); every later array is one batch of updates.
- **Add and change look the same.** Each element is a full object: `{"id", "type", "version", "permissions", "info": {..., "change-mask": [...], "props": {...}}}`. A change-only update such as `"change-mask": ["state"]` still carries the full `info.props`, so the mirror can **replace** objects instead of merging fields. Whether an object is new can only be told from the mirror (the id was not seen before).
- **Removal** is `{"id": N, "info": null}`, with **no `type` key**. The object's type and properties have to be taken from the mirror before deleting it.
- **Order seen for one capture stream**, open then close (times relative to start):
  - `+1.495` client added, then node added with `state: suspended`, `media.class: Stream/Input/Audio`
  - `+1.507` two links added (one per channel), `negotiating`, from `Audio/Source` ports `capture_FL` and `capture_FR`, then `active`
  - `+1.539` node `suspended → running`. The source node also turns `running`.
  - on close, each object comes in its **own one-element batch**: links removed, then ports, then the node, then the client
  - the source node goes `running → idle` afterwards (in a separate batch)
- **Batches are small and frequent.** One stream open plus close produced 16 update batches. Any "is this a new stream?" decision must run on the mirror state after a batch has been applied, not on single events.

## 3. Where identity lives in the graph

- The **stream node** carries no PID on the native path. On the pulse path it repeats `application.process.id`. Process properties live on the **Client** object, reached through the node's `client.id`.
- **`pipewire.sec.pid` is only trustworthy on the native path.** Two test clients recording from the same mic:

  | Client | `client.api` | `pipewire.sec.pid` | `application.process.id` | Real stream owner |
  | --- | --- | --- | --- | --- |
  | `pw-record` (native PipeWire) | _(unset)_ | 1279929 → `pw-cat` ✔ | 1279929 ✔ | `pw-cat` |
  | `parecord` (pulse protocol) | `pipewire-pulse` | **6148 → `pipewire-pulse` server ✘** | 1288290 → `pacat` ✔ | `pacat` |

  On the pulse path, the PipeWire client is created by the pipewire-pulse server, so the socket peer is the server. No other property gives a PID the app can't fake: the pulse client's full props were checked, and only the self-reported `application.process.*` keys name the app.
- **Consequence:** Chrome, Firefox and the Electron apps are expected to use the pulse protocol. For them, the PID comes from `application.process.id`, which libpulse fills from the app's own `getpid()`. That is honest for normal apps. Inside a Flatpak it may be a sandbox-namespace PID; **the Discord and Rocket.Chat captures will show this.**
- **Probe rule (now in `dump_streams.py`):** if `client.api == "pipewire-pulse"`, use `application.process.id`, otherwise `pipewire.sec.pid`. `pid_check` flags any mismatch between the PID's real exe or comm and the reported binary.
- `pipewire.access.portal.app_id` (lookup step 2) is also a client property, not a node property.
- **Source classification:** the link's `output-node-id` gives the source node. `media.class` `Audio/Source` means a capture device, and `Audio/Sink` (port names `monitor_*`) means a sink monitor.

## 4. Cannot be distinguished

Expected, and not an M0 failure: Jitsi, Meet and voice search in the same browser share one process identity. Separating them is the browser path's job in M1.

- **Firefox:** `media.name` holds the page title, so a regex such as `\| Jitsi Meet$` identifies a Jitsi call directly.
- **Chrome:** `media.name` is always `RecordStream`. Only the dwell and calendar matching (M4) can separate a Jitsi call from other mic use.

_Fill in after the captures. If Discord and Rocket.Chat resolve to the same identity, M0 fails._
