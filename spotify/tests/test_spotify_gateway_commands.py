"""Gateway slash-command handlers: one-line formatting over the existing Spotify tools."""

from __future__ import annotations

import pytest
from spotify_plugin import gateway_commands as cmds
from spotify_plugin import spotify_tools
from spotify_plugin.spotify_client import SpotifyError

TRACK = {"uri": "spotify:track:abc", "name": "Blackbird", "artists": [{"name": "The Beatles"}], "duration_ms": 562000}


class _FakeClient:
    def __init__(self):
        self.calls: list[tuple] = []
        self.response: dict = {}

    def request(self, method, path, *, params=None, json_body=None, **kw):
        if path == "/me/player/devices":  # an active device, for the tools' device guard / default device
            return {"devices": [{"id": "dev", "name": "Kitchen Sonos", "is_active": True}]}
        self.calls.append((method, path, params, json_body))
        return self.response

    def get_currently_playing(self, **kw):
        self.calls.append(("GET", "/me/player/currently-playing", kw, None))
        return self.response


@pytest.fixture
def client(monkeypatch):
    fake = _FakeClient()
    monkeypatch.setattr(spotify_tools, "SpotifyClient", lambda: fake)
    return fake


def test_play_without_a_query_resumes(client):
    assert cmds.play("") == "Playback resumed"
    assert client.calls == [("PUT", "/me/player/play", {"device_id": None},
                              {"context_uri": None, "uris": None, "offset": None, "position_ms": None})]


def test_play_searches_then_plays_the_first_track(client):
    responses = [{"tracks": {"items": [TRACK]}},
                 {"uri": "spotify:track:abc", "album": {"uri": "spotify:album:alb"}}, {"ok": True}]
    calls = []

    def request(method, path, *, params=None, json_body=None, **_kw):
        if path == "/me/player/devices":
            return {"devices": [{"id": "dev", "is_active": True}]}
        calls.append((method, path, params, json_body))
        return responses.pop(0)

    client.request = request
    assert cmds.play("blackbird") == "Now playing: Blackbird — The Beatles"
    assert calls[0][1] == "/search" and calls[0][2]["q"] == "blackbird" and calls[0][2]["type"] == "track"
    assert calls[2][1] == "/me/player/play" and calls[2][3]["offset"] == {"uri": "spotify:track:abc"}


def test_play_reports_no_match(client):
    client.response = {"tracks": {"items": []}}
    assert cmds.play("nothing like this") == "No track found for 'nothing like this'"


def test_pause(client):
    assert cmds.pause() == "Paused"
    assert client.calls[-1][1] == "/me/player/pause"


def test_skip_names_the_next_track(client):
    client.response = {"item": TRACK}
    assert cmds.skip() == "Skipped · next: Blackbird — The Beatles"
    assert [c[1] for c in client.calls] == ["/me/player/next", "/me/player/currently-playing"]


def test_nowplaying_is_one_line_with_device_and_progress(client):
    client.response = {"item": TRACK, "device": {"name": "Kitchen Sonos"}, "progress_ms": 42000, "is_playing": True}
    assert cmds.nowplaying() == "▶ Blackbird — The Beatles · 0:42 / 9:22 · Kitchen Sonos"


def test_nowplaying_without_a_device_or_track(client):
    client.response = {"item": TRACK, "progress_ms": 0}
    assert cmds.nowplaying() == "▶ Blackbird — The Beatles · 0:00 / 9:22"
    client.response = {}
    assert cmds.nowplaying() == "Nothing is playing"


def test_queue_adds_the_first_match(client):
    responses = [{"tracks": {"items": [TRACK]}}, {"ok": True}]
    calls = []

    def request(method, path, *, params=None, json_body=None, **_kw):
        if path == "/me/player/devices":
            return {"devices": [{"id": "dev", "is_active": True}]}
        calls.append((method, path, params, json_body))
        return responses.pop(0)

    client.request = request
    assert cmds.queue("blackbird") == "Queued: Blackbird — The Beatles"
    assert calls[1][1] == "/me/player/queue" and calls[1][2]["uri"] == "spotify:track:abc"


def test_queue_usage(client):
    assert cmds.queue("") == "Usage: /spotify queue <track>"


@pytest.mark.parametrize("message, expected", [
    ("Spotify Premium required for this action", cmds.PREMIUM_REQUIRED),
    ("Player command failed: No active device found", "No active Spotify device — open Spotify somewhere"),
    ("Spotify is not configured — run `hermes spotify login`", cmds.NOT_CONFIGURED),
    ("Odd upstream failure", "Odd upstream failure"),
])
def test_errors_map_to_one_line_messages(client, monkeypatch, message, expected):
    def boom(*_a, **_k):
        raise SpotifyError(message)

    monkeypatch.setattr(client, "get_currently_playing", boom)
    assert cmds.nowplaying() == expected


def test_a_missing_login_answers_not_configured_instead_of_raising(monkeypatch):
    from spotify_plugin.spotify_client import SpotifyAuthRequiredError

    def no_login():
        raise SpotifyAuthRequiredError("Spotify is not authenticated. Run `hermes spotify login` first.")

    monkeypatch.setattr(spotify_tools, "SpotifyClient", no_login)
    assert cmds.nowplaying() == cmds.NOT_CONFIGURED
    assert cmds.pause() == cmds.NOT_CONFIGURED


@pytest.mark.parametrize("raw, action, query", [
    ("play", "play", ""),
    ("play blackbird the beatles", "play", "blackbird the beatles"),
    ("PAUSE", "pause", None),
    ("skip", "skip", None),
    ("np", "np", None),
    ("queue  hey jude", "queue", " hey jude"),
])
def test_spotify_command_routes_each_subcommand(monkeypatch, raw, action, query):
    seen = []
    for name in ("play", "pause", "skip", "np", "queue"):
        takes_query = cmds._SUBCOMMANDS[name][1]
        fn = (lambda q="", _n=name: seen.append((_n, q)) or _n) if takes_query \
            else (lambda _n=name: seen.append((_n, None)) or _n)
        monkeypatch.setitem(cmds._SUBCOMMANDS, name, (fn, takes_query))
    assert cmds.spotify_command(raw) == action
    assert seen == [(action, query)]


def test_spotify_command_subcommands_reach_the_right_tool_action(client):
    client.response = {"item": TRACK, "device": {"name": "Kitchen Sonos"}, "progress_ms": 42000}
    assert cmds.spotify_command("np") == "▶ Blackbird — The Beatles · 0:42 / 9:22 · Kitchen Sonos"
    assert cmds.spotify_command("pause") == "Paused"
    assert client.calls[-1][1] == "/me/player/pause"
    assert cmds.spotify_command("skip").startswith("Skipped")
    assert "/me/player/next" in [c[1] for c in client.calls]


@pytest.mark.parametrize("raw", ["", "   ", "rewind", "nowplaying", "volume 50"])
def test_spotify_command_unknown_subcommand_prints_usage(client, raw):
    assert cmds.spotify_command(raw) == cmds.USAGE
    assert client.calls == []
