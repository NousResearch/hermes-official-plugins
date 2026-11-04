"""Tool schemas. Each tool takes only the arguments its ElevenLabs endpoint accepts.

Limits mirror ElevenLabs' published OpenAPI spec and Music guides (https://api.elevenlabs.io/openapi.json,
https://elevenlabs.io/docs/eleven-api/guides/how-to/music/composition-plans): prompts up to 4,100
characters, songs 3 s to 10 min, plans of 1-30 chunks of 3-120 s, at most 30 lyric lines of 200
characters per chunk, at most 50 styles per list, conditioning references up to 30 s.
"""

from __future__ import annotations

from .elevenlabs_api import MODELS, PLAN_MODELS

MAX_PROMPT_CHARS = 4100
MIN_SONG_SECONDS, MAX_SONG_SECONDS = 3, 600
MAX_CHUNKS = 30
MIN_CHUNK_MS, MAX_CHUNK_MS = 3000, 120000
MAX_LINES, MAX_LINE_CHARS = 30, 200
MAX_STYLES = 50
MAX_CONDITIONING_MS = 30000
MIN_RANGE_MS = 50

_DELIVER = ("Put each media_tag line from the result on its own line in your reply to deliver the audio (it "
            "plays on every surface).")
_BILLED = "Billed to the user's ElevenLabs account by length."

_RANGE = {
    "type": "object",
    "properties": {"start_ms": {"type": "integer", "minimum": 0}, "end_ms": {"type": "integer", "minimum": 1}},
    "required": ["start_ms", "end_ms"],
}
_SONG_REF = {
    "type": "object",
    "description": "A slice of a song stored at ElevenLabs (an elevenlabs_song_id from an earlier result).",
    "properties": {"song_id": {"type": "string"}, "range": _RANGE},
    "required": ["song_id", "range"],
}

COMPOSE = {
    "name": "elevenlabs_music_compose",
    "description": (
        "Make a song or instrumental with ElevenLabs Music from a text prompt (3 s to 10 min, vocals in many "
        "languages or instrumental). Answer genre, mood, instruments, tempo (BPM), key and era in the prompt; "
        "lyrics may go in it too. A length the user names goes in length_seconds. Returns the file, title, "
        "lyrics, composition plan and elevenlabs_song_id (for later edits). " + _DELIVER + " " + _BILLED
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "prompt": {"type": "string", "maxLength": MAX_PROMPT_CHARS, "description": "What the music is."},
            "length_seconds": {"type": "number", "minimum": MIN_SONG_SECONDS, "maximum": MAX_SONG_SECONDS,
                               "description": "Length in seconds; omitted, ElevenLabs picks one."},
            "instrumental": {"type": "boolean", "description": "Guarantee no vocals."},
            "model": {"type": "string", "enum": list(MODELS), "description": "Omit for the user's setting."},
            "finetune_id": {"type": "string", "description": "A Music finetune id from elevenlabs_music_finetunes."},
        },
        "required": ["prompt"],
    },
}

_GENERATION_CHUNK = {
    "type": "object",
    "description": "New audio for one section.",
    "properties": {
        "text": {"type": "string", "description": (
            "Section name in brackets, then lyric lines separated by \\n; inline cues in braces "
            "({guitar solo}), sung sounds in parentheses ((ooh)). '[Outro]' alone = instrumental.")},
        "duration_ms": {"type": "integer", "minimum": MIN_CHUNK_MS, "maximum": MAX_CHUNK_MS},
        "positive_styles": {"type": "array", "items": {"type": "string"}, "maxItems": MAX_STYLES,
                            "description": "Styles to include; the first chunk's set the song (6+)."},
        "negative_styles": {"type": "array", "items": {"type": "string"}, "maxItems": MAX_STYLES},
        "context_adherence": {"type": "string", "enum": ["low", "medium", "high"],
                              "description": "How closely it follows neighbouring chunks (default high)."},
        "conditioning_ref": {**_SONG_REF, "description": "Up to 30 s of a stored song to sound like."},
        "condition_strength": {"type": "string", "enum": ["low", "medium", "high", "xhigh"]},
    },
    "required": ["text", "duration_ms", "positive_styles"],
}

