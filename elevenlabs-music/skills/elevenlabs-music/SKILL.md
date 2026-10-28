---
name: elevenlabs-music
description: Write, edit, extend and split songs with ElevenLabs Music.
version: 1.0.0
author: Nous Research
license: MIT
metadata:
  hermes:
    tags: [music, songs, lyrics, elevenlabs, stems, soundtrack, audio]
---

# ElevenLabs Music Skill

Make songs, instrumentals, jingles, loops and video scores with the `elevenlabs_music_*` tools, edit or
extend songs they made, and split any song into stems. Every generation is billed to the user's
ElevenLabs account by length; drafting a plan and listing finetunes are free. Sound effects (one-shot
foley, UI blips) belong to the ElevenLabs Sound Effects plugin, not here.

## When to Use

- The user wants music: a song with vocals, an instrumental, a jingle, background music, a loop, a theme.
- They give lyrics or want exact structure (verse/chorus/bridge, timings).
- They want to change part of a song made earlier, make it longer, add an intro/outro, loop it, or make
  "another one like this".
- They want the vocals or drums of a song on their own (stems), or music for a video.

## Prerequisites

- The plugin's tools are listed (they appear only once `ELEVENLABS_API_KEY` is set). The Music API needs
  a paid ElevenLabs plan; a 401/403 names the fix.
- Local files the user mentions (audio to split or upload, videos to score) must be on the machine Hermes
  runs on; pass absolute paths, or paths relative to the working directory.

## How to Run

| Tool | Billed | Use for |
|---|---|---|
| `elevenlabs_music_compose` | yes | A song or instrumental from one prompt (3 s to 10 min) |
| `elevenlabs_music_draft_plan` | free | ElevenLabs writes a composition plan (sections, lyrics, styles, durations) from a prompt |
| `elevenlabs_music_compose_plan` | yes | Generate from a plan: exact lyrics/structure, and every edit, extension, loop or "sounds like" |
| `elevenlabs_music_upload` | yes | Store the user's own song at ElevenLabs so plans can edit or sample it |
| `elevenlabs_music_stems` | yes | Split a track into 2 stems (vocals, accompaniment) or 6 (vocals, drums, bass, guitar, piano, other) |
| `elevenlabs_music_from_video` | yes | Music timed to 1-10 videos, optionally with sound effects |
| `elevenlabs_music_finetunes` | free | Finetune ids (own, workspace, ElevenLabs-curated styles such as reggaeton or 80s nu-disco) |
| `elevenlabs_music_job` | free | Check or wait for a generation that is still running |

## Quick Reference

- Results carry `media_tag` (or `media_tags`), `track_id`, `title`, `lyrics`, `sections` (label + ms
  range of each part), `composition_plan` and `elevenlabs_song_id`.
- `elevenlabs_song_id` + a `range` is how a plan keeps part of an earlier song. It works only while the
  result says `stored_for_editing: true`.
- Plan limits: 1-30 chunks, each 3-120 s, total 3 s to 10 min; at most 30 lyric lines of 200 characters
  per chunk; up to 50 styles per list; a `conditioning_ref` covers at most 30 s.
- Models: `music_v2_5` (default, newest), `music_v2`, `music_v1`. Plans need v2 or v2.5.

## Procedure

### 1. Write the prompt (from ElevenLabs' prompting guide)

A prompt answers five questions: genre, mood, instrumentation, tempo (BPM), production era. Any left
open gets the most average answer. Add a key ("in A minor") for layering. Studio words move real
levers: sidechained, close-mic'd, bone-dry, tape saturation, plate reverb. Narrate arrangement in order
("start with just a shuffled drum loop, add a sub bass after four bars, then chopped vocal stabs");
`just` and `then` matter. Vocal delivery: whispered, belted, breathy, deadpan, stacked harmonies, "two
singers harmonizing". Lyrics can go straight into the prompt. Loops are about exclusion: bars, BPM, key,
and what is banned ("four bars, no melody, just drums").

- A length the user names goes in `length_seconds`, not only in the prompt.
- Instrumental: `instrumental: true` (it guarantees no vocals).
- Many languages work; say which ("Spanish lyrics").

