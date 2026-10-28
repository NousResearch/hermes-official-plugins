"""Handlers for the ElevenLabs Music tools.

Billed calls (compose, compose plan, upload, stems, video) run as background jobs (``jobs.py``): the
handler checks every argument and setting, starts the job, waits inline as long as the surface allows
and returns either the finished result or the job id with what to do next. Free calls (draft plan,
finetunes) answer directly.

Files land in ``$HERMES_HOME/cache/generated/audio/`` (inside the folders the gateway delivers from,
outside the transient caches it sweeps), resolved per call so a multiplexed gateway serves each profile
its own; every result carries ``MEDIA:<path>`` lines the model puts in its reply to deliver them.
"""

from __future__ import annotations

import datetime
import io
import re
import uuid
import zipfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from tools.registry import tool_error, tool_result

from . import audio_length as _audio_length
from . import inputs as _in
from . import jobs as _jobs
from . import provenance as _provenance
from . import schemas as S
from .elevenlabs_api import (
    FORMATS,
    LEGACY_FORMATS,
    MODELS,
    NO_RETRY,
    PLAN_MODELS,
    Settings,
    ToolFailure,
    api_key,
    call,
    ok,
    read_settings,
    split_multipart,
)

Setting = Callable[[str], Any]

# Surfaces where a person waits at a local app and a long tool call is fine. Anything else is a chat app
# (Telegram, Discord, Slack, ...), where the chat should not sit silent for a whole generation.
_LOCAL_SURFACES = {"", "cli", "tui", "desktop", "acp", "cron", "api_server", "webhook", "kanban", "local"}
LOCAL_WAIT_SECONDS = 280.0
_SONG_DEADLINE = 900.0
_EXT_BY_TYPE = {"audio/mpeg": "mp3", "audio/mp3": "mp3", "audio/ogg": "opus", "audio/opus": "opus",
                "audio/wav": "wav", "audio/x-wav": "wav", "audio/flac": "flac", "audio/aac": "aac",
                "audio/mp4": "m4a"}
_STEM_EXTS = {"mp3", "opus", "wav", "flac", "m4a", "aac", "ogg"}
_MAX_STEM_TOTAL = 400 * 1024 * 1024


def is_chat_app(surface: str | None) -> bool:
    return (surface or "") not in _LOCAL_SURFACES


def _estimate(seconds: float | None) -> float:
    # Measured: a 10 s clip took ~4 s end to end; generation runs well under real time.
    return 8 + 0.4 * (seconds if seconds else 120)


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[:48] or "track"


def _save(audio: bytes, prefix: str, label: str, ext: str, track_id: str) -> Path:
    directory = _provenance.audio_dir()
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    path = directory / f"{prefix}_{_slug(label)}_{stamp}_{track_id}.{ext}"
    # "x": never overwrite (the id makes a clash practically impossible; a clash is a bug, not data loss).
    with open(path, "xb") as handle:
        handle.write(audio)
    return path


def _sections(plan: dict) -> list[dict[str, Any]]:
    """Each chunk's label and time range in the finished song: what an edit keeps or regenerates."""
    out, at = [], 0
    for index, chunk in enumerate(plan.get("chunks") or []):
        if not isinstance(chunk, dict):
            continue
        if "range" in chunk and isinstance(chunk["range"], dict):
            length = int(chunk["range"].get("end_ms", 0)) - int(chunk["range"].get("start_ms", 0))
            label = "kept slice"
        else:
            length = int(chunk.get("duration_ms") or 0)
            first = str(chunk.get("text") or "").split("\n", 1)[0].strip()
            label = first[1:-1] if first.startswith("[") and first.endswith("]") else (first[:40] or "section")
        out.append({"index": index, "label": label, "start_ms": at, "end_ms": at + length})
        at += length
    return out


def _lyrics(plan: dict) -> str:
    return "\n\n".join(str(c.get("text") or "").strip() for c in plan.get("chunks") or []
                       if isinstance(c, dict) and c.get("text")).strip()


