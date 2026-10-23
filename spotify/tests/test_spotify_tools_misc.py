"""spotify_tools helpers, validation and error plumbing the action-level tests do not reach."""

from __future__ import annotations

import json

import pytest

from spotify_plugin import spotify_tools
from spotify_plugin.spotify_client import SpotifyError


class _FakeClient:
    def __init__(self):
        self.calls: list[tuple] = []
        self.response: dict = {"ok": True}

    def request(self, method, path, *, params=None, json_body=None, **kw):
        self.calls.append((method, path, params, json_body))
        return self.response

    def get_playback_state(self, **kw):
        self.calls.append(("GET", "/me/player", kw, None))
        return self.response

    def get_currently_playing(self, **kw):
        self.calls.append(("GET", "/me/player/currently-playing", kw, None))
        return self.response


@pytest.fixture
def client(monkeypatch):
    fake = _FakeClient()
    monkeypatch.setattr(spotify_tools, "SpotifyClient", lambda: fake)
    return fake


def _error(out):
    return json.loads(out).get("error")


def test_available_reports_auth_failures_as_unavailable(monkeypatch):
    def boom():
        raise RuntimeError("no auth module")

    monkeypatch.setattr(spotify_tools, "auth_status", boom)
    assert spotify_tools._check_spotify_available() is False
    monkeypatch.setattr(spotify_tools, "auth_status", lambda: {"logged_in": True})
    assert spotify_tools._check_spotify_available() is True


def test_api_errors_render_verbatim_and_others_are_named():
    assert _error(spotify_tools._spotify_tool_error(SpotifyError("API says no"))) == "API says no"
    assert _error(spotify_tools._spotify_tool_error(ValueError("bad"))) == "Spotify tool failed: ValueError: bad"


def test_coercion_helpers_default_on_junk():
    assert spotify_tools._as_list(None) == []
    assert spotify_tools._as_list(" a ") == ["a"]
    assert spotify_tools._coerce_bool("YES") is True
    assert spotify_tools._coerce_bool("off") is False
    assert spotify_tools._coerce_bool("maybe") is False
    assert spotify_tools._coerce_bool(True) is True


def test_player_commands_pass_device_only(client):
    spotify_tools._handle_spotify_playback({"action": "pause", "device_id": "dev"})
    spotify_tools._handle_spotify_playback({"action": "next"})
    spotify_tools._handle_spotify_playback({"action": "previous"})
    assert client.calls == [
        ("PUT", "/me/player/pause", {"device_id": "dev"}, None),
        ("POST", "/me/player/next", {"device_id": None}, None),
        ("POST", "/me/player/previous", {"device_id": None}, None),
    ]


def test_seek_requires_and_converts_position(client):
    assert _error(spotify_tools._handle_spotify_playback({"action": "seek"})) == "position_ms is required for action='seek'"
    spotify_tools._handle_spotify_playback({"action": "seek", "position_ms": "1500"})
    assert client.calls == [("PUT", "/me/player/seek", {"position_ms": 1500, "device_id": None}, None)]


def test_volume_clamps_to_spotify_range(client):
    spotify_tools._handle_spotify_playback({"action": "set_volume", "volume_percent": 140})
    spotify_tools._handle_spotify_playback({"action": "set_volume", "volume_percent": -3})
    assert [call[2]["volume_percent"] for call in client.calls] == [100, 0]


def test_repeat_validates_state(client):
    assert _error(spotify_tools._handle_spotify_playback({"action": "set_repeat", "state": "loop"})) == "state must be one of: track, context, off"
    spotify_tools._handle_spotify_playback({"action": "set_repeat", "state": "Track"})
    assert client.calls == [("PUT", "/me/player/repeat", {"state": "track", "device_id": None}, None)]


def test_shuffle_accepts_boolean_like_state(client):
    spotify_tools._handle_spotify_playback({"action": "set_shuffle", "state": "on"})
    spotify_tools._handle_spotify_playback({"action": "set_shuffle", "state": False})
    spotify_tools._handle_spotify_playback({"action": "set_shuffle", "state": "maybe"})
    assert [call[2]["state"] for call in client.calls] == ["true", "false", "false"]


def test_empty_playback_reads_report_the_fallback(client):
    client.response = {"empty": True}
    state = json.loads(spotify_tools._handle_spotify_playback({"action": "get_state"}))
    assert state["has_active_device"] is False and state["status_code"] == 204
    assert "No active Spotify playback" in state["message"]
    client.response = {"empty": True, "message": "custom"}
    now = json.loads(spotify_tools._handle_spotify_playback({"action": "get_currently_playing"}))
    assert now["is_playing"] is False and now["message"] == "custom"


def test_nonempty_playback_reads_pass_through(client):
    client.response = {"is_playing": True}
    assert json.loads(spotify_tools._handle_spotify_playback({"action": "get_state"})) == {"is_playing": True}


def test_recently_played_cursors(client):
    assert _error(spotify_tools._handle_spotify_playback({"action": "recently_played", "after": 1, "before": 2})) == "Provide only one of 'after' or 'before'"
    spotify_tools._handle_spotify_playback({"action": "recently_played", "after": "150", "limit": "abc"})
    assert client.calls[-1] == ("GET", "/me/player/recently-played",
                                {"limit": 20, "after": 150, "before": None}, None)


