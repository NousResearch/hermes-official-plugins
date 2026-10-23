"""Spotify Web API client: 401 retry, empty-204 payloads, URI normalisation, auth error wrapping.

Moved from hermes-agent core ``tests/tools/test_spotify_client.py``.
"""

from __future__ import annotations

import json

import pytest

from hermes_cli.auth_constants import AuthError
from spotify_plugin import spotify_client as spotify_mod


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict | None = None, *, text: str = "", headers: dict | None = None):
        self.status_code = status_code
        self._payload = payload
        self.text = text or (json.dumps(payload) if payload is not None else "")
        self.headers = headers or {"content-type": "application/json"}
        self.content = self.text.encode("utf-8") if self.text else b""

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(spotify_mod, "resolve_runtime_credentials",
                        lambda **kwargs: {"access_token": "token-1", "base_url": "https://api.spotify.com/v1"})
    return spotify_mod.SpotifyClient()


def test_client_retries_once_after_401(monkeypatch):
    calls: list[str] = []
    tokens = iter([{"access_token": "token-1", "base_url": "https://api.spotify.com/v1"},
                   {"access_token": "token-2", "base_url": "https://api.spotify.com/v1"}])
    monkeypatch.setattr(spotify_mod, "resolve_runtime_credentials", lambda **kwargs: next(tokens))

    def fake_request(method, url, headers=None, params=None, json=None, timeout=None):
        calls.append(headers["Authorization"])
        if len(calls) == 1:
            return _FakeResponse(401, {"error": {"message": "expired token"}})
        return _FakeResponse(200, {"devices": [{"id": "dev-1"}]})

    monkeypatch.setattr(spotify_mod.httpx, "request", fake_request)
    payload = spotify_mod.SpotifyClient().request("GET", "/me/player/devices")
    assert payload["devices"][0]["id"] == "dev-1"
    assert calls == ["Bearer token-1", "Bearer token-2"]


def test_client_strips_none_from_query_and_json_parameters(monkeypatch, client):
    captured = {}

    def fake_request(method, url, headers=None, params=None, json=None, timeout=None):
        captured.update({"params": params, "json": json})
        return _FakeResponse(200, {"snapshot_id": "snapshot-1"})

    monkeypatch.setattr(spotify_mod.httpx, "request", fake_request)

    result = client.request(
        "POST", "/playlists/playlist-1/tracks",
        params={"position": None, "limit": 0},
        json_body={"uris": ["spotify:track:track-1"], "snapshot_id": None},
    )

    assert result == {"snapshot_id": "snapshot-1"}
    assert captured == {
        "params": {"limit": 0},
        "json": {"uris": ["spotify:track:track-1"]},
    }


def test_client_returns_text_for_non_json_success(monkeypatch, client):
    monkeypatch.setattr(
        spotify_mod.httpx, "request",
        lambda *args, **kwargs: _FakeResponse(200, text="accepted", headers={"content-type": "text/plain"}),
    )

    assert client.request("GET", "/non-json") == {"success": True, "text": "accepted"}


def test_client_raises_structured_api_error(monkeypatch, client):
    monkeypatch.setattr(
        spotify_mod.httpx, "request",
        lambda *args, **kwargs: _FakeResponse(400, {"error": {"message": "Invalid market"}}),
    )

    with pytest.raises(spotify_mod.SpotifyAPIError, match="Invalid market") as exc_info:
        client.request("GET", "/search")

    assert exc_info.value.status_code == 400
    assert exc_info.value.path == "/search"
    assert exc_info.value.response_body == '{"error": {"message": "Invalid market"}}'


def test_client_raises_plain_text_api_error(monkeypatch, client):
    monkeypatch.setattr(
        spotify_mod.httpx, "request",
        lambda *args, **kwargs: _FakeResponse(
            500, text="upstream unavailable", headers={"content-type": "text/plain"}),
    )

    with pytest.raises(spotify_mod.SpotifyAPIError, match="upstream unavailable") as exc_info:
        client.request("GET", "/search")

    assert exc_info.value.status_code == 500
    assert exc_info.value.response_body == "upstream unavailable"


def test_client_does_not_retry_a_second_401(monkeypatch):
    runtime_calls = []
    request_calls = []
    tokens = iter([{"access_token": "token-1", "base_url": "https://api.spotify.com/v1"},
                   {"access_token": "token-2", "base_url": "https://api.spotify.com/v1"}])

    def resolve_runtime(**kwargs):
        runtime_calls.append(kwargs)
        return next(tokens)

    def fake_request(*args, **kwargs):
        request_calls.append(kwargs["headers"]["Authorization"])
        return _FakeResponse(401, {"error": {"message": "expired token"}})

    monkeypatch.setattr(spotify_mod, "resolve_runtime_credentials", resolve_runtime)
    monkeypatch.setattr(spotify_mod.httpx, "request", fake_request)

    with pytest.raises(spotify_mod.SpotifyAPIError, match="Spotify authentication failed or expired"):
        spotify_mod.SpotifyClient().request("GET", "/me/player/devices")

    assert request_calls == ["Bearer token-1", "Bearer token-2"]
    assert runtime_calls == [
        {"force_refresh": False, "refresh_if_expiring": True},
        {"force_refresh": True, "refresh_if_expiring": True},
    ]