def _song_result(settings: Settings, response, meta: dict[str, Any], session_id: str) -> dict[str, Any]:
    """Save a ``/v1/music/detailed`` answer and record it; the tool result."""
    metadata, audio, _ctype = split_multipart(response)
    song = metadata.get("song_metadata") if isinstance(metadata.get("song_metadata"), dict) else {}
    plan = metadata.get("composition_plan") if isinstance(metadata.get("composition_plan"), dict) else {}
    ext = FORMATS[settings.output_format]
    track_id = uuid.uuid4().hex[:12]
    title = str(song.get("title") or "").strip()
    parent = meta.get("parent") or {}
    if not title and parent.get("title"):
        title = f"{parent['title']} (edit)"
    path = _save(audio, "music", title or meta.get("prompt") or "song", ext, track_id)
    length = _audio_length.audio_seconds(audio, ext)
    song_id = response.headers.get("song-id") or None
    record = {
        "id": track_id, "kind": "song", "tool": meta["tool"], "title": title or None,
        "prompt": meta.get("prompt"), "description": song.get("description"), "genres": song.get("genres"),
        "languages": song.get("languages"), "explicit": song.get("is_explicit"), "model": meta["model"],
        "instrumental": meta.get("instrumental"), "finetune_id": meta.get("finetune_id"), "seed": meta.get("seed"),
        "requested_seconds": meta.get("requested_seconds"), "length_seconds": length,
        "output_format": settings.output_format, "file_name": path.name, "bytes": len(audio), "song_id": song_id,
        "stored_for_editing": bool(song_id) and settings.store_for_editing, "parent_id": parent.get("id"),
        "session_id": session_id or None,
    }
    _provenance.record(record, {"composition_plan": plan, "song_metadata": song,
                                "words_timestamps": metadata.get("words_timestamps") or []})
    result = {
        "success": True, "track_id": track_id, "title": record["title"], "description": record["description"],
        "genres": record["genres"], "file_path": str(path), "media_tag": f"MEDIA:{path}",
        "length_seconds": length, "requested_seconds": meta.get("requested_seconds"), "model": meta["model"],
        "output_format": settings.output_format, "bytes": len(audio), "elevenlabs_song_id": song_id,
        "stored_for_editing": record["stored_for_editing"], "sections": _sections(plan),
        "lyrics": _lyrics(plan) or None, "composition_plan": plan or None,
    }
    if not record["stored_for_editing"]:
        result["editing_note"] = ("Not stored at ElevenLabs for editing (store_for_editing is off), so its slices "
                                  "cannot be reused; elevenlabs_music_upload can store the file.")
    return result


def _pending(record: dict[str, Any], chat_app: bool) -> dict[str, Any]:
    job_id = record["id"]
    remaining = max(15, int(record.get("est_seconds") or 60) - int(_jobs.elapsed(record)))
    view = {"success": True, "status": "running", "job_id": job_id, "label": record.get("label"),
            "elapsed_seconds": round(_jobs.elapsed(record)), "estimated_seconds_left": remaining}
    if chat_app:
        sleep = min(600, remaining + 10)
        view["next_step"] = (
            f"Still generating in the background. Now, in this same turn: (1) reply to the user in one line that it "
            f"started and you'll send it here in about {remaining} s; (2) arm a wake with "
            f"terminal(command=\"sleep {sleep} && echo elevenlabs music job {job_id}\", background=true, "
            f"notify_on_complete=true); (3) end your turn. When the wake arrives, call elevenlabs_music_job with "
            f"job_id \"{job_id}\" and wait_seconds 60, then send each media_tag line. If the user asks meanwhile, "
            f"check with wait_seconds 0. If the terminal tool or its notifications are unavailable here, call "
            f"elevenlabs_music_job with wait_seconds 240 instead. Never start the same request again: it would "
            f"bill twice.")
    else:
        view["next_step"] = (f"Still generating. Call elevenlabs_music_job with job_id \"{job_id}\" and "
                             f"wait_seconds 240 to wait for it. Never start the same request again: it would bill twice.")
    return view


def job_view(record: dict[str, Any] | None, chat_app: bool) -> str:
    if record is None:
        return tool_error("No music job with that id in this profile (job records are kept for 7 days).")
    status = record.get("status")
    if status == "done" and isinstance(record.get("result"), dict):
        return tool_result({**record["result"], "job_id": record["id"]})
    if status == "failed":
        return tool_error(f"Music job {record['id']} ({record.get('label')}) failed: {record.get('error')}")
    if _jobs.is_lost(record):
        return tool_error(f"Music job {record['id']} ({record.get('label')}) was interrupted (Hermes restarted while "
                          f"it ran) and left no result. ElevenLabs may still have billed it; tell the user before "
                          f"trying again.")
    return tool_result(_pending(record, chat_app))


