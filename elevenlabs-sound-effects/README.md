<p align="center"><img src="../assets/elevenlabs-sound-effects.jpg" alt="ElevenLabs Sound Effects: official Hermes Agent plugin" width="100%"></p>

# ElevenLabs Sound Effects for Hermes Agent

> Part of the [Hermes Official Plugins](../README.md), Nous Research's official plugins for Hermes Agent.

Official [Hermes Agent](https://github.com/NousResearch/hermes-agent) plugin for the
[ElevenLabs Sound Effects API](https://elevenlabs.io/docs/capabilities/sound-effects). It gives the
agent one tool:

| Tool | What it does |
|---|---|
| `elevenlabs_sound_effect` | Turns a text description into an audio file (0.5 to 30 seconds, optionally a seamless loop) |

Ask for "the sound of a heavy wooden door creaking open in a stone hall", "a short retro coin pickup
blip" or "90s hip-hop drum loop, 90 BPM, loopable" and Hermes generates it and saves it. The tool hands
the agent a `MEDIA:` line for the file; when the agent puts it in its reply, the file is sent as an
attachment on Telegram, Discord, Slack and the other messaging platforms. MP3 (the default format)
plays inline on most chat apps; Ogg Opus and WAV can arrive as plain files (Telegram shows them that
way). On Discord, Hermes releases after v0.22.5 send MP3 and WAV as normal attachments that play
inline (v0.22.5 and earlier sent them as voice messages). In Hermes Desktop the
same line becomes an inline sound card (below). In the terminal you get the file path.

The plugin also ships:

| Part | What it does |
|---|---|
| `elevenlabs-sound-design` skill | How to write ElevenLabs prompts (from ElevenLabs' own prompting guide), make loops, pick a format for where the sound goes (game engine, video, UI, chat), build a sound pack, and deliver per surface |
| `sfx_tool.py` (in the skill) | Local post-processing with ffmpeg: trim silence, loudness-normalise, fades, loop-seam click check, convert to OGG/WAV/MP3, pack a named set into a folder + `manifest.json` + zip. Costs no ElevenLabs credits |
| Desktop sound card | Each generated sound shows in the chat as a card: waveform, play/pause, gapless loop audition, download in each format, reveal, and **Next** actions (Hermes Desktop with tool-media renderers; older Desktops use their built-in player) |
| Desktop sound library | ⌘K → **ElevenLabs: open sound library**: every sound this profile made, with its prompt and settings |

## Install

```bash
hermes plugins install elevenlabs-sound-effects
```

Or straight from this repo: `hermes plugins install NousResearch/hermes-official-plugins#elevenlabs-sound-effects`.

The installer asks for your ElevenLabs API key if `ELEVENLABS_API_KEY` is not set yet (create one
under **Profile → API keys** at <https://elevenlabs.io/app/settings/api-keys>). It is the same key
Hermes's ElevenLabs text-to-speech voice uses, so if you already have that set up there is nothing
to enter. The tool only appears to the agent once the key is set. Plugins are installed per profile:
use `hermes -p <profile> plugins install elevenlabs-sound-effects` for another profile.

The only Python dependency is `httpx`, which Hermes already ships.

## Settings

In Hermes Desktop: **Capabilities → Plugins**, then the gear icon on the plugin (the settings page is
titled with the package name, `elevenlabs-sound-effects`). Everywhere else, with `hermes config set`:

| Setting | Default | What it does |
|---|---|---|
| `api_key` | (unset) | Your ElevenLabs API key, stored in `.env` as `ELEVENLABS_API_KEY`: `hermes config set ELEVENLABS_API_KEY <key>` |
| `sound_model` | `eleven_text_to_sound_v2` | `eleven_text_to_sound_v2` or `eleven_text_to_sound_v3`: `hermes config set plugins.entries.elevenlabs-sound-effects.settings.sound_model eleven_text_to_sound_v3` |
| `output_format` | `mp3_44100_128` | `mp3_44100_128`, `mp3_44100_192`, `opus_48000_128`, `opus_48000_192`, `wav_44100`, `wav_48000`: `hermes config set plugins.entries.elevenlabs-sound-effects.settings.output_format wav_48000`. ElevenLabs documents 192 kbps MP3 for its Creator plan and above and 44.1 kHz WAV for Pro and above; if your plan refuses a format, the tool reports ElevenLabs' error. |
| `api_base_url` | `https://api.elevenlabs.io` | Change only for an ElevenLabs data-residency endpoint, e.g. `https://api.eu.residency.elevenlabs.io` |

The agent chooses the per-sound parameters itself: the description (at most 450 characters; longer
prompts are refused before anything is sent), the length (`duration_seconds`, 0.5 to 30; omit it and
ElevenLabs picks one), `loop`, and `prompt_influence` (0 to 1; higher follows the prompt more
literally, lower varies more).

The tool also takes an optional `model` per call (`eleven_text_to_sound_v2` or
`eleven_text_to_sound_v3`); without it the `sound_model` setting applies.

## Where files go

Each sound is saved to `<HERMES_HOME>/cache/generated/audio/sfx_<description>_<timestamp>_<id>.<ext>`
and never overwrites an earlier one. The Desktop card and library serve a file only while it has a single
link: after a backup tool hard-links your Hermes folder (rsnapshot, `cp -al`), those sounds play in
Desktop's built-in player instead of the card, and the library can't play or download them until the
extra links are removed (the files themselves are untouched). Ask the agent to copy it into your project if you want it
somewhere else.

Each generation also appends one line to `<HERMES_HOME>/plugin-data/<plugin namespace>/sounds.jsonl`
(the plugin's own data folder, per profile): the sound id, prompt, requested length, the length of
the audio ElevenLabs returned, loop, prompt influence, model, format, file name, size, the credits
ElevenLabs billed, and the chat it came from.
The Desktop library reads it. Nothing in it leaves your machine.

## Hermes Desktop

The Desktop half is opt-in, also after `hermes plugins install` from the CLI (which turns on only the
agent half): open **Capabilities → Plugins**, switch to the list view, and turn on
**Desktop: ElevenLabs Sound Effects** on the plugin's row. Without it Desktop still plays every sound,
in its built-in audio player.

**Sound card.** The tool always hands the agent a plain `MEDIA:` line, the one every surface plays.
Hermes Desktop works out which tool produced that file and, when it was `elevenlabs_sound_effect` and
the Desktop half is on, lets this plugin draw it as a sound card instead of the built-in audio player.
That needs a Hermes Desktop with tool-media renderers (on `main` since commit `567569267a`; first in the
next release after v0.22.5). On older Desktops, or with the Desktop half off, the same line plays in
Desktop's built-in player. The card also hands the sound back to the built-in player whenever it can't
play it itself: a sound made in another profile, a removed record, or a file the plugin won't serve
(see [Where files go](#where-files-go)). Nothing depends on which surface a chat was started on, and the
agent never writes a card line.

The card shows the prompt, length, loop, model, format and credits billed, a waveform you can click
to seek, Play/Pause, a **Loop** toggle that repeats the sound gaplessly so you can hear the seam,
**Download** (the original, or MP3 / WAV / OGG converted by ffmpeg on the machine Hermes runs on;
saved through Desktop's Save dialog on your computer), and **Reveal** in your file manager. **Next**
offers 3 variations, shorter, longer, make it loop and try the other model. Each row shows its cost
worked out from what ElevenLabs billed for this sound (an automatic-length sound is billed a flat
amount per generation, so its per-second price isn't known, and the row says so), and sends a chat message to the agent naming the tool and its
arguments. Nothing is billed until the agent runs the tool. On Desktop builds that tell plugins which
chat shows a card, the message goes to that chat, and nowhere else: if that chat is not on screen,
Desktop shows a notice instead. Older builds send it to the chat you last typed in.

**Sound library.** ⌘K → **ElevenLabs: open sound library** opens a tab listing every sound the tool
made in the active profile, MP3, Ogg Opus and WAV alike, with per-format counts and sizes, a format
filter and a prompt search. Each row plays, downloads, reveals, **Reuse prompt** (into the composer)
and **Open chat** (the conversation that made it). It reloads when a sound is generated, when you
switch profile or connection, and when you save the plugin's settings; it never polls.

**Remote Hermes.** The Desktop half runs in the Desktop app, so install the plugin on the Hermes
machine (agent half) and into the Desktop you use: **Capabilities → Plugins → Install from Git** with
`NousResearch/hermes-official-plugins/elevenlabs-sound-effects` and **Desktop UI** checked, then turn
on the Desktop switch. Without that, Desktop lists the half as unavailable for the remote backend and
sounds play in its built-in player. The files stay on the Hermes machine. Playback and downloads stream through Hermes, so they work the same. On a registered remote
connection Reveal is replaced by a note saying which host holds the file; where Desktop can't tell
(a per-profile remote override) the library names the host the backend reports and Reveal explains
when the file is not on this computer. Format conversion needs ffmpeg on that host; without it only
the original format is offered and the menu says why.

## Cost and privacy

Every call is a paid ElevenLabs generation billed to your account: ElevenLabs charges per second of
audio when a duration is set (about 10 credits per second, as billed to us) and a flat amount per
generation otherwise (about 50 credits, for 2 s and 3 s automatic clips alike; see their
[pricing](https://elevenlabs.io/pricing/api)). Loops can come back longer than requested (we measured
1 s → 1.5 s and 2 s → 2.25 s, billed 10 and 20 credits): the tool reports the real length as
`length_seconds` beside `requested_duration_seconds`, and the card shows both. The tool reports the `character-cost` header
ElevenLabs returns as `billed_characters`. Desktop format conversions and the processing script run
locally and cost nothing. What leaves your machine: the sound description and the
parameters above, sent to `api_base_url` with your API key. Nothing else from the conversation is
sent.

## Local processing script

The bundled skill runs `skills/elevenlabs-sound-design/scripts/sfx_tool.py` (Python standard library
plus `ffmpeg`/`ffprobe` on PATH) when a sound needs processing (`python` instead of `python3` where
only that exists, as on Windows):

```bash
python3 sfx_tool.py process in.wav --out out.ogg --trim --loudness -16 --fade-ms 10   # one-shot: trim + normalise + fade + convert
python3 sfx_tool.py process loop.mp3 --out loop.ogg                                   # plain conversion keeps a loop's edges
python3 sfx_tool.py loopfix loop.wav --out fixed.wav                                  # crossfade the seam (+ crossfade_level_db)
python3 sfx_tool.py loopcheck fixed.wav                                               # seamless | click | silent, worst channel (+ a note when the seam dips)
python3 sfx_tool.py pack a.mp3 b.mp3 --name "UI pack" --out-dir . --format ogg # folder + manifest.json + zip
```

Every run prints one JSON line (`{"ok": true, ...}` or `{"ok": false, "code": ..., "error": ...}`),
including when ffmpeg is missing, an input is not audio or an output can't be written. Codes:
`ffmpeg_missing`, `bad_input`, `bad_args`, `ffmpeg_failed`, `interrupted`, `internal`. `process`
reports the loudness it measured on the output (`output.integrated_lufs`, `output.true_peak_dbtp`):
`--loudness` aims at the target under a -1.5 dBTP ceiling, so a peaky clip can land below it.

## Development

```bash
cd elevenlabs-sound-effects
HERMES_AGENT_REPO=../../hermes-agent PYTHONPATH=../../hermes-agent python -m pytest -q
hermes plugins validate .
npm ci && npm test && npm run typecheck && npm run build   # Desktop half: src/*.tsx → desktop/plugin.js
```

The tests load the plugin through Hermes's real plugin discovery and dispatch the tool through the
real registry against a local stand-in for `POST /v1/sound-generation` that checks requests against
ElevenLabs' published OpenAPI schema; they never call ElevenLabs. `tests/test_library.py` reads the
provenance records back through `dashboard/plugin_api.py`; `tests/test_sfx_tool_script.py` runs the
script (its ffmpeg cases skip where ffmpeg is absent); `tests/sound.test.mjs` covers the card's Next
actions and helpers, and `tests/plugin.test.mjs` registers the real Desktop half against a stub SDK with
and without Desktop's tool-media renderer area.

The Desktop half is TypeScript in `src/`, bundled with esbuild into `desktop/plugin.js`, which is
committed (Hermes Desktop loads it as is). It imports only `@hermes/plugin-sdk` and `react`;
`sdk.d.ts` declares the SDK names it uses, and newer SDK calls are feature-detected. Rebuild and
commit `desktop/plugin.js` after any change under `src/`.

## License

MIT, see [LICENSE](LICENSE).