def test_client_includes_retry_after_in_rate_limit_error(monkeypatch, client):
    monkeypatch.setattr(
        spotify_mod.httpx, "request",
        lambda *args, **kwargs: _FakeResponse(
            429, {"error": "rate limited"}, headers={"content-type": "application/json", "Retry-After": "7"}),
    )

    with pytest.raises(spotify_mod.SpotifyAPIError, match="Retry after 7 seconds") as exc_info:
        client.request("GET", "/search")

    assert str(exc_info.value) == "Spotify rate limit exceeded. Retry after 7 seconds."


def test_normalize_spotify_uri_accepts_urls():
    uri = spotify_mod.normalize_spotify_uri("https://open.spotify.com/track/7ouMYWpwJ422jRcDASZB7P", "track")
    assert uri == "spotify:track:7ouMYWpwJ422jRcDASZB7P"


def test_normalize_spotify_id_accepts_locale_prefixed_urls():
    """Share links from a localized web player carry /intl-<locale>/ ahead of the type."""
    localized = "https://open.spotify.com/intl-de/track/7ouMYWpwJ422jRcDASZB7P"
    assert spotify_mod.normalize_spotify_id(localized, "track") == "7ouMYWpwJ422jRcDASZB7P"
    # Subtags are not always two letters.
    subtagged = "https://open.spotify.com/intl-pt-br/album/1DFixLWuPkv3KT3TnV35m3"
    assert spotify_mod.normalize_spotify_id(subtagged, "album") == "1DFixLWuPkv3KT3TnV35m3"
    # Untyped, the old code returned the type segment ("track") as the id instead of raising.
    assert spotify_mod.normalize_spotify_id(localized) == "7ouMYWpwJ422jRcDASZB7P"
    tracked = "https://open.spotify.com/intl-de/playlist/37i9dQZF1DXcBWIGoYBM5M?si=abc123"
    assert spotify_mod.normalize_spotify_id(tracked, "playlist") == "37i9dQZF1DXcBWIGoYBM5M"
    # A wrong type is still rejected once the locale is out of the way.
    with pytest.raises(spotify_mod.SpotifyError):
        spotify_mod.normalize_spotify_id(localized, "album")


@pytest.mark.parametrize("url, item_type, expected", [
    ("https://open.spotify.com/intl-de/track/7ouMYWpwJ422jRcDASZB7P", "track", "spotify:track:7ouMYWpwJ422jRcDASZB7P"),
    ("https://open.spotify.com/intl-pt-br/album/1DFixLWuPkv3KT3TnV35m3", "album", "spotify:album:1DFixLWuPkv3KT3TnV35m3"),
    ("https://open.spotify.com/intl-de/playlist/37i9dQZF1DXcBWIGoYBM5M?si=abc123", "playlist", "spotify:playlist:37i9dQZF1DXcBWIGoYBM5M"),
])
def test_normalize_spotify_uri_accepts_locale_prefixed_urls(url, item_type, expected):
    assert spotify_mod.normalize_spotify_uri(url, item_type) == expected


@pytest.mark.parametrize("value", [
    "spotify:album:album-1",
    "https://open.spotify.com/album/album-1",
])
def test_normalize_spotify_uri_rejects_type_mismatch(value):
    with pytest.raises(spotify_mod.SpotifyError, match="Expected a Spotify track, got album"):
        spotify_mod.normalize_spotify_uri(value, "track")


def test_normalize_spotify_uris_deduplicates_in_first_seen_order():
    assert spotify_mod.normalize_spotify_uris([
        "spotify:track:track-1",
        "https://open.spotify.com/track/track-1",
        "track-2",
        "spotify:track:track-2",
    ], "track") == ["spotify:track:track-1", "spotify:track:track-2"]


def test_get_currently_playing_returns_explanatory_empty_payload(monkeypatch, client):
    monkeypatch.setattr(spotify_mod.httpx, "request", lambda *a, **k: _FakeResponse(204, None, text=""))
    assert client.get_currently_playing() == {
        "status_code": 204, "empty": True,
        "message": "Spotify is not currently playing anything. Start playback in Spotify and try again.",
    }


def test_get_playback_state_returns_explanatory_empty_payload(monkeypatch, client):
    monkeypatch.setattr(spotify_mod.httpx, "request", lambda *a, **k: _FakeResponse(204, None, text=""))
    assert client.get_playback_state() == {
        "status_code": 204, "empty": True,
        "message": "No active Spotify playback session was found. Open Spotify on a device and start playback, or transfer playback to an available device.",
    }


def test_client_wraps_invalid_grant_as_auth_required_error(monkeypatch):
    def _raise_invalid_grant(**kwargs):
        raise AuthError("Spotify refresh token has expired or was revoked.", provider="spotify",
                        code="spotify_refresh_invalid_grant", relogin_required=True)

    monkeypatch.setattr(spotify_mod, "resolve_runtime_credentials", _raise_invalid_grant)
    with pytest.raises(spotify_mod.SpotifyAuthRequiredError, match="expired or was revoked"):
        spotify_mod.SpotifyClient()