def _start(kind: str, label: str, est: float, work: Callable[[], dict], *, surface: str | None,
           settings: Settings, session_id: str) -> str:
    chat_app = is_chat_app(surface)
    job_id = _jobs.start(kind, label, est, work, session_id=session_id)
    wait = settings.chat_app_wait_seconds if chat_app else LOCAL_WAIT_SECONDS
    return job_view(_jobs.wait(job_id, wait), chat_app)


def _guarded(handler: Callable[..., str]) -> Callable[..., str]:
    def run(*args, **kwargs) -> str:
        try:
            return handler(*args, **kwargs)
        except ValueError as exc:
            return tool_error(str(exc))
        except ToolFailure as exc:
            return tool_error(str(exc))
    return run


# ---------------------------------------------------------------------------------------------- compose

@_guarded
def compose(args: dict, setting: Setting, *, surface: str | None = None, session_id: str = "") -> str:
    settings = read_settings(setting)
    prompt = _in.text(args, "prompt", S.MAX_PROMPT_CHARS, required=True)
    length = _in.number(args, "length_seconds", S.MIN_SONG_SECONDS, S.MAX_SONG_SECONDS)
    model = _in.choice(args, "model", MODELS, settings.model)
    instrumental = _in.flag(args.get("instrumental"), "instrumental")
    finetune = _in.finetune(args)
    api_key()  # a missing key fails here, before a job exists
    body: dict[str, Any] = {"prompt": prompt, "model_id": model, "force_instrumental": instrumental,
                            "store_for_inpainting": settings.store_for_editing, "with_timestamps": True}
    if length is not None:
        body["music_length_ms"] = int(round(length * 1000))
    if finetune:
        body["finetune_id"] = finetune
    meta = {"tool": S.COMPOSE["name"], "prompt": prompt, "model": model, "instrumental": instrumental,
            "finetune_id": finetune, "requested_seconds": length}

    def work() -> dict:
        response = ok(call(settings, "POST", "/v1/music/detailed", params={"output_format": settings.output_format},
                           json_body=body, deadline=_SONG_DEADLINE))
        return _song_result(settings, response, meta, session_id)

    return _start("song", prompt[:80], _estimate(length), work, surface=surface, settings=settings,
                  session_id=session_id)


