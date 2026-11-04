---
name: elevenlabs-sound-design
description: Write ElevenLabs sound effect prompts, loops and packs.
version: 1.2.0
author: Teknium (teknium1), Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [audio, sound-effects, elevenlabs, game-audio, foley]
    related_skills: []
---

# ElevenLabs Sound Design

Turns a request for a sound ("a coin pickup for my platformer", "rain ambience I can loop", "a UI
sound pack") into good `elevenlabs_sound_effect` calls, picks the file format for where the sound is
going, post-processes it locally when the destination needs it, and delivers it on the user's
surface. Every generation is billed to the user's ElevenLabs account; local processing is free.

## When to Use

- The user asks for a sound effect, foley, ambience, UI sound, stinger, whoosh, drum loop or a set of them.
- The user wants a generated sound trimmed, normalised, made mono, looped cleanly, converted (ogg/wav/mp3) or packed.
- The user is building game audio: positional one-shots, variation sets for an engine's random picker, loops.
- Don't use for: speech or voices (Hermes's text-to-speech), full songs (ElevenLabs' Music API, not this plugin).

## Prerequisites

- The `elevenlabs_sound_effect` tool (this plugin, with `ELEVENLABS_API_KEY` set). A restricted
  ElevenLabs key needs the **Sound Effects** permission. When ElevenLabs refuses a key over a missing
  permission, the tool passes its error through: tell the user to enable that permission on the key.
- Optional, for post-processing: `ffmpeg` and `ffprobe` on PATH of the machine Hermes runs on. Check
  with `terminal(command="ffmpeg -version")`; without them skip processing and deliver the original.

## Tool arguments (exact names from the tool schema)

| Argument | Values | Notes |
|---|---|---|
| `prompt` | text, at most 450 characters | Required. What the sound is. The tool refuses longer prompts before any request. |
| `duration_seconds` | 0.5-30 | Omit to let ElevenLabs choose (billed a flat ~50 credits). Set it for anything that must fit a slot (UI blip, cut); billed ~10 credits per second of the request. When the user names a length, pass it here, not in the prompt. Loops can come back longer than asked: the result's `length_seconds` is the real length. |
| `loop` | true/false | Seamless loop (ambience, beds, engine hums). |
| `prompt_influence` | 0-1 (default 0.3) | Higher = more literal, lower = more varied. |
| `model` | `eleven_text_to_sound_v2`, `eleven_text_to_sound_v3` | Omit to use the user's setting. |

The format is a user setting (`output_format`: mp3, Ogg Opus or 48/44.1 kHz WAV), not an argument;
convert locally (below) when one sound needs another format. Never invent other arguments.

## Writing the prompt (ElevenLabs' prompting guide)

Source: ElevenLabs, "Sound effects → Prompting guide",
https://elevenlabs.io/docs/capabilities/sound-effects

1. Simple effects: a clear, concise description of source + action + surface/space:
   "Glass shattering on concrete", "Heavy wooden door creaking open", "Thunder rumbling in the distance".
2. Sequences: describe the order of events with "then"/"followed by":
   "Footsteps on gravel, then a metallic door opens".
3. Musical elements: genre, tempo, key: "90s hip-hop drum loop, 90 BPM", "Vintage brass stabs in F minor".
4. Audio terms the model understands: impact, whoosh, ambience, one-shot, loop, stem, braam, glitch, drone.
5. Add what you know about the destination: "short, dry, no reverb" for UI; "distant, wide stereo"
   for ambience; "single hit, no tail" for one-shots that a game engine will layer.

Keep one sound per call. For a set, make one call per sound with a consistent style phrase
("soft, rounded, wooden, short decay") repeated in every prompt so the pack sounds like one family.

## Loops

- Set `loop: true` and a `duration_seconds` long enough to avoid obvious repetition (6-20 s for
  ambience, a whole number of bars for music: at 90 BPM one 4/4 bar is 2.667 s).
- Loops often come back longer than requested (measured: 1 s → 1.5 s, 2 s → 2.25 s; 4-6 s exact), and
  the bill follows the request. The result's `length_seconds` is the length of the file and
  `length_note` says when it differs; quote that, never the request.
- Music on a tempo grid: request `duration_seconds` = the bar length (one 4/4 bar at 90 BPM is
  2.667 s), then read `length_seconds`. When it differs, don't cut the loop to the bar: the returned
  length is what repeats seamlessly, and `loopfix` would blend audio that was not written to overlap.
  Prefer giving the user the tempo the file really has (BPM = 60 x beats / `length_seconds`). If they
  need the bar length, time-stretch it, `ffmpeg -i IN -af atempo=<length_seconds / bar> OUT.wav`
  (keeps the pitch), then `probe` and `loopcheck` OUT and report both: measured on real 2.25 s loops
  it lands within ~20 ms of the bar, not exactly on it, and can add a click at the seam (`loopfix` it).
- Verify the seam before calling it seamless: run `loopcheck` (below) on the file you deliver and report
  its verdict (`seamless`, `click`, or `silent` for a file with nothing audible in it). When it adds a
  `dip_note`, pass that on as a thing to listen for; it is not a failure and never a reason to
  regenerate on its own.
- A loop flagged `click`: run `loopfix` (crossfades the end into the start, 20 ms by default; it reduces
  the seam jump), then `loopcheck` the result and report `loopfix`'s `crossfade_level_db`. Never "fix" a loop with `process --fade-ms`: fades dip to silence at every
  repeat.
- Never pass `--trim` or `--fade-ms` to `process` for a loop; plain conversion keeps the edges.
- Lossy OGG/MP3 can add a small edge artifact a WAV does not have: if the converted file checks
  `click` while the WAV is `seamless`, deliver the WAV.

## Format by destination

| Destination | Deliver | Why |
|---|---|---|
| Godot, Unity, Unreal, web games | OGG (Vorbis) for music/ambience loops, WAV for short one-shots; mono for positional sounds (`process --mono`), stereo for ambience, music and UI | engines import both; OGG keeps loops small, WAV avoids decode cost and MP3 padding on one-shots. A sound placed in the world (footstep, hit, door, creature) is a point source the engine attenuates and pans, so it ships mono by convention (Unity has a Force To Mono import option for it); a stereo bed stays stereo |
| Video editing (Premiere, Resolve, Final Cut) | WAV 48 kHz | the video standard; no generation loss |
| App/UI sounds | WAV (or OGG on Android/web) | short, sample-accurate start |
| Chat (Telegram, Discord, Slack, WhatsApp) | MP3 | plays inline on most chat apps; Ogg Opus and WAV may arrive as plain file attachments (Telegram does this). Discord: Hermes after v0.22.5 sends MP3/WAV as normal attachments that play inline; v0.22.5 and earlier sent them as voice messages |

MP3 adds a short silence at the start (encoder padding), so never use MP3 for gapless loops in an
engine; convert loops to OGG or WAV.

## Game sound sets (variations)

Sounds a game repeats (footsteps, impacts, sword swings, pickups) need several takes that the engine
picks between at random, or the repetition is obvious. ElevenLabs has no seed or batch option: each
take is its own call.

1. Brief: the sound, its surface or material, and how many takes (4-8 for footsteps and impacts,
   3-4 for rarer sounds). Quote the cost: takes x the per-sound bill.
2. Generate each take with the SAME prompt, the same `duration_seconds`, and `prompt_influence`
   around 0.2-0.3 so the takes differ. Add the destination to the prompt ("single hit, no tail,
   dry"): the engine adds the room.
3. Level every take to one target, and downmix positional sounds:
   `process TAKE --out TAKE_lvl.wav --trim --loudness -18 --mono` (drop `--mono` for UI and
   ambience; -14 to -18 LUFS suits games). Takes at different loudness stand out when the engine
   picks at random.
4. `pack TAKE_lvl... --name "Footstep Gravel" --out-dir DIR --variations`: names the files
   `footstep_gravel_01.wav`, `_02`... (one base name, numbered, which is how engine random containers
   expect takes), measures each take, and reports `loudness_spread_lu`. Over 1 LU the result carries a
   `loudness_note`: re-level the outliers and pack again. Report the spread.
5. In the engine (say so when you deliver; you can't do it from here unless an engine tool is
   connected): import the files, then put the takes in its random picker: a Sound Cue Random node or a
   MetaSound in Unreal, an Audio Random Container in Unity 6, an `AudioStreamRandomizer` in Godot 4.
   Loops need the engine's loop flag set on import (Unreal: the Sound Wave's Looping option).

## Local post-processing script

`scripts/sfx_tool.py` (stdlib Python + ffmpeg/ffprobe). Every run prints exactly one JSON line,
`{"ok": true, ...}` or `{"ok": false, "error": "...", "code": "..."}`, and exits 0 or 1; read that
line, never guess. Codes: `ffmpeg_missing` (install ffmpeg), `bad_input` (input missing, not audio,
too short or too long), `bad_args` (a bad option, or an output that can't be written: the error names
it), `ffmpeg_failed`, `interrupted` (stopped by Ctrl-C), `internal` (a bug in the script; report it).
Run it with `terminal` (use `python` if `python3` is missing, as on Windows):

```
python3 "${HERMES_SKILL_DIR}/scripts/sfx_tool.py" probe IN
python3 "${HERMES_SKILL_DIR}/scripts/sfx_tool.py" process IN --out OUT.ogg [--mono] [--trim] [--loudness -16] [--fade-ms 10]
python3 "${HERMES_SKILL_DIR}/scripts/sfx_tool.py" loopfix IN --out OUT.wav [--crossfade-ms 20]
python3 "${HERMES_SKILL_DIR}/scripts/sfx_tool.py" loopcheck IN
python3 "${HERMES_SKILL_DIR}/scripts/sfx_tool.py" pack --name NAME --out-dir DIR FILE [FILE ...] [--format ogg|wav|mp3] [--prompts prompts.json] [--variations]
```

A file name that starts with `-` reads as an option: pass it as `./-name.mp3` (or put `--` before
the file names).

- `process`: converts to the format implied by `OUT`'s extension (`.ogg` Vorbis, `.wav` 16-bit PCM,
  `.mp3`, `.opus`). `--mono` downmixes to one channel before anything else, so the loudness is set
  and measured on the mono file that ships (fine on loops too). Options for one-shots: `--trim` removes leading/trailing silence, `--loudness`
  aims at that many LUFS (EBU R128; -16 for chat/web, -23 for broadcast video, -14 to -18 for games),
  capped by a -1.5 dBTP true-peak ceiling, `--fade-ms` adds in/out fades (refused when the two fades
  would cover the whole clip). Without options it only converts. It never overwrites its input. Its
  JSON `output` carries the measured `integrated_lufs` and `true_peak_dbtp`: report the measured
  loudness, not the target (`applied.loudness_note` says when they differ by more than 1 LU; a clip under 0.4 s gets one plain gain, measured looped; clips of 0.4–3 s go through loudnorm and land within
  about 0.5 LU, and the reported measurement is the real result).
