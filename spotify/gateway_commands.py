"""The ``/spotify`` slash command: terse one-line playback control without an agent turn.

One command with subcommands (``play [query]``, ``pause``, ``skip``, ``np``, ``queue <track>``),
registered through ``ctx.register_command`` so it works in the CLI and every messaging gateway.
A single ``/spotify`` name keeps clear of core's own ``/pause`` and ``/queue``. Each subcommand
calls the same tool the model would and formats one line instead of raw tool JSON; the tools stay
the only Spotify surface. Without a stored login every subcommand answers "not configured".
"""

from __future__ import annotations

import json

from . import spotify_tools as _tools
from .spotify_client import SpotifyAuthRequiredError

NOT_CONFIGURED = "Spotify not configured — run `hermes spotify login`"
PREMIUM_REQUIRED = "Spotify Premium required for playback control"
UNAVAILABLE = "Spotify is unreachable right now"
NOT_PLAYING = "Nothing is playing"


def _call(handler, **args) -> dict:
    try:
        return json.loads(handler({k: v for k, v in args.items() if v is not None}))
    except SpotifyAuthRequiredError:  # raised by the client constructor, outside the tool's error boundary
        return {"error": "Spotify not configured"}


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
        return "Usage: /spotify queue <track>"
    search = _call(_tools._handle_spotify_search, query=query, types=["track"], limit=1)
    failed = _failure(search)
    if failed:
        return failed
    tracks = ((search.get("tracks") or {}).get("items") or [])
    if not tracks:
        return f"No track found for {query!r}"
    out = _call(_tools._handle_spotify_queue, action="add", uri=tracks[0].get("uri"))
    return _failure(out) or f"Queued: {_title(tracks[0])}"


# Subcommand -> (handler, takes the rest of the line as its query).
_SUBCOMMANDS = {
    "play": (play, True),
    "pause": (pause, False),
    "skip": (skip, False),
    "np": (nowplaying, False),
    "queue": (queue, True),
}
USAGE = "Usage: /spotify play [query] | pause | skip | np | queue <track>"


def spotify_command(raw_args: str = "") -> str:
    """``/spotify <subcommand> [args]`` handler; an unknown or missing subcommand answers the usage line."""
    name, _, rest = (raw_args or "").strip().partition(" ")
    entry = _SUBCOMMANDS.get(name.lower())
    if entry is None:
        return USAGE
    handler, takes_query = entry
    return handler(rest) if takes_query else handler()
