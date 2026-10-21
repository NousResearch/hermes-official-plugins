"""URI type inference and the active-device guard for the playback and queue tools.

Covers the normalizer contract the queue and playback paths rely on (a typed
URI keeps its own type, a bare id becomes a track, an unqueueable type is
refused locally) and the guard that distinguishes the active device from
merely-listed devices.
"""

from __future__ import annotations

import json

import pytest

from spotify_plugin import spotify_tools


class _FakeClient:
    """Records the calls a handler makes and answers the device lookup."""

    def __init__(self, *, devices=None, devices_error=None):
        self.calls: list[tuple] = []
        self._devices = devices if devices is not None else [{"id": "dev", "is_active": True}]
        self._devices_error = devices_error

    def request(self, method, path, *, params=None, json_body=None, **kw):
        self.calls.append((method, path, params, json_body))
        if path == "/me/player/devices":
            if self._devices_error is not None:
                raise self._devices_error
            return {"devices": self._devices}
        return {"ok": True}


def _install(monkeypatch, client):
    monkeypatch.setattr(spotify_tools, "SpotifyClient", lambda: client)
    return client


def _call(handler, args):
    return json.loads(handler(args))


# -- spotify_uri_type ---------------------------------------------------------


@pytest.mark.parametrize("value, expected", [
    ("spotify:track:abc", "track"),
    ("spotify:episode:abc", "episode"),
    ("https://open.spotify.com/track/abc", "track"),
    ("https://open.spotify.com/episode/abc?si=x", "episode"),
    ("https://open.spotify.com/intl-de/track/abc", "track"),
    ("https://open.spotify.com/intl-pt-br/album/abc", "album"),
    # A bare id carries no type.
    ("abc123", None),
    ("", None),
])
def test_spotify_uri_type_reads_the_type(value, expected):
    assert spotify_tools.spotify_uri_type(value) == expected


# -- queueable URI coercion ---------------------------------------------------


@pytest.mark.parametrize("value, expected", [
    ("spotify:track:abc", "spotify:track:abc"),
    ("spotify:episode:abc", "spotify:episode:abc"),
    ("https://open.spotify.com/track/abc", "spotify:track:abc"),
    ("https://open.spotify.com/episode/abc?si=x", "spotify:episode:abc"),
    # A bare id is the search-result case: it becomes a track.
    ("abc123", "spotify:track:abc123"),
])
def test_coerce_queueable_spotify_uri_canonicalizes(value, expected):
    assert spotify_tools._coerce_queueable_spotify_uri(value) == expected


@pytest.mark.parametrize("value", [
    "spotify:album:abc",
    "https://open.spotify.com/playlist/abc",
    "spotify:show:abc",
])
def test_coerce_queueable_spotify_uri_rejects_unqueueable_types(value):
    with pytest.raises(spotify_tools.SpotifyError, match="can only queue a track or an episode"):
        spotify_tools._coerce_queueable_spotify_uri(value)


def test_coerce_queueable_spotify_uri_rejects_mis_cased_type():
    """An oddly-cased type fails here rather than as a Spotify "Bad URI"."""
    with pytest.raises(spotify_tools.SpotifyError, match="Expected a Spotify episode, got EPISODE"):
        spotify_tools._coerce_queueable_spotify_uri("spotify:EPISODE:abc")


# -- queue add ----------------------------------------------------------------


def _queue(monkeypatch, client, **args):
    _install(monkeypatch, client)
    return _call(spotify_tools._handle_spotify_queue, {"action": "add", **args})


def test_queue_add_sends_the_canonicalized_uri(monkeypatch):
    client = _FakeClient()
    out = _queue(monkeypatch, client, uri="https://open.spotify.com/track/abc", device_id="dev")
    assert out["uri"] == "spotify:track:abc"
    assert client.calls[-1] == ("POST", "/me/player/queue", {"uri": "spotify:track:abc", "device_id": "dev"}, None)


def test_queue_add_accepts_a_bare_search_result_id(monkeypatch):
    client = _FakeClient()
    out = _queue(monkeypatch, client, uri="abc123", device_id="dev")
    assert out["uri"] == "spotify:track:abc123"


def test_queue_add_rejects_an_album_without_calling_spotify(monkeypatch):
    client = _FakeClient()
    out = _queue(monkeypatch, client, uri="spotify:album:abc", device_id="dev")
    assert "can only queue a track or an episode" in out.get("error", "")
    assert not any(call[1] == "/me/player/queue" for call in client.calls)


# -- the active-device guard ---------------------------------------------------


def test_playback_without_an_active_device_is_refused(monkeypatch):
    client = _FakeClient(devices=[{"id": "dev", "is_active": False}])
    _install(monkeypatch, client)
    out = _call(spotify_tools._handle_spotify_playback, {"action": "play", "uris": ["spotify:track:abc"]})
    assert "No active Spotify playback device" in out.get("error", "")
    assert not any(call[1] == "/me/player/play" for call in client.calls)


def test_queue_add_without_an_active_device_is_refused(monkeypatch):
    client = _FakeClient(devices=[])
    out = _queue(monkeypatch, client, uri="spotify:track:abc")
    assert "No active Spotify playback device" in out.get("error", "")
    assert not any(call[1] == "/me/player/queue" for call in client.calls)


def test_an_explicit_device_id_skips_the_device_lookup(monkeypatch):
    client = _FakeClient(devices=[])
    out = _queue(monkeypatch, client, uri="spotify:track:abc", device_id="dev")
    assert out.get("success") is True
    # No device lookup happened at all.
    assert not any(call[1] == "/me/player/devices" for call in client.calls)


def test_playback_proceeds_when_a_device_is_active(monkeypatch):
    client = _FakeClient(devices=[{"id": "dev", "is_active": True}])
    _install(monkeypatch, client)
    out = _call(spotify_tools._handle_spotify_playback, {"action": "pause"})
    assert out.get("success") is True
    assert ("PUT", "/me/player/pause", {"device_id": None}, None) in client.calls


def test_a_device_lookup_failure_is_not_masked_as_no_active_device(monkeypatch):
    """A broken lookup surfaces as itself, not as a misleading "no active device"."""
    client = _FakeClient(devices_error=RuntimeError("lookup exploded"))
    _install(monkeypatch, client)
    out = _call(spotify_tools._handle_spotify_playback, {"action": "pause"})
    assert "No active Spotify playback device" not in out.get("error", "")
    assert "lookup exploded" in out.get("error", "")
