"""ElevenLabs Music backend: what the Desktop half needs from the machine Hermes runs on.

Mounted at ``/api/plugins/elevenlabs-music/`` once the plugin is in ``plugins.enabled``. The Desktop
renderer never reads files itself (Desktop may be connected to a remote Hermes, where the music lives
on another machine), so audio is streamed from here as a ``data:`` URL. The host scopes each request to
the requesting profile; every path below is resolved per request, never at import.

Routes:
    GET /tracks?limit=           the profile's music library (newest first) from the provenance records
                                 the tools write, with per-kind totals and what this host can convert to
    GET /track?id=               one track's record plus its lyrics timing and composition plan (card)
    GET /audio?id=&format=       the track's bytes: ``original`` or, when ffmpeg is on this host,
                                 ``mp3`` / ``wav`` / ``ogg`` / ``flac`` converted on the fly (nothing billed)
"""

from __future__ import annotations

import base64
import importlib.util
import os
import platform
import shutil
import subprocess
import tempfile
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query

router = APIRouter()

_MAX_AUDIO_BYTES = 60 * 1024 * 1024
# Under the plugin host's 60 s request cap, so a slow conversion reports its own error.
_CONVERT_TIMEOUT = 45
_MIME = {"mp3": "audio/mpeg", "opus": "audio/ogg", "ogg": "audio/ogg", "wav": "audio/wav", "flac": "audio/flac",
         "m4a": "audio/mp4", "aac": "audio/aac"}
_CONVERSIONS = {"mp3": ["-c:a", "libmp3lame", "-b:a", "192k"], "wav": ["-c:a", "pcm_s16le"],
                "ogg": ["-c:a", "libvorbis", "-q:a", "5"], "flac": ["-c:a", "flac"]}
_KINDS = ("song", "stem", "score")


def _load_provenance():
    """``provenance.py`` beside this folder. The plugin host imports this file standalone (no package),
    so a relative import is not available."""
    name = "hermes_elevenlabs_music_provenance"
    spec = importlib.util.spec_from_file_location(name, Path(__file__).resolve().parents[1] / "provenance.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_prov = _load_provenance()


def _ffmpeg() -> str | None:
    return shutil.which("ffmpeg")


def _view(row: dict) -> dict:
    path = _prov.audio_path(row)
    size = None
    if path is not None:
        try:
            size = path.stat().st_size
        except OSError:
            path = None
    ext = (row.get("file_name") or "").rsplit(".", 1)[-1] if row.get("file_name") else ""
    return {**row, "ext": ext, "exists": path is not None, "path": str(path) if path else None, "size": size}


def _host() -> dict:
    ffmpeg = _ffmpeg()
    return {"host": platform.node() or "this machine", "ffmpeg": bool(ffmpeg),
            "conversions": sorted(_CONVERSIONS) if ffmpeg else []}


@router.get("/tracks")
def tracks(limit: int = Query(500, ge=1, le=5000)) -> dict:
    rows = [_view(row) for row in _prov.read_all()]
    totals: dict[str, dict] = {}
    for row in rows:
        bucket = totals.setdefault(row.get("kind") if row.get("kind") in _KINDS else "song",
                                   {"count": 0, "bytes": 0, "missing": 0, "seconds": 0.0})
        bucket["count"] += 1
        if isinstance(row.get("length_seconds"), (int, float)):
            bucket["seconds"] += row["length_seconds"]
        if row["exists"]:
            bucket["bytes"] += row["size"] or 0
        else:
            bucket["missing"] += 1
    # `total` is every record in the file; `readable` the newest ones this response covers.
    return {**_host(), "tracks": rows[:limit], "readable": len(rows), "total": max(len(rows), _prov.count()),
            "totals": totals}


def _record(track_id: str) -> dict:
    if not _prov.TRACK_ID_RE.fullmatch(track_id or ""):
        raise HTTPException(status_code=400, detail="track id must be 12 hex characters")
    row = _prov.find(track_id)
    if row is None:
        raise HTTPException(status_code=404, detail="no track with that id in this profile")
    return row


@router.get("/track")
def track(id: str = Query("")) -> dict:
    row = _record(id)
    details = _prov.details(row["id"])
    words = details.get("words_timestamps") if isinstance(details.get("words_timestamps"), list) else []
    plan = details.get("composition_plan") if isinstance(details.get("composition_plan"), dict) else None
    return {**_host(), "track": _view(row), "words": words[:5000], "plan": plan}


def _convert(ffmpeg: str, source: Path, target_format: str) -> bytes:
    workdir = _prov.data_dir() / "convert"
    workdir.mkdir(parents=True, exist_ok=True)
    fd, out = tempfile.mkstemp(prefix="music_", suffix=f".{target_format}", dir=workdir)
    os.close(fd)
    try:
        proc = subprocess.run(
            [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", str(source), "-vn",
             *_CONVERSIONS[target_format], out],
            capture_output=True, timeout=_CONVERT_TIMEOUT, check=False)
        if proc.returncode != 0:
            tail = proc.stderr.decode("utf-8", "replace").strip().splitlines()[-1:] or [f"exit {proc.returncode}"]
            raise HTTPException(status_code=502, detail=f"ffmpeg could not convert the track: {tail[0][:300]}")
        if os.path.getsize(out) > _MAX_AUDIO_BYTES:
            raise HTTPException(status_code=413, detail="converted track is over the 60 MB cap")
        return Path(out).read_bytes()
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail=f"ffmpeg took longer than {_CONVERT_TIMEOUT} s") from None
    except OSError as exc:
        raise HTTPException(status_code=502, detail=f"ffmpeg could not run: {exc.strerror or exc}") from None
    finally:
        try:
            os.unlink(out)
        except OSError:
            pass


