"""Argument checks. Everything that could waste a billed call is refused before the request goes out,
with a message the model can act on (``ValueError``)."""

from __future__ import annotations

import math
import os
import re
from pathlib import Path
from typing import Any

from . import schemas as S

_SONG_ID_RE = re.compile(r"[A-Za-z0-9_-]{4,100}")
_FINETUNE_RE = re.compile(r"[A-Za-z0-9_.:-]{1,100}")
AUDIO_EXTS = {".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".webm", ".aiff", ".aif"}
VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi"}
MAX_AUDIO_UPLOAD_BYTES = 100 * 1024 * 1024
MAX_VIDEO_BYTES = 200 * 1024 * 1024


def number(args: dict, key: str, low: float, high: float) -> float | None:
    raw = args.get(key)
    if raw is None or raw == "":
        return None
    try:
        if isinstance(raw, bool):
            raise TypeError
        value = float(raw)
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"{key} must be a number between {low:g} and {high:g}") from None
    if not (math.isfinite(value) and low <= value <= high):
        raise ValueError(f"{key} must be between {low:g} and {high:g} (got {raw!r})")
    return value


def integer(value: Any, key: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or (isinstance(value, float) and not value.is_integer()):
        raise ValueError(f"{key} must be a whole number between {low} and {high} (got {value!r})")
    value = int(value)
    if not low <= value <= high:
        raise ValueError(f"{key} must be between {low} and {high} (got {value})")
    return value


def flag(raw: Any, key: str, default: bool = False) -> bool:
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    cleaned = raw.strip().lower() if isinstance(raw, str) else None
    if cleaned in {"true", "1", "yes", "on"}:
        return True
    if cleaned in {"false", "0", "no", "off", ""}:
        return False
    raise ValueError(f"{key} must be true or false (got {raw!r})")


def text(args: dict, key: str, limit: int, required: bool = False) -> str | None:
    raw = args.get(key)
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        if required:
            raise ValueError(f"{key} is required")
        return None
    if not isinstance(raw, str):
        raise ValueError(f"{key} must be text")
    value = raw.strip()
    if len(value) > limit:
        raise ValueError(f"{key} is {len(value)} characters long; shorten it to at most {limit}")
    return value


def choice(args: dict, key: str, allowed: tuple[str, ...] | list[str], default: str | None) -> str | None:
    raw = args.get(key)
    if raw in (None, ""):
        return default
    if raw not in allowed:
        raise ValueError(f"{key} must be one of {', '.join(allowed)} (got {raw!r})")
    return raw


def finetune(args: dict) -> str | None:
    value = text(args, "finetune_id", 100)
    if value is not None and not _FINETUNE_RE.fullmatch(value):
        raise ValueError("finetune_id must be an id from elevenlabs_music_finetunes")
    return value


def string_list(value: Any, key: str, limit: int, item_chars: int = 300, required: bool = False) -> list[str]:
    if value is None:
        if required:
            raise ValueError(f"{key} is required")
        return []
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ValueError(f"{key} must be a list of strings")
    items = [v.strip() for v in value if v.strip()]
    if required and not items:
        raise ValueError(f"{key} needs at least one entry")
    if len(items) > limit:
        raise ValueError(f"{key} has {len(items)} entries; at most {limit} are allowed")
    if any(len(v) > item_chars for v in items):
        raise ValueError(f"each entry of {key} must be at most {item_chars} characters")
    return items


def _range(value: Any, where: str) -> dict[str, int]:
    if not isinstance(value, dict) or set(value) - {"start_ms", "end_ms"}:
        raise ValueError(f"{where}.range must be {{start_ms, end_ms}}")
    start = integer(value.get("start_ms"), f"{where}.range.start_ms", 0, S.MAX_SONG_SECONDS * 1000)
    end = integer(value.get("end_ms"), f"{where}.range.end_ms", 1, S.MAX_SONG_SECONDS * 1000)
    if end - start < S.MIN_RANGE_MS:
        raise ValueError(f"{where}.range must cover at least {S.MIN_RANGE_MS} ms (end_ms after start_ms)")
    return {"start_ms": start, "end_ms": end}


def song_ref(value: Any, where: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) - {"song_id", "range"}:
        raise ValueError(f"{where} must be {{song_id, range}}")
    song_id = value.get("song_id")
    if not isinstance(song_id, str) or not _SONG_ID_RE.fullmatch(song_id):
        raise ValueError(f"{where}.song_id must be an elevenlabs_song_id from an earlier result")
    return {"song_id": song_id, "range": _range(value.get("range"), where)}


_GENERATION_KEYS = {"text", "duration_ms", "positive_styles", "negative_styles", "context_adherence",
                    "conditioning_ref", "condition_strength"}


def _generation_chunk(chunk: dict, where: str) -> dict[str, Any]:
    unknown = set(chunk) - _GENERATION_KEYS
    if unknown:
        raise ValueError(f"{where} has unknown fields {sorted(unknown)}")
    body = chunk.get("text")
    if not isinstance(body, str) or not body.strip():
        raise ValueError(f"{where}.text is required (section name in brackets, then lyric lines)")
    lines = body.strip().split("\n")
    if len(lines) > S.MAX_LINES:
        raise ValueError(f"{where}.text has {len(lines)} lines; at most {S.MAX_LINES} are allowed per chunk")
    if any(len(line) > S.MAX_LINE_CHARS for line in lines):
        raise ValueError(f"{where}.text has a line over {S.MAX_LINE_CHARS} characters")
    out: dict[str, Any] = {
        "text": body.strip(),
        "duration_ms": integer(chunk.get("duration_ms"), f"{where}.duration_ms", S.MIN_CHUNK_MS, S.MAX_CHUNK_MS),
        "positive_styles": string_list(chunk.get("positive_styles"), f"{where}.positive_styles", S.MAX_STYLES,
                                       required=True),
        "negative_styles": string_list(chunk.get("negative_styles"), f"{where}.negative_styles", S.MAX_STYLES),
    }
    adherence = chunk.get("context_adherence")
    if adherence is not None:
        if adherence not in ("low", "medium", "high"):
            raise ValueError(f"{where}.context_adherence must be low, medium or high")
        out["context_adherence"] = adherence
    if chunk.get("conditioning_ref") is not None:
        ref = song_ref(chunk["conditioning_ref"], f"{where}.conditioning_ref")
        if ref["range"]["end_ms"] - ref["range"]["start_ms"] > S.MAX_CONDITIONING_MS:
            raise ValueError(f"{where}.conditioning_ref may cover at most {S.MAX_CONDITIONING_MS // 1000} s")
        out["conditioning_ref"] = ref
        strength = chunk.get("condition_strength")
        if strength is not None:
            if strength not in ("low", "medium", "high", "xhigh"):
                raise ValueError(f"{where}.condition_strength must be low, medium, high or xhigh")
            out["condition_strength"] = strength
    elif chunk.get("condition_strength") is not None:
        raise ValueError(f"{where}.condition_strength needs a conditioning_ref")
    return out


def chunks(value: Any) -> tuple[list[dict[str, Any]], int]:
    """The validated chunk list and its total length in ms."""
    if not isinstance(value, list) or not value:
        raise ValueError("chunks must be a non-empty list")
    if len(value) > S.MAX_CHUNKS:
        raise ValueError(f"chunks has {len(value)} entries; at most {S.MAX_CHUNKS} are allowed")
    out, total = [], 0
    styles: list[str] = []
    for index, chunk in enumerate(value):
        where = f"chunks[{index}]"
        if not isinstance(chunk, dict):
            raise ValueError(f"{where} must be an object")
        if "song_id" in chunk and "text" not in chunk:
            ref = song_ref(chunk, where)
            total += ref["range"]["end_ms"] - ref["range"]["start_ms"]
            out.append(ref)
        else:
            # A later section without styles keeps the previous one's (the schema: the first chunk's set the song).
            if chunk.get("positive_styles") is None and styles:
                chunk = {**chunk, "positive_styles": styles}
            gen = _generation_chunk(chunk, where)
            styles = gen["positive_styles"]
            total += gen["duration_ms"]
            out.append(gen)
    if not S.MIN_SONG_SECONDS * 1000 <= total <= S.MAX_SONG_SECONDS * 1000:
        raise ValueError(f"the plan is {total / 1000:g} s long; it must be {S.MIN_SONG_SECONDS} s to "
                         f"{S.MAX_SONG_SECONDS // 60} min in total")
    return out, total


def local_file(raw: Any, key: str, exts: set[str], max_bytes: int, cwd: str) -> Path:
    """An existing regular file of an allowed type and size; relative paths resolve against ``cwd``."""
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError(f"{key} must be a file path")
    path = Path(os.path.expanduser(raw.strip()))
    if not path.is_absolute():
        if not cwd:
            raise ValueError(f"{key} {raw!r} is relative and this session has no working directory: pass an absolute path")
        path = Path(cwd) / path
    if path.suffix.lower() not in exts:
        raise ValueError(f"{key} {raw!r} is not one of the accepted types ({', '.join(sorted(exts))})")
    try:
        if not path.is_file():
            raise ValueError(f"{key} {raw!r} is not a file on this machine")
        size = path.stat().st_size
    except OSError as exc:
        raise ValueError(f"{key} {raw!r} cannot be read ({exc.strerror or type(exc).__name__})") from None
    if size == 0:
        raise ValueError(f"{key} {raw!r} is empty")
    if size > max_bytes:
        raise ValueError(f"{key} {raw!r} is {size // (1024 * 1024)} MB; at most {max_bytes // (1024 * 1024)} MB is accepted")
    return path
