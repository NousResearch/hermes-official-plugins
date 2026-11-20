"""hermes-spotify — Spotify playback, search, playlists and library tools for Hermes Agent.

Formerly built into hermes-agent core (``plugins/spotify`` + ``hermes_cli/auth_spotify.py``). What a
migrated install relies on is unchanged: toolset ``spotify``, the seven ``spotify_*`` tool names and
schemas, env vars ``HERMES_SPOTIFY_CLIENT_ID`` / ``HERMES_SPOTIFY_REDIRECT_URI``, and the stored login
(``providers.spotify`` in the profile's ``auth.json``). Login moves from ``hermes auth spotify`` to
``hermes spotify login``.

Wiring goes only through the public plugin API (``ctx.register_tool`` / ``ctx.register_cli_command`` /
``ctx.register_command`` for the ``/spotify`` slash command).
"""

from __future__ import annotations

from . import spotify_tools as _t
from .gateway_commands import spotify_command
from .spotify_cli import handle_cli, setup_cli

TOOLSET = "spotify"

_TOOLS = (
    ("spotify_playback",  _t.SPOTIFY_PLAYBACK_SCHEMA,  _t._handle_spotify_playback,  "🎵"),
    ("spotify_devices",   _t.SPOTIFY_DEVICES_SCHEMA,   _t._handle_spotify_devices,   "🔈"),
    ("spotify_queue",     _t.SPOTIFY_QUEUE_SCHEMA,     _t._handle_spotify_queue,     "📻"),
    ("spotify_search",    _t.SPOTIFY_SEARCH_SCHEMA,    _t._handle_spotify_search,    "🔎"),
    ("spotify_playlists", _t.SPOTIFY_PLAYLISTS_SCHEMA, _t._handle_spotify_playlists, "📚"),
    ("spotify_albums",    _t.SPOTIFY_ALBUMS_SCHEMA,    _t._handle_spotify_albums,    "💿"),
    ("spotify_library",   _t.SPOTIFY_LIBRARY_SCHEMA,   _t._handle_spotify_library,   "❤️"),
)


def register(ctx) -> None:
    """Plugin entry point — called by the Hermes plugin loader.

    On a core that still bundles ``plugins/spotify`` this installed copy has the same plugin key and
    directory name, so discovery loads it instead of the bundled one (never both); the stored login is
    shared, so installing before updating loses nothing."""
    for name, schema, handler, emoji in _TOOLS:
        ctx.register_tool(name=name, toolset=TOOLSET, schema=schema, handler=handler,
                          check_fn=_t._check_spotify_available, emoji=emoji)
    ctx.register_cli_command(
        name="spotify", help="Log in to Spotify and check the plugin's auth status",
        description="Spotify login for the spotify plugin (loopback PKCE; needs your own Spotify app).",
        setup_fn=setup_cli, handler_fn=handle_cli)
    ctx.register_command(
        "spotify", handler=spotify_command,
        description="Spotify playback in one line: play [query], pause, skip, np (now playing), queue <track>",
        args_hint="play [query] | pause | skip | np | queue <track>")


__all__ = ["register"]