def _refused(row: dict) -> HTTPException | None:
    """Why a file that is still there is not served: a symlink, or another hard link to it (a backup tool
    such as rsnapshot or ``cp -al``). The tools write each file once, so a second link may point anywhere."""
    name = row.get("file_name")
    if not isinstance(name, str) or not _prov.AUDIO_NAME_RE.fullmatch(name):
        return None
    path = _prov.audio_dir() / name
    try:
        if path.is_symlink():
            return HTTPException(status_code=410, detail="the track file was replaced by a symbolic link, so this plugin won't serve it")
        links = path.lstat().st_nlink
    except OSError:
        return None
    if links > 1:
        return HTTPException(status_code=410, detail=(
            "the track file has another hard link (a backup tool such as rsnapshot or cp -al?), so this plugin "
            "won't serve it; Desktop's own player can still play it from the chat"))
    return None


@router.get("/audio")
def audio(id: str = Query(""), format: str = Query("original")) -> dict:
    row = _record(id)
    if format != "original" and format not in _CONVERSIONS:
        raise HTTPException(status_code=400, detail=f"format must be original or one of {', '.join(sorted(_CONVERSIONS))}")
    gone = HTTPException(status_code=410, detail="the track file is no longer on the Hermes host")
    path = _prov.audio_path(row)
    if path is None:
        raise _refused(row) or gone
    try:
        too_big = path.stat().st_size > _MAX_AUDIO_BYTES
    except OSError:
        raise gone from None
    if too_big:
        raise HTTPException(status_code=413, detail="track file is over the 60 MB cap")
    kind = path.suffix.lstrip(".")
    if format in ("original", kind):
        try:
            data = path.read_bytes()
        except OSError:
            raise gone from None
        ext = kind
    else:
        ffmpeg = _ffmpeg()
        if not ffmpeg:
            raise HTTPException(status_code=409, detail="ffmpeg is not installed on the Hermes host, so only the original "
                                                        "format can be downloaded")
        data, ext = _convert(ffmpeg, path, format), format
    mime = _MIME.get(ext, "application/octet-stream")
    return {"id": row["id"], "format": ext, "mime": mime, "bytes": len(data), "file_name": f"{path.stem}.{ext}",
            "data_url": f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"}