COMPOSE_PLAN = {
    "name": "elevenlabs_music_compose_plan",
    "description": (
        "Make music from a composition plan: an ordered list of chunks, each either new audio (text with "
        "section name and lyrics, duration, styles) or a kept slice of a stored song (song_id + range). Use "
        "it for exact lyrics and structure, and to edit, extend, loop or re-style a song from an earlier "
        "result: keep its slices, regenerate the rest. 1-30 chunks, 3 s to 10 min total. "
        + _DELIVER + " " + _BILLED
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "chunks": {"type": "array", "minItems": 1, "maxItems": MAX_CHUNKS,
                       "items": {"anyOf": [_GENERATION_CHUNK, {**_SONG_REF, "description":
                                                               "A slice of a stored song, kept unchanged."}]}},
            "model": {"type": "string", "enum": list(PLAN_MODELS), "description": "Omit for the user's setting."},
            "seed": {"type": "integer", "minimum": 0, "maximum": 2147483647},
            "finetune_id": {"type": "string"},
        },
        "required": ["chunks"],
    },
}

DRAFT_PLAN = {
    "name": "elevenlabs_music_draft_plan",
    "description": (
        "Free (no credits): have ElevenLabs write a composition plan from a prompt, with sections, lyrics, "
        "durations and styles. Show or edit it, then pass its chunks to elevenlabs_music_compose_plan. "
        "source_chunks revises an existing plan instead."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "prompt": {"type": "string", "maxLength": MAX_PROMPT_CHARS},
            "length_seconds": {"type": "number", "minimum": MIN_SONG_SECONDS, "maximum": MAX_SONG_SECONDS},
            "model": {"type": "string", "enum": list(PLAN_MODELS)},
            "source_chunks": {"type": "array", "items": {"type": "object"}, "maxItems": MAX_CHUNKS,
                              "description": "A plan's chunks to rework according to the prompt."},
        },
        "required": ["prompt"],
    },
}

_SOURCE = {
    "track_id": {"type": "string", "description": "A track_id from an earlier result of these tools."},
    "file_path": {"type": "string", "description": "An audio file on this machine (absolute, or relative to the working directory)."},
}

UPLOAD = {
    "name": "elevenlabs_music_upload",
    "description": (
        "Store an existing song (the user's own audio file) at ElevenLabs so elevenlabs_music_compose_plan can "
        "edit, extend or sample it. Returns its elevenlabs_song_id and, optionally, its composition plan and "
        "lyrics. Billed like generating a song of that length; uploads are screened for copyright."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            **_SOURCE,
            "extract_plan": {"type": "boolean", "description": "Return the song's composition plan (default true)."},
        },
    },
}

STEMS = {
    "name": "elevenlabs_music_stems",
    "description": (
        "Split a song into stems with ElevenLabs: two (vocals, accompaniment) or six (vocals, drums, bass, "
        "guitar, piano, other). Works on a track from these tools or any audio file. " + _DELIVER + " " + _BILLED
    ),
    "parameters": {
        "type": "object",
        "properties": {**_SOURCE, "stems": {"type": "string", "enum": ["two", "six"], "description": "Default six."}},
    },
}

FROM_VIDEO = {
    "name": "elevenlabs_music_from_video",
    "description": (
        "Score a video with ElevenLabs: background music timed to 1-10 video files on this machine (joined in "
        "order, 200 MB and 10 min total). Optional description and style tags steer it; add_sfx adds sound "
        "effects for on-screen events. " + _DELIVER + " " + _BILLED
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "video_paths": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 10},
            "description": {"type": "string", "maxLength": MAX_PROMPT_CHARS},
            "tags": {"type": "array", "items": {"type": "string"}, "maxItems": 10},
            "model": {"type": "string", "enum": list(MODELS)},
            "add_sfx": {"type": "boolean"},
            "separate_tracks": {"type": "boolean", "description": "Music and sound effects as separate files."},
            "use_audio_as_reference": {"type": "boolean",
                                       "description": "Match the genre, tempo and mood of the video's own audio."},
        },
        "required": ["video_paths"],
    },
}

FINETUNES = {
    "name": "elevenlabs_music_finetunes",
    "description": "Free: list the Music finetunes this ElevenLabs account can use (own, workspace, ElevenLabs-curated) with their ids.",
    "parameters": {
        "type": "object",
        "properties": {"created_by": {"type": "string", "enum": ["self", "workspace", "elevenlabs"]}},
    },
}

JOB = {
    "name": "elevenlabs_music_job",
    "description": (
        "Check a music job these tools started in the background (by job_id), optionally waiting up to "
        "wait_seconds for it to finish. A finished job returns the same result as the tool that started it."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "job_id": {"type": "string"},
            "wait_seconds": {"type": "number", "minimum": 0, "maximum": 240, "description": "Default 0."},
        },
        "required": ["job_id"],
    },
}