def _source_track(chunks: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The library record of the first stored song a plan reuses (kept slice or conditioning reference),
    so an edit is listed under that song's title."""
    ids = [c.get("song_id") or (c.get("conditioning_ref") or {}).get("song_id") for c in chunks]
    wanted = next((i for i in ids if i), None)
    if not wanted:
        return None
    return next((row for row in _provenance.read_all() if row.get("song_id") == wanted), None)


@_guarded
def compose_plan(args: dict, setting: Setting, *, surface: str | None = None, session_id: str = "") -> str:
    settings = read_settings(setting)
    chunks, total_ms = _in.chunks(args.get("chunks"))
    default_model = settings.model if settings.model in PLAN_MODELS else PLAN_MODELS[-1]
    model = _in.choice(args, "model", PLAN_MODELS, default_model)
    seed = None if args.get("seed") in (None, "") else _in.integer(args["seed"], "seed", 0, 2147483647)
    finetune = _in.finetune(args)
    api_key()
    body: dict[str, Any] = {"composition_plan": {"chunks": chunks}, "model_id": model,
                            "store_for_inpainting": settings.store_for_editing, "with_timestamps": True}
    if seed is not None:
        body["seed"] = seed
    if finetune:
        body["finetune_id"] = finetune
    first = next((c["text"].split("\n", 1)[0] for c in chunks if "text" in c), "plan")
    styles = next((", ".join(c["positive_styles"][:6]) for c in chunks if "positive_styles" in c), "")
    meta = {"tool": S.COMPOSE_PLAN["name"], "prompt": styles or first, "model": model, "seed": seed,
            "finetune_id": finetune, "requested_seconds": total_ms / 1000, "parent": _source_track(chunks)}

    def work() -> dict:
        response = ok(call(settings, "POST", "/v1/music/detailed", params={"output_format": settings.output_format},
                           json_body=body, deadline=_SONG_DEADLINE))
        return _song_result(settings, response, meta, session_id)

    return _start("song", f"plan, {len(chunks)} chunks, {total_ms / 1000:g} s", _estimate(total_ms / 1000), work,
                  surface=surface, settings=settings, session_id=session_id)


@_guarded
def draft_plan(args: dict, setting: Setting, **_) -> str:
    settings = read_settings(setting)
    prompt = _in.text(args, "prompt", S.MAX_PROMPT_CHARS, required=True)
    length = _in.number(args, "length_seconds", S.MIN_SONG_SECONDS, S.MAX_SONG_SECONDS)
    default_model = settings.model if settings.model in PLAN_MODELS else PLAN_MODELS[-1]
    model = _in.choice(args, "model", PLAN_MODELS, default_model)
    body: dict[str, Any] = {"prompt": prompt, "model_id": model}
    if length is not None:
        body["music_length_ms"] = int(round(length * 1000))
    if args.get("source_chunks") not in (None, []):
        source, _total = _in.chunks(args["source_chunks"])
        body["source_composition_plan"] = {"chunks": source}
    plan = ok(call(settings, "POST", "/v1/music/plan", json_body=body, deadline=120)).json()
    if not isinstance(plan, dict) or not isinstance(plan.get("chunks"), list):
        raise ToolFailure("ElevenLabs returned a plan this tool cannot read")
    total = sum(int(c.get("duration_ms") or 0) for c in plan["chunks"] if isinstance(c, dict))
    return tool_result({"success": True, "model": model, "total_seconds": total / 1000, "chunks": plan["chunks"],
                        "sections": _sections(plan), "billed": False,
                        "next_step": "Show the user the sections and lyrics; after any edits, pass chunks (and this "
                                     "model) to elevenlabs_music_compose_plan to make it."})


@_guarded
def finetunes(args: dict, setting: Setting, **_) -> str:
    settings = read_settings(setting)
    params: dict[str, Any] = {"page_size": 100}
    created_by = _in.choice(args, "created_by", ("self", "workspace", "elevenlabs"), None)
    if created_by:
        params["created_by"] = created_by
    data = ok(call(settings, "GET", "/v1/music/finetunes", params=params, deadline=60)).json()
    rows = data.get("finetunes") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise ToolFailure("ElevenLabs returned a finetune list this tool cannot read")
    keep = ("id", "name", "tags", "model_id", "status", "created_by", "primary_genre")
    return tool_result({"success": True, "finetunes": [{k: r.get(k) for k in keep} for r in rows if isinstance(r, dict)],
                        "more": bool(data.get("has_more"))})


# ------------------------------------------------------------------------------------- files as input

def _source_audio(args: dict, cwd: str) -> tuple[str, bytes, dict[str, Any] | None]:
    """``(file name, bytes, source record or None)`` from ``track_id`` or ``file_path`` (exactly one)."""
    track_id, file_path = args.get("track_id"), args.get("file_path")
    if bool(track_id) == bool(file_path):
        raise ValueError("pass exactly one of track_id (a track from these tools) or file_path (an audio file)")
    if track_id:
        row = _provenance.find(str(track_id))
        if row is None:
            raise ValueError(f"no track {track_id!r} in this profile's music library")
        path = _provenance.audio_path(row)
        if path is None:
            raise ValueError(f"track {track_id!r} has no audio file on this machine any more")
    else:
        path = _in.local_file(file_path, "file_path", _in.AUDIO_EXTS, _in.MAX_AUDIO_UPLOAD_BYTES, cwd)
        row = None
    return path.name, path.read_bytes(), row


def _mime(name: str) -> str:
    ext = name.rsplit(".", 1)[-1].lower()
    return {"mp3": "audio/mpeg", "wav": "audio/wav", "flac": "audio/flac", "ogg": "audio/ogg", "opus": "audio/ogg",
            "m4a": "audio/mp4", "aac": "audio/aac", "webm": "audio/webm", "mp4": "video/mp4", "mov": "video/quicktime",
            "m4v": "video/mp4", "mkv": "video/x-matroska", "avi": "video/x-msvideo"}.get(ext, "application/octet-stream")


@_guarded
def upload(args: dict, setting: Setting, *, surface: str | None = None, session_id: str = "", cwd: str = "") -> str:
    settings = read_settings(setting)
    name, audio, row = _source_audio(args, cwd)
    extract = _in.flag(args.get("extract_plan"), "extract_plan", default=True)
    api_key()
    data = {"with_timestamps": "true"}
    if extract:
        data["extract_composition_plan"] = settings.model if settings.model in PLAN_MODELS else PLAN_MODELS[-1]

    def work() -> dict:
        answer = ok(call(settings, "POST", "/v1/music/upload", data=data,
                         files=[("file", (name, audio, _mime(name)))], deadline=_SONG_DEADLINE)).json()
        if not isinstance(answer, dict) or not answer.get("song_id"):
            raise ToolFailure("ElevenLabs accepted the upload but returned no song id")
        plan = answer.get("composition_plan") if isinstance(answer.get("composition_plan"), dict) else {}
        return {"success": True, "elevenlabs_song_id": answer["song_id"], "source": row["id"] if row else name,
                "sections": _sections(plan), "lyrics": _lyrics(plan) or None, "composition_plan": plan or None,
                "words": len(answer.get("words_timestamps") or []),
                "next_step": "Use elevenlabs_song_id in elevenlabs_music_compose_plan chunks (song_id + range) to keep, "
                             "extend or condition on parts of this song."}

    return _start("upload", f"upload {name}", 30 + len(audio) / 200_000, work, surface=surface, settings=settings,
                  session_id=session_id)


def _legacy_format(settings: Settings) -> str:
    """The stems and video endpoints accept only 44.1 kHz MP3 / Opus: map a 48 kHz MP3 setting down."""
    return settings.output_format if settings.output_format in LEGACY_FORMATS else "mp3_44100_192"


def _extract_zip(blob: bytes, prefix: str, label: str, base: dict[str, Any], output_format: str,
                 session_id: str) -> list[dict[str, Any]]:
    """Save every audio member of a ZIP answer as its own track; returns one entry per file."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(blob))
    except zipfile.BadZipFile:
        raise ToolFailure(f"ElevenLabs returned a damaged ZIP archive. {NO_RETRY}") from None
    members = [m for m in archive.infolist() if not m.is_dir()
               and m.filename.rsplit(".", 1)[-1].lower() in _STEM_EXTS]
    if not members:
        raise ToolFailure("ElevenLabs' ZIP archive held no audio files")
    if sum(m.file_size for m in members) > _MAX_STEM_TOTAL:
        raise ToolFailure(f"ElevenLabs' ZIP archive unpacks to more than {_MAX_STEM_TOTAL // (1024 * 1024)} MB")
    out = []
    for member in members:
        stem = _slug(Path(member.filename).stem)[:24]
        ext = member.filename.rsplit(".", 1)[-1].lower()
        audio = archive.read(member)
        track_id = uuid.uuid4().hex[:12]
        path = _save(audio, prefix, f"{stem}-{label}", ext, track_id)
        length = _audio_length.audio_seconds(audio, ext)
        _provenance.record({**base, "id": track_id, "stem": stem, "length_seconds": length,
                            "output_format": output_format, "file_name": path.name, "bytes": len(audio),
                            "session_id": session_id or None})
        out.append({"track_id": track_id, "stem": stem, "file_path": str(path), "media_tag": f"MEDIA:{path}",
                    "length_seconds": length, "bytes": len(audio)})
    return out


@_guarded
def stems(args: dict, setting: Setting, *, surface: str | None = None, session_id: str = "", cwd: str = "") -> str:
    settings = read_settings(setting)
    name, audio, row = _source_audio(args, cwd)
    variation = {"two": "two_stems_v1", "six": "six_stems_v1"}[_in.choice(args, "stems", ("two", "six"), "six")]
    output_format = _legacy_format(settings)
    api_key()
    label = (row or {}).get("title") or (row or {}).get("prompt") or Path(name).stem

    def work() -> dict:
        response = ok(call(settings, "POST", "/v1/music/stem-separation", params={"output_format": output_format},
                           data={"stem_variation_id": variation}, files=[("file", (name, audio, _mime(name)))],
                           deadline=_SONG_DEADLINE))
        base = {"kind": "stem", "tool": S.STEMS["name"], "title": (row or {}).get("title"),
                "prompt": (row or {}).get("prompt") or name, "parent_id": (row or {}).get("id"),
                "model": variation}
        files = _extract_zip(response.body, "stem", str(label), base, output_format, session_id)
        return {"success": True, "stems": files, "source": (row or {}).get("id") or name,
                "media_tags": [f["media_tag"] for f in files]}

    return _start("stems", f"stems of {label}"[:120], 45 + len(audio) / 100_000, work, surface=surface,
                  settings=settings, session_id=session_id)


@_guarded
def from_video(args: dict, setting: Setting, *, surface: str | None = None, session_id: str = "", cwd: str = "") -> str:
    settings = read_settings(setting)
    raw = args.get("video_paths")
    if not isinstance(raw, list) or not 1 <= len(raw) <= 10:
        raise ValueError("video_paths must list 1 to 10 video files")
    paths = [_in.local_file(p, f"video_paths[{i}]", _in.VIDEO_EXTS, _in.MAX_VIDEO_BYTES, cwd) for i, p in enumerate(raw)]
    if sum(p.stat().st_size for p in paths) > _in.MAX_VIDEO_BYTES:
        raise ValueError("the videos add up to more than 200 MB; ElevenLabs accepts at most 200 MB in total")
    description = _in.text(args, "description", S.MAX_PROMPT_CHARS)
    tags = _in.string_list(args.get("tags"), "tags", 10, item_chars=100)
    model = _in.choice(args, "model", MODELS, settings.model)
    flags = {key: _in.flag(args.get(key), key) for key in ("add_sfx", "separate_tracks", "use_audio_as_reference")}
    output_format = _legacy_format(settings)
    api_key()
    data: dict[str, Any] = {"model_id": model, **{k: str(v).lower() for k, v in flags.items()}}
    if description:
        data["description"] = description
    if tags:
        data["tags"] = tags
    label = description or ", ".join(tags) or paths[0].stem

    def work() -> dict:
        files = [("videos", (p.name, p.read_bytes(), _mime(p.name))) for p in paths]
        response = ok(call(settings, "POST", "/v1/music/video-to-music", params={"output_format": output_format},
                           data=data, files=files, deadline=_SONG_DEADLINE))
        base = {"kind": "score", "tool": S.FROM_VIDEO["name"], "prompt": label, "model": model,
                "song_id": response.headers.get("song-id") or None}
        ctype = response.headers.get("content-type", "").split(";")[0].strip().lower()
        if ctype == "application/zip" or response.body[:2] == b"PK":
            tracks = _extract_zip(response.body, "score", label, base, output_format, session_id)
        else:
            ext = _EXT_BY_TYPE.get(ctype, FORMATS.get(output_format, "mp3"))
            track_id = uuid.uuid4().hex[:12]
            path = _save(response.body, "score", label, ext, track_id)
            length = _audio_length.audio_seconds(response.body, ext)
            _provenance.record({**base, "id": track_id, "length_seconds": length, "output_format": output_format,
                                "file_name": path.name, "bytes": len(response.body), "session_id": session_id or None})
            tracks = [{"track_id": track_id, "file_path": str(path), "media_tag": f"MEDIA:{path}",
                       "length_seconds": length, "bytes": len(response.body)}]
        return {"success": True, "tracks": tracks, "media_tags": [t["media_tag"] for t in tracks],
                "videos": [p.name for p in paths], "model": model}

    return _start("score", f"score for {paths[0].name}"[:120], 90 + sum(p.stat().st_size for p in paths) / 2_000_000,
                  work, surface=surface, settings=settings, session_id=session_id)


@_guarded
def job(args: dict, setting: Setting, *, surface: str | None = None, **_) -> str:
    job_id = args.get("job_id")
    if not isinstance(job_id, str) or not _jobs.JOB_ID_RE.fullmatch(job_id.strip()):
        raise ValueError("job_id must be the 12-character id a music tool returned")
    wait = _in.number(args, "wait_seconds", 0, 240) or 0
    return job_view(_jobs.wait(job_id.strip(), wait), is_chat_app(surface))

