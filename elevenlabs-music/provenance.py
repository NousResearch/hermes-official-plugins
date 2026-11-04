"""What made each track: one JSON line per file in the plugin's own data directory.

The Desktop music card and library read these records through ``dashboard/plugin_api.py``. The
directory is ``PluginState(<plugin>).data_dir`` (the ``<HERMES_HOME>/plugin-data/<namespace>/`` that
``ctx.state`` uses), resolved on every call so a multiplexed gateway and the Desktop backend each write
and read the active profile's own files.

``tracks.jsonl`` is appended, never rewritten: concurrent generations from the CLI, the gateway and
Desktop can't lose each other's lines, and a torn last line is skipped on read. The bulky parts of a
song (its composition plan and word timestamps) go in ``details/<id>.json`` beside it so the list stays
small. Only the file NAME is stored; the backend resolves it under its own ``cache/generated/audio`` so
a record never points outside that folder.
"""

from __future__ import annotations

import datetime
import json
import os
import re
import uuid
from pathlib import Path
from typing import Any

PLUGIN_ID = "elevenlabs-music"
FILE_NAME = "tracks.jsonl"
TRACK_ID_RE = re.compile(r"[0-9a-f]{12}")
AUDIO_NAME_RE = re.compile(
    r"(?:music|stem|score)_[a-z0-9-]{1,48}_\d{8}_\d{6}_[0-9a-f]{12}\.(?:mp3|opus|wav|flac|m4a|aac|ogg)")
# Read at most the newest ~4 MB (many thousands of records); older lines stay on disk untouched.
_TAIL_BYTES = 4 * 1024 * 1024
_LOCK_OFFSET = 0x7FFFFFFF
# kind: song (compose / compose plan), stem (one stem of a split), score (video to music).
# requested_seconds: the length asked for (null = ElevenLabs chose); length_seconds: the file's real length.
FIELDS = ("id", "created_at", "kind", "tool", "title", "prompt", "description", "genres", "languages",
          "explicit", "model", "instrumental", "finetune_id", "seed", "requested_seconds", "length_seconds",
          "output_format", "file_name", "bytes", "song_id", "stored_for_editing", "parent_id", "stem",
          "session_id")


def data_dir() -> Path:
    from hermes_cli.plugins_state import PluginState

    return PluginState(PLUGIN_ID).data_dir


def audio_dir() -> Path:
    from hermes_constants import get_hermes_home

    return get_hermes_home() / "cache" / "generated" / "audio"


def record(entry: dict[str, Any], details: dict[str, Any] | None = None) -> None:
    """Append one track (and write its details file). Never raises: the audio is already saved and
    billed, and a lost library row must not turn the tool call into an error."""
    row = {key: entry.get(key) for key in FIELDS}
    row["created_at"] = row["created_at"] or datetime.datetime.now(datetime.UTC).isoformat(
        timespec="seconds")
    line = json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n"
    try:
        if details:
            _write_details(str(row["id"]), details)
        _append(FILE_NAME, line)
    except (OSError, ImportError, RuntimeError, ValueError, TypeError):
        pass


def _write_details(track_id: str, details: dict[str, Any]) -> None:
    directory = data_dir() / "details"
    directory.mkdir(parents=True, exist_ok=True)
    temp = directory / f".{track_id}.{uuid.uuid4().hex[:6]}.tmp"
    temp.write_text(json.dumps(details, ensure_ascii=False), encoding="utf-8")
    os.replace(temp, directory / f"{track_id}.json")


def details(track_id: str) -> dict[str, Any]:
    """The track's composition plan, word timestamps and song metadata ({} when there are none)."""
    if not isinstance(track_id, str) or not TRACK_ID_RE.fullmatch(track_id):
        return {}
    try:
        value = json.loads((data_dir() / "details" / f"{track_id}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, ImportError, RuntimeError):
        return {}
    return value if isinstance(value, dict) else {}


def _append(name: str, line: str) -> None:
    directory = data_dir()
    directory.mkdir(parents=True, exist_ok=True)
    # O_APPEND: each write lands at the end even with several writers (one short line per write).
    fd = os.open(directory / name, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        if os.name == "nt":  # pragma: no cover - Windows emulates O_APPEND with seek+write: lock it
            import msvcrt
            # Windows byte locks are mandatory: lock a sentinel byte far past any real end of file so
            # readers of the records never hit it, only other writers taking the same lock.
            os.lseek(fd, _LOCK_OFFSET, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
            try:
                os.lseek(fd, 0, os.SEEK_END)
                os.write(fd, line.encode("utf-8"))
            finally:
                os.lseek(fd, _LOCK_OFFSET, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            os.write(fd, line.encode("utf-8"))
    finally:
        os.close(fd)


def read_all() -> list[dict[str, Any]]:
    """Every readable record, newest first, de-duplicated by id."""
    try:
        path = data_dir() / FILE_NAME
        with open(path, "rb") as handle:
            size = handle.seek(0, os.SEEK_END)
            handle.seek(max(0, size - _TAIL_BYTES))
            raw = handle.read()
    except (OSError, ImportError, RuntimeError, ValueError):
        return []
    lines = raw.split(b"\n")
    if size > _TAIL_BYTES and lines:
        lines = lines[1:]  # the read started mid-file: the first line may be partial
    seen: set[str] = set()
    rows: list[dict[str, Any]] = []
    for line in reversed(lines):
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict):
            continue
        track_id = row.get("id")
        if not isinstance(track_id, str) or not TRACK_ID_RE.fullmatch(track_id) or track_id in seen:
            continue
        seen.add(track_id)
        rows.append({key: row.get(key) for key in FIELDS})
    return rows


def count() -> int:
    """How many records the file holds (lines), including any older than the read window."""
    try:
        with open(data_dir() / FILE_NAME, "rb") as handle:
            return sum(chunk.count(b"\n") for chunk in iter(lambda: handle.read(1 << 20), b""))
    except (OSError, ImportError, RuntimeError, ValueError):
        return 0


def find(track_id: str) -> dict[str, Any] | None:
    if not isinstance(track_id, str) or not TRACK_ID_RE.fullmatch(track_id):
        return None
    return next((row for row in read_all() if row["id"] == track_id), None)


def audio_path(row: dict[str, Any]) -> Path | None:
    """The record's file inside this profile's ``cache/generated/audio``, or ``None`` when the name
    is not one this plugin writes, the file is gone, it resolves (symlink) outside the folder, or it
    is hard-linked (possibly to a file elsewhere)."""
    name = row.get("file_name")
    if not isinstance(name, str) or not AUDIO_NAME_RE.fullmatch(name):
        return None
    root = audio_dir()
    try:
        resolved = (root / name).resolve(strict=True)
        root_resolved = root.resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    if resolved.parent != root_resolved or not resolved.is_file():
        return None
    # A hard link has no symlink to resolve: refuse any extra link, the tool writes each file once.
    try:
        if resolved.stat().st_nlink > 1:
            return None
    except OSError:
        return None
    return resolved
