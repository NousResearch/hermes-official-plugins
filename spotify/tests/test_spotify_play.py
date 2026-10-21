"""spotify_playback play: bare track lists start inside their album and queue the rest."""

from __future__ import annotations

import json

from spotify_plugin import spotify_tools


class _FakeClient:
    def __init__(self):
        self.calls: list[tuple] = []

    def request(self, method, path, *, params=None, json_body=None, **kw):
        self.calls.append((method, path, params, json_body))
        if path == "/me/player/devices":
            return {"devices": [{"id": "dev", "is_active": True}]}
        if path.startswith("/tracks/"):
            return {"uri": "spotify:track:relinked", "album": {"uri": "spotify:album:alb"}}
        return {"success": True, "status_code": 204, "empty": True}


def _play(monkeypatch, **args):
    client = _FakeClient()
    monkeypatch.setattr(spotify_tools, "SpotifyClient", lambda: client)
    return client, json.loads(spotify_tools._handle_spotify_playback({"action": "play", **args}))


def test_uris_start_first_track_in_its_album_and_queue_the_rest(monkeypatch):
    client, out = _play(monkeypatch, uris=["spotify:track:aaa", "spotify:track:bbb", "spotify:track:ccc"], device_id="dev")
    assert client.calls == [
        ("GET", "/tracks/aaa", {"market": "from_token"}, None),
        ("PUT", "/me/player/play", {"device_id": "dev"},
         {"context_uri": "spotify:album:alb", "offset": {"uri": "spotify:track:relinked"}, "position_ms": None}),
        ("POST", "/me/player/queue", {"uri": "spotify:track:bbb", "device_id": "dev"}, None),
        ("POST", "/me/player/queue", {"uri": "spotify:track:ccc", "device_id": "dev"}, None),
    ]
    assert out["success"] and out["queued"] == ["spotify:track:bbb", "spotify:track:ccc"]


def test_context_uri_requests_pass_through_unchanged(monkeypatch):
    client, _ = _play(monkeypatch, context_uri="spotify:album:xyz", offset={"position": 2})
    # The device lookup comes first: without a device_id the guard asks for one.
    assert client.calls == [
        ("GET", "/me/player/devices", None, None),
        ("PUT", "/me/player/play", {"device_id": None},
         {"context_uri": "spotify:album:xyz", "uris": None, "offset": {"position": 2}, "position_ms": None}),
    ]