- `loopfix`: crossfade of the loop's last N ms into its first N ms, gain-matched to how alike the two
  segments are (`seam_correlation`); the result is N ms shorter and its seam jump is gone. An
  anti-correlated seam (`seam_correlation` well below 0) or an attack inside the first N ms can leave
  a dip inside the crossfade: `crossfade_level_db` measures it (near 0 is clean; a few dB below is
  audible on a steady hum). It also reports `clipped_samples` (should be 0).
- `loopcheck`: checks every channel and reports the worst: verdict `seamless`, `click` (the last and
  first samples jump, or a sound is cut off at the seam), or `silent` (nothing above about -70 dBFS),
  plus `seam_dip_db` and, when the level drops sharply at the seam, a `dip_note` (a fade or a cut
  inside a rest; the samples can't tell which). A sound starting just after a silent seam is an
  onset, not a click. Report what it measured.
- `pack`: copies (and converts with `--format`) the files into `DIR/NAME/` with tidy ASCII names
  (`01-name.ext`; a file without an extension gets the one its contents imply), writes
  `manifest.json` (file, source, duration, sample rate, channels, codec, and the prompt for each file
  when `--prompts` maps file names to prompts) and `DIR/NAME.zip`. `--force` replaces an earlier pack
  of the same name completely. `--variations` treats the files as takes of ONE sound: it names them
  `name_01.ext`, `name_02.ext`..., adds each take's `integrated_lufs`, and reports `variation_set`,
  `loudness_spread_lu` and, over 1 LU, a `loudness_note` (in the JSON line and `manifest.json`).

## Procedure

1. Settle the brief before spending: what the sound is, where it goes (engine, video, UI, chat), and
   the length. Ask only for what you can't infer. Completion: you can fill `prompt`, `duration_seconds`
   and `loop`.
2. State the cost before a set or a long sound: with `duration_seconds` set ElevenLabs bills per
   generated second (~10 credits/s measured); without it a flat amount per sound (~50 credits measured).
   The tool's `billed_characters` reports the real charge: read it from the first result and use it for
   the rest of a set. Completion: the user knows the size of the spend.
3. Call `elevenlabs_sound_effect`. For variations, call it again with the same prompt (and a lower
   `prompt_influence` for more variety). Completion: each call returned `success` and a `sound_id`.
4. Post-process only when the user asked for it or the destination needs it (loudness, trimming, a
   loop check, a format for a file they will import elsewhere).
   Completion: the script printed `"ok": true` for every file, or you report its error line.
5. Deliver: put each sound's `media_tag` (`MEDIA:<file_path>`) on its own line in your reply, with one
   line of text before it. That one line works on every surface: Hermes Desktop plays it, as this
   plugin's sound card where Desktop supports plugin media cards (waveform, loop audition,
   downloads, Next actions) and in its built-in player otherwise; Telegram, Discord, Slack,
   WhatsApp, Signal and the other messaging apps send it as a file; the CLI shows the path. Rules:
   - The file path appears ONLY on its `MEDIA:` line, never in prose, or messaging apps send it twice.
     Markdown links and images are not delivered.
   - Always deliver the generated sound's own `media_tag`, even when you also converted or processed
     it: where Desktop shows plugin media cards, that line is the sound card (play, loop audition,
     and downloads in other formats when the Hermes host has ffmpeg). Files you made from it (a
     `loopfix` result, a conversion, a pack zip) go on their own `MEDIA:<abs path>` lines after it,
     so messaging apps receive both files; say which one to use for what.
   Completion: every sound produced is on exactly one `MEDIA:` line.