def test_device_transfer_requires_a_device_and_coerces_play(client):
    assert _error(spotify_tools._handle_spotify_devices({"action": "transfer"})) == "device_id is required for action='transfer'"
    spotify_tools._handle_spotify_devices({"action": "transfer", "device_id": "dev-1", "play": "yes"})
    assert client.calls == [("PUT", "/me/player", None, {"device_ids": ["dev-1"], "play": True})]


def test_queue_get_and_add(client):
    spotify_tools._handle_spotify_queue({"action": "get"})
    out = json.loads(spotify_tools._handle_spotify_queue({"action": "add", "uri": "spotify:track:abc", "device_id": "dev"}))
    assert client.calls == [
        ("GET", "/me/player/queue", None, None),
        ("POST", "/me/player/queue", {"uri": "spotify:track:abc", "device_id": "dev"}, None),
    ]
    assert out["uri"] == "spotify:track:abc"


def test_queue_add_requires_a_uri(client):
    assert _error(spotify_tools._handle_spotify_queue({"action": "add"})) == "Spotify URI/url/id is required."


def test_search_validates_query_and_types(client):
    assert _error(spotify_tools._handle_spotify_search({})) == "query is required"
    assert "types must contain" in _error(spotify_tools._handle_spotify_search({"query": "x", "types": ["nope"]}))


def test_search_builds_params_and_clamps(client):
    spotify_tools._handle_spotify_search({"query": " miles davis ", "types": ["TRACK", "artist"], "limit": 99})
    assert client.calls[-1] == ("GET", "/search", {
        "q": "miles davis", "type": "track,artist", "limit": 50, "offset": 0,
        "market": None, "include_external": None}, None)


def test_search_reports_api_errors(client, monkeypatch):
    def boom(*args, **kwargs):
        raise SpotifyError("rate limited")

    monkeypatch.setattr(client, "request", boom)
    assert _error(spotify_tools._handle_spotify_search({"query": "x"})) == "rate limited"


def test_playlist_create_requires_a_name(client):
    assert _error(spotify_tools._handle_spotify_playlists({"action": "create"})) == "name is required for action='create'"


def test_playlist_create_sends_flags(client):
    spotify_tools._handle_spotify_playlists({"action": "create", "name": "Mix", "public": "yes",
                                             "collaborative": False, "description": "d"})
    assert client.calls == [("POST", "/me/playlists", None,
                             {"name": "Mix", "public": True, "collaborative": False, "description": "d"})]


def test_playlist_remove_and_update(client):
    spotify_tools._handle_spotify_playlists({"action": "remove_items", "playlist_id": "pl",
                                             "uris": ["spotify:track:a", "spotify:track:b"]})
    spotify_tools._handle_spotify_playlists({"action": "update_details", "playlist_id": "pl", "name": "new"})
    assert client.calls == [
        ("DELETE", "/playlists/pl/items", None,
         {"items": [{"uri": "spotify:track:a"}, {"uri": "spotify:track:b"}], "snapshot_id": None}),
        ("PUT", "/playlists/pl", None,
         {"name": "new", "public": None, "collaborative": None, "description": None}),
    ]


def test_albums_accept_an_uri_or_a_bare_id(client):
    spotify_tools._handle_spotify_albums({"action": "tracks", "id": "spotify:album:abc", "offset": "5"})
    spotify_tools._handle_spotify_albums({"action": "get", "album_id": "def"})
    assert client.calls == [
        ("GET", "/albums/abc/tracks", {"limit": 20, "offset": 5, "market": None}, None),
        ("GET", "/albums/def", {"market": None}, None),
    ]


def test_unknown_action_is_named(client):
    assert _error(spotify_tools._handle_spotify_albums({"action": "nope", "album_id": "abc"})) == "Unknown spotify_albums action: nope"
    assert _error(spotify_tools._handle_spotify_playback({"action": "nope"})) == "Unknown spotify_playback action: nope"


def test_library_requires_a_known_kind(client):
    assert _error(spotify_tools._handle_spotify_library({"action": "list"})) == "kind must be one of: tracks, albums"


def test_library_routes_by_kind(client):
    spotify_tools._handle_spotify_library({"action": "list", "kind": "tracks", "limit": 2})
    spotify_tools._handle_spotify_library({"action": "save", "kind": "albums", "items": ["spotify:album:alb1"]})
    spotify_tools._handle_spotify_library({"action": "remove", "kind": "tracks", "ids": ["one", "two"]})
    assert client.calls == [
        ("GET", "/me/tracks", {"limit": 2, "offset": 0, "market": None}, None),
        ("PUT", "/me/library", {"uris": "spotify:album:alb1"}, None),
        ("DELETE", "/me/library", {"uris": "spotify:track:one,spotify:track:two"}, None),
    ]


def test_library_remove_requires_ids(client):
    assert _error(spotify_tools._handle_spotify_library({"action": "remove", "kind": "albums"})) == "ids/items is required for action='remove'"
