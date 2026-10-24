"""Gateway slash-command handlers for Spotify (Discord, Telegram, Slack, ...).

Five terse one-line commands for common actions, so they do not need a full agent turn. Each
handler calls the same tool the model would and formats one line instead of raw tool JSON; the
underlying tools stay the only Spotify surface.

Dispatch lives in core today: it routes ``/play``, ``/pause``, ``/skip``, ``/nowplaying`` and
``/queue`` here and answers with the returned string. Availability gating must be two-factor
(toolset enabled AND Spotify authenticated) and needs core's ``gateway_availability_fn`` hook.
"""

from __future__ import annotations

import json

from . import spotify_tools as _tools

NOT_CONFIGURED = "Spotify not configured — run `hermes spotify login`"
PREMIUM_REQUIRED = "Spotify Premium required for playback control"
UNAVAILABLE = "Spotify is unreachable right now"
NOT_PLAYING = "Nothing is playing"


def _call(handler, **args) -> dict:
    return json.loads(handler({k: v for k, v in args.items() if v is not None}))


def _failure(out: dict) -> str | None:
    """Map a tool error to its one-line user message, or ``None`` on success."""
    error = str(out.get("error") or "")
    if not error:
        return None
    lowered = error.lower()
    if "no active device" in lowered or "premium" in lowered or "403" in lowered:
        return PREMIUM_REQUIRED if "premium" in lowered else "No active Spotify device — open Spotify somewhere"
    if "not configured" in lowered or "auth" in lowered or "logged" in lowered or "login" in lowered:
        return NOT_CONFIGURED
    return error


def _title(item: dict) -> str:
    artists = ", ".join(a.get("name", "") for a in item.get("artists") or [] if a.get("name"))
    name = item.get("name") or "Unknown"
    return f"{name} — {artists}" if artists else name


def _clock(ms: int | None) -> str:
    total = max(0, int(ms or 0)) // 1000
    return f"{total // 60}:{total % 60:02d}"


def play(query: str = "") -> str:
    query = (query or "").strip()
    if not query:
        out = _call(_tools._handle_spotify_playback, action="play")
        return _failure(out) or "Playback resumed"
    search = _call(_tools._handle_spotify_search, query=query, types=["track"], limit=1)
    failed = _failure(search)
    if failed:
        return failed
    tracks = ((search.get("tracks") or {}).get("items") or [])
    if not tracks:
        return f"No track found for {query!r}"
    out = _call(_tools._handle_spotify_playback, action="play", uris=[tracks[0].get("uri")])
    return _failure(out) or f"Now playing: {_title(tracks[0])}"


def pause() -> str:
    out = _call(_tools._handle_spotify_playback, action="pause")
    return _failure(out) or "Paused"


def skip() -> str:
    out = _call(_tools._handle_spotify_playback, action="next")
    failed = _failure(out)
    if failed:
        return failed
    state = _call(_tools._handle_spotify_playback, action="get_currently_playing")
    item = (state.get("item") or {})
    return f"Skipped · next: {_title(item)}" if item else "Skipped"


def nowplaying() -> str:
    out = _call(_tools._handle_spotify_playback, action="get_currently_playing")
    failed = _failure(out)
    if failed:
        return failed
    if not out.get("item"):
        return NOT_PLAYING
    device = (out.get("device") or {}).get("name")
    line = f"▶ {_title(out['item'])} · {_clock(out.get('progress_ms'))} / {_clock(out['item'].get('duration_ms'))}"
    return f"{line} · {device}" if device else line


def queue(query: str = "") -> str:
    query = (query or "").strip()
    if not query:
        return "Usage: /queue <track>"
    search = _call(_tools._handle_spotify_search, query=query, types=["track"], limit=1)
    failed = _failure(search)
    if failed:
        return failed
    tracks = ((search.get("tracks") or {}).get("items") or [])
    if not tracks:
        return f"No track found for {query!r}"
    out = _call(_tools._handle_spotify_queue, action="add", uri=tracks[0].get("uri"))
    return _failure(out) or f"Queued: {_title(tracks[0])}"