6. A sound pack: generate each sound (steps 3-4), then run `pack`; deliver the zip on its own
   `MEDIA:` line, plus the manifest summary. Completion: the zip exists and
   `manifest.json` lists every sound. Takes of one sound for a game follow "Game sound sets" above
   (`--variations`; completion: `loudness_spread_lu` is 1 or less).
7. End with what was made: per sound the real length (`length_seconds`; say so when `length_note`
   reports a difference from the request), format and the credits billed, plus the loop
   verdict when a loop was checked. Offer the next step (variations, shorter/longer, a loop, the other model).

## Pitfalls

- The format can't be chosen per call; convert locally instead of changing the user's setting.
- ElevenLabs documents 44.1 kHz WAV for its Pro plan and above and 192 kbps MP3 for Creator and
  above. If the account's plan refuses a format, the tool reports "ElevenLabs returned HTTP 4xx: ...":
  report it and suggest the default format.
- An auto-length sound (no `duration_seconds`) is billed a flat amount per generation (~50 credits
  measured, for 2 s and 3 s clips alike), whatever the prompt length; with `duration_seconds` set, the
  bill is per second (~10 credits/s). Quote `billed_characters`.
- Prompts over 450 characters are refused by the tool before any request; shorten them.
- Post-processing runs where the `terminal` tool runs. With a non-local terminal backend (Docker, SSH,
  Modal) the generated files and this script are not there and every call reports `bad_input`; then
  deliver the original sound and say processing needs the local terminal.
- Files live on the machine Hermes runs on. A Desktop connected to a remote Hermes still plays and
  downloads them; paths in replies are paths on that host.

## Verification

- Each delivered sound came from a successful tool result (`success: true`) or an `ok: true` script line.
- Loops you called seamless have a `loopcheck` verdict of `seamless`.
- Every length you quoted is a `length_seconds` (or a `probe` result), not a requested duration.
- A variation set you delivered has a `loudness_spread_lu` of 1 or less, and positional takes report
  `channels: 1`.
- Each delivered file appears on exactly one `MEDIA:` line.