### 2. Pick the route

- Quick idea, no exact lyrics: `elevenlabs_music_compose`.
- User's lyrics or exact structure: `elevenlabs_music_draft_plan` (free) → show the sections and lyrics →
  apply the user's edits to `chunks` → `elevenlabs_music_compose_plan` with those chunks and the same
  model. Put the user's lyric lines in each chunk's `text` after a `[Section]` name; overall vocal style
  goes in `positive_styles`, short cues in braces (`{guitar solo}`), sung sounds in parentheses
  (`(ooh)`). The first chunk's styles set the song: give it 6+.
- A style the user named that matches a curated finetune: `elevenlabs_music_finetunes` with
  `created_by: elevenlabs`, then pass its `finetune_id` and its `model_id` as `model`.

### 3. Edit, extend, loop, re-style (plan chunks over a stored song)

Read the earlier result's `sections`, `length_seconds` and `elevenlabs_song_id`, then build chunks:

| Goal | Chunks |
|---|---|
| Redo one section | slice `0..start` + new chunk for that section (its old text/styles, changed as asked) + slice `end..length` |
| Extend / add an outro | slice `0..length` + new chunk (`[Outro]`, styles from the first chunk) |
| New intro | new `[Intro]` chunk + slice `0..length` |
| Seamless loop | slice of the phrase + short glue chunk (`context_adherence: high`) + the same slice again |
| "Another song like this" | new chunks, first one with `conditioning_ref` = a ≤30 s slice + `condition_strength` |

A slice is `{"song_id": "<elevenlabs_song_id>", "range": {"start_ms": a, "end_ms": b}}`. Billing counts the
whole result's length, kept slices included. For the user's own file: `elevenlabs_music_upload` first
(billed like a generation of that length), then use its `elevenlabs_song_id` and `sections`.

### 4. Deliver

- **Every surface:** put each `media_tag` line from the result on its own line in the reply, copied
  verbatim. Add the title and a one-line description; offer next steps (variations, extend, stems).
- **Hermes Desktop:** the line becomes a music card with synced lyrics, download per format and a Next
  menu; don't describe buttons the user can see.
- **Chat apps (Telegram, Discord, Slack, WhatsApp, Signal, ...):** a generation that is not done within a
  few seconds returns `status: running` with a `job_id` and a `next_step`. Follow it exactly, in the same
  turn: reply in one line that it started (with the estimate), arm
  `terminal(command="sleep <N> && echo elevenlabs music job <job_id>", background=true, notify=true)`,
  and end the turn. A foreground `sleep` holds the chat just like waiting. When the wake arrives, call
  `elevenlabs_music_job` with `wait_seconds: 60` and send the `media_tag` lines. If the user asks "is it
  done?" meanwhile, check with `wait_seconds: 0`, deliver if finished, and answer the later wake with
  "already sent above". If the terminal tool or its notifications are unavailable (cron, `hermes chat -q`),
  call `elevenlabs_music_job` with `wait_seconds: 240` instead. Cards can't be clicked there: end with
  next steps as a short numbered list.
- **Terminal:** the result's `file_path` is the file; say where it is.

## Pitfalls

- Never start the same request again because it is slow or a job is still running: it bills twice. Check
  the job instead.
- `elevenlabs_music_compose_plan` refuses `music_v1`; drafts and edits use v2 or v2.5.
- An edit needs `stored_for_editing: true` on the source result; otherwise upload the file first.
- Section names in brackets alone (`[Outro]`) make an instrumental part; lyric lines make it sung.
- Copyrighted lyrics, artist names or uploads can be refused by ElevenLabs; say so and offer an original.
- Files go to `<HERMES_HOME>/cache/generated/audio/`; copy one into the user's project when asked.

## Verification

- The reply contains each `media_tag` line exactly as returned (one per file for stems and video scores).
- For a running job in a chat app: a one-line "started" reply plus exactly one background wake, and the
  delivery turn sends the files once.
- For edits: the result's `sections` show the kept slices where the user expected them.
