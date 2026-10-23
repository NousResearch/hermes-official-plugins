"""spotify_playlists: paging through a playlist's items."""

from __future__ import annotations

import json

from spotify_plugin import spotify_tools


class _FakeClient:
    def __init__(self):
        self.calls: list[tuple] = []

    def request(self, method, path, *, params=None, json_body=None, **kw):
        self.calls.append((method, path, params, json_body))
        return {"items": [], "offset": (params or {}).get("offset"), "limit": (params or {}).get("limit")}


def _playlists(monkeypatch, **args):
    client = _FakeClient()
    monkeypatch.setattr(spotify_tools, "SpotifyClient", lambda: client)
    return client, json.loads(spotify_tools._handle_spotify_playlists(args))


def test_items_forwards_limit_and_offset(monkeypatch):
    client, out = _playlists(monkeypatch, action="items", playlist_id="3lK9HyA9p04GNo4UcjuKvP", limit=50, offset=200)
    assert client.calls == [("GET", "/playlists/3lK9HyA9p04GNo4UcjuKvP/items", {"limit": 50, "offset": 200, "market": None}, None)]
    assert out["offset"] == 200 and out["limit"] == 50


def test_items_accepts_share_url_and_clamps_limit(monkeypatch):
    client, _ = _playlists(monkeypatch, action="items", playlist_id="https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M?si=x",
                           limit=100, market="DE")
    assert client.calls == [("GET", "/playlists/37i9dQZF1DXcBWIGoYBM5M/items", {"limit": 50, "offset": 0, "market": "DE"}, None)]


def test_get_is_unchanged(monkeypatch):
    client, _ = _playlists(monkeypatch, action="get", playlist_id="3lK9HyA9p04GNo4UcjuKvP", limit=5, offset=100)
    assert client.calls == [("GET", "/playlists/3lK9HyA9p04GNo4UcjuKvP", {"market": None}, None)]


def test_items_is_in_the_schema():
    props = spotify_tools.SPOTIFY_PLAYLISTS_SCHEMA["parameters"]["properties"]
    assert "items" in props["action"]["enum"]
    assert props["limit"]["type"] == "integer" and props["offset"]["type"] == "integer"
