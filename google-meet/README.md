<p align="center"><img src="../assets/google-meet.jpg" alt="Google Meet: official Hermes Agent plugin" width="100%"></p>

# google-meet plugin

> Part of the [Hermes Official Plugins](../README.md), Nous Research's official plugins for Hermes Agent. Formerly bundled with Hermes core as `google_meet`; existing users are migrated automatically.

Let the hermes agent join a Google Meet call, transcribe it, optionally speak
in it, and do the followup work afterwards.

## What ships

| Version | What | Status |
|---|---|---|
| v1 | Transcribe-only: Playwright joins Meet, scrapes captions to transcript file | ✓ ships by default |
| v2 | Realtime speech out: bot speaks in-call via OpenAI Realtime + BlackHole/PulseAudio null-sink; input stays caption-derived | opt in with `mode='realtime'` (local bot only, see [Known limitations](#known-limitations)) |
| v3 | Remote node host: run the bot on a different machine than the gateway | opt in with `node='<name>'` (transcribe only) |

> **Known limitation:** a bot started by the agent's `meet_join` is stopped when that agent turn ends, so
> multi-turn meetings driven by the agent do not work yet
> ([#518](https://github.com/NousResearch/hermes-official-plugins/issues/518)). See
> [Known limitations](#known-limitations).

## Architecture

```
┌─ gateway (Linux box, where hermes runs) ────────────────────────────┐
│                                                                      │
│   agent → meet_join(url, node='my-mac')                              │
│         │                                                            │
│         └─ NodeClient ─── ws ────┐                                   │
│                                  │                                   │
└──────────────────────────────────┼───────────────────────────────────┘
                                   │ ws (shared token, no TLS)
                                   ▼
┌─ node host (user's Mac, signed-in Chrome lives here) ───────────────┐
│                                                                      │
│   NodeServer (from `hermes meet node run`)                           │
│     │                                                                │
│     ├─ start_bot → process_manager.start() → spawns meet_bot         │
│     │                                                                │
│     └─ meet_bot (Playwright)                                         │
│        ├─ Chromium → meet.google.com                                 │
│        ├─ caption scraper → transcript.txt                           │
│        └─ (realtime, local bot only) RealtimeSpeaker thread          │
│             ↓                                                        │
│           OpenAI Realtime WS → speaker.pcm                           │
│             ↓                                                        │
│           paplay → null-sink ← Chrome fake mic                       │
│                                                                      │
└──────────────────────────────────────────────────────────────────────┘
```

Without v3: the whole right column runs on the gateway machine.
Without v2: the "realtime" path is skipped; transcribe runs alone.

## Files

| Path | Purpose |
|---|---|
| `plugin.yaml` | manifest |
| `__init__.py` | `register(ctx)` — registers 5 tools + `on_session_end` hook + `hermes meet` CLI |
| `meet_bot.py` | Playwright bot subprocess (standalone; `process_manager` spawns it with the plugin directory mounted as a package) |
| `process_manager.py` | local bot lifecycle + `enqueue_say` |
| `tools.py` | agent-facing tools + node-routing helper |
| `cli.py` | `hermes meet setup / auth / join / status / transcript / say / stop / node ...` |
| `audio_bridge.py` | v2: PulseAudio null-sink (Linux) + BlackHole probe (macOS) |
| `realtime/openai_client.py` | v2: `RealtimeSession` + `RealtimeSpeaker` (file-queue → OpenAI Realtime WS → PCM) |
| `node/protocol.py` | v3: message envelope + validation |
| `node/registry.py` | v3: `$HERMES_HOME/workspace/meetings/nodes.json` |
| `node/server.py` | v3: `NodeServer` (runs on host machine) |
| `node/client.py` | v3: `NodeClient` (used by tool handlers + CLI on gateway) |
| `node/cli.py` | v3: `hermes meet node {run,list,approve,remove,status,ping}` |
| `SKILL.md` | agent usage guide |

## Local quick start

```bash
hermes plugins install google-meet
hermes meet install                                      # Chromium
hermes meet setup                                        # preflight
hermes meet auth                                         # optional
hermes meet join https://meet.google.com/abc-defg-hij    # transcribe
```

## Realtime mode

Linux (preferred, most automated):
```bash
hermes meet install --realtime                     # installs pulseaudio-utils
echo 'OPENAI_API_KEY=sk-...' >> ~/.hermes/.env
hermes meet join https://meet.google.com/abc-defg-hij --mode realtime
# then from the agent or CLI:
hermes meet say "Good morning everyone, I'm the note-taker bot."
```

macOS:
```bash
hermes meet install --realtime     # runs: brew install blackhole-2ch ffmpeg
# then — manually! — open System Settings → Sound → Input → BlackHole 2ch
echo 'OPENAI_API_KEY=sk-...' >> ~/.hermes/.env
hermes meet join https://meet.google.com/abc-defg-hij --mode realtime
```

On macOS, hermes will **not** switch your system audio input automatically — the
user has to do it. This is deliberate: switching default input on a whim would
be a surprising side effect.

Realtime mode is **speak-only**: `speaker.pcm` is streamed into the virtual mic by a
stdin-fed `paplay` / `ffmpeg` pump that follows the file as Realtime appends audio.
Incoming speech is still the caption scrape (v1) — meeting audio is never sent to the
Realtime session, so there is no barge-in on raw audio and no STT billing. After
admission the bot unmutes itself if Meet seated it muted; `status.json` / `hermes meet
status` report the result as `micState` (`unmuted`, `unmuted_clicked`, `unknown`).

## Remote node host

On the node machine (e.g. user's Mac with a signed-in Chrome):
```bash
hermes plugins install google-meet
hermes meet install
hermes meet node run --display-name my-mac --host 0.0.0.0 --port 18789
# prints the bearer token on first run; copy it
```

On the gateway:
```bash
hermes meet node approve my-mac ws://<mac-ip>:18789 <token>
hermes meet node ping my-mac
# now any meet_* tool call accepts node='my-mac' (or 'auto')
```

## What leaves your machine

Read off the code at this commit (paths are in this directory):

| What | Goes to | Where in the code |
|---|---|---|
| The Meet URL, the guest name, and the bot's camera and microphone | Google Meet (meet.google.com), through Playwright's Chromium. In transcribe mode the camera and microphone are Chromium's fake devices (a test pattern and a tone, [#518](https://github.com/NousResearch/hermes-official-plugins/issues/518)); in realtime mode the microphone is the virtual device carrying the agent's speech | `meet_bot.py` `run_bot` (launch, `page.goto`, fake-device flags), `_join`, `_ensure_mic_on` |
| Your Google session cookies, if you ran `hermes meet auth` | Google, as the browser's signed-in session. Only `hermes meet join` on the CLI uses them; the agent's `meet_join` tool joins as a guest | `cli.py` `_cmd_auth`, `_cmd_join`; `tools.py` `handle_meet_join` (passes no `auth_state`) |
| Captions | Nowhere by themselves. The bot reads Meet's live captions in the page and appends them to `transcript.txt` in the profile's `workspace/meetings/<meeting-id>/`. They leave the machine only when the agent reads them with `meet_transcript`: the tool result goes to your configured model provider like any tool output | `meet_bot.py` `_CAPTION_OBSERVER_JS`, `_BotState.record_caption`; `process_manager.py` `transcript` |
| Realtime mode: the session instructions, the voice name and each `meet_say` text | OpenAI, over `wss://api.openai.com/v1/realtime`, with `HERMES_MEET_REALTIME_KEY` (or `OPENAI_API_KEY`). OpenAI sends back synthesized speech (PCM), which is written to `speaker.pcm` and played into the virtual microphone, so it reaches the meeting through Google Meet | `realtime/openai_client.py` `RealtimeSession.connect` (`session.update`), `speak` (`conversation.item.create` with `input_text`, `response.create`); `meet_bot.py` `_start_pcm_pump` |
| Meeting audio | Never sent to OpenAI or anywhere else by the plugin: no audio is read from the call. Barge-in sends only `response.cancel`, triggered by a caption | `realtime/openai_client.py` `cancel_response` |
| Remote node mode: requests (Meet URL, guest name, duration, `meet_say` text) and replies (status, transcript lines) | The node you approved, over a plain WebSocket (`ws://`, no TLS built in) with the shared token in every request. The node then does all of the above from its own machine | `node/client.py`, `node/protocol.py`, `node/server.py` |
| Installation | `hermes meet install` downloads Chromium through Hermes and, with `--realtime`, asks before running `apt-get` or `brew` for audio tools | `cli.py` `_cmd_install` |

## Known limitations

- **The agent's bot does not outlive the turn that started it.** Hermes runs the `on_session_end` hook at
  the end of every agent turn, and this plugin's hook stops a running bot, so a meeting joined with the
  `meet_join` tool is left as soon as that turn finishes, and a bot started with `hermes meet join` is
  stopped by the next agent turn in that profile. The transcript written so far is kept. Tracked in
  [#518](https://github.com/NousResearch/hermes-official-plugins/issues/518) with the other open
  defects (waiting room reported as `inCall`, no exit when the call ends, English-only UI matching).
- **Remote nodes run transcribe mode only.** The node server passes `url`, `guest_name`, `duration`,
  `headed`, `auth_state`, `session_id` and `out_dir` to the bot, not `mode`, so `mode='realtime'` with
  `node=` starts a listen-only bot and `meet_say` text is queued but never spoken
  (`node/server.py` `_START_BOT_KEYS`).
- **No live Meet call has been run against this plugin in CI or in the move to this repo;** the tests
  use mocked Playwright pages, a fake Realtime socket and in-process node servers.

## Safety

- URL gate: only `https://meet.google.com/abc-defg-hij`, `/new`, `/lookup/<id>`.
- No calendar scanning, no auto-dial, no auto-consent announcement.
- Node server uses bearer-token auth; no key exchange, no TLS termination
  built in — run it on a LAN or behind a reverse proxy you trust.
- One active meeting per (gateway, node) pair. A second `meet_join` leaves the first.
- `meet_say` refuses unless the active meeting was started with `mode='realtime'`.

## Out of scope

- **Calendar scanning** — deliberately not implemented. Join URLs must be explicit.
- **Multi-tenant node sharing** — a node serves one gateway at a time.
- **Windows** — audio bridging isn't tested; `register()` no-ops on Windows.
- **System audio input switching on macOS** — user responsibility, not the bot's.
- **Meeting-audio ingestion into Realtime** — input is caption-derived; true bidirectional audio is a separate feature.
