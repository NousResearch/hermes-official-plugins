"""The configured default device (``spotify.default_device_name``).

A playback call without an explicit ``device_id`` lands on the named device
instead of on whatever Spotify considers active, and an offline default is
reported rather than silently ignored.
"""

from __future__ import annotations

import json

import pytest

from spotify_plugin import spotify_device, spotify_tools


class _FakeClient:
    def __init__(self, devices=None):
        self.calls: list[tuple] = []
        self._devices = devices if devices is not None else []

    def request(self, method, path, *, params=None, json_body=None, **kw):
        self.calls.append((method, path, params, json_body))
        if path == "/me/player/devices":
            return {"devices": self._devices}
        if path.startswith("/tracks/"):
            return {"uri": "spotify:track:abc", "album": {"uri": "spotify:album:alb"}}
        return {"success": True}


def _configure(monkeypatch, name):
    monkeypatch.setattr(spotify_device, "configured_default_device_name", lambda: name)


# -- reading the setting ------------------------------------------------------


def test_no_default_is_configured_by_default(monkeypatch):
    """Without the setting, the tools behave exactly as before."""
    monkeypatch.setattr(spotify_device, "configured_default_device_name", lambda: "")
    assert spotify_device.configured_default_device_name() == ""


def test_the_setting_is_read_from_the_spotify_config_section(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.config.load_config_readonly",
        lambda: {"spotify": {"default_device_name": "Kitchen Sonos"}})
    assert spotify_device.configured_default_device_name() == "Kitchen Sonos"


def test_a_missing_section_reads_as_no_default(monkeypatch):
    monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: {})
    assert spotify_device.configured_default_device_name() == ""


def test_an_unreadable_config_reads_as_no_default(monkeypatch):
    def boom():
        raise RuntimeError("no config")
    monkeypatch.setattr("hermes_cli.config.load_config_readonly", boom)
    assert spotify_device.configured_default_device_name() == ""


# -- resolving the device -----------------------------------------------------


def test_the_named_device_is_resolved_to_its_id(monkeypatch):
    _configure(monkeypatch, "Kitchen Sonos")
    client = _FakeClient([{"id": "dev1", "name": "Kitchen Sonos"}, {"id": "dev2", "name": "Phone"}])
    out = spotify_device.resolve_default_device(client, {})
    assert out["device_id"] == "dev1"


def test_the_name_match_ignores_case(monkeypatch):
    _configure(monkeypatch, "kitchen sonos")
    client = _FakeClient([{"id": "dev1", "name": "Kitchen Sonos"}])
    assert spotify_device.resolve_default_device(client, {})["device_id"] == "dev1"


def test_an_explicit_device_id_wins_over_the_config(monkeypatch):
    _configure(monkeypatch, "Kitchen Sonos")
    client = _FakeClient([{"id": "dev1", "name": "Kitchen Sonos"}])
    out = spotify_device.resolve_default_device(client, {"device_id": "explicit"})
    assert out["device_id"] == "explicit"
    # The device list was not even needed.
    assert not client.calls


def test_without_a_default_the_arguments_are_untouched(monkeypatch):
    _configure(monkeypatch, "")
    client = _FakeClient([{"id": "dev1", "name": "Kitchen Sonos"}])
    assert spotify_device.resolve_default_device(client, {}) == {}
    assert not client.calls


def test_an_offline_default_is_reported_with_the_available_devices(monkeypatch):
    _configure(monkeypatch, "Kitchen Sonos")
    client = _FakeClient([{"id": "dev2", "name": "Phone"}])
    with pytest.raises(spotify_tools.SpotifyError, match="is offline"):
        spotify_device.resolve_default_device(client, {})


# -- the tools use it ----------------------------------------------------------


def _play(monkeypatch, client, **args):
    monkeypatch.setattr(spotify_tools, "SpotifyClient", lambda: client)
    return json.loads(spotify_tools._handle_spotify_playback({"action": "play", **args}))


def test_a_play_without_a_device_id_uses_the_configured_default(monkeypatch):
    _configure(monkeypatch, "Kitchen Sonos")
    client = _FakeClient([{"id": "dev1", "name": "Kitchen Sonos"}])
    out = _play(monkeypatch, client, uris=["spotify:track:abc"])
    assert out.get("success") is True
    play = [call for call in client.calls if call[1] == "/me/player/play"]
    assert play and play[0][2]["device_id"] == "dev1"


def test_a_read_only_action_does_not_fetch_the_device_list(monkeypatch):
    """get_state names no device, so there is nothing to resolve."""
    _configure(monkeypatch, "Kitchen Sonos")
    client = _FakeClient([{"id": "dev1", "name": "Kitchen Sonos"}])
    monkeypatch.setattr(spotify_tools, "SpotifyClient", lambda: client)
    spotify_tools._handle_spotify_playback({"action": "get_state"})
    assert not any(call[1] == "/me/player/devices" for call in client.calls)


def test_an_offline_default_surfaces_as_a_tool_error(monkeypatch):
    _configure(monkeypatch, "Kitchen Sonos")
    client = _FakeClient([{"id": "dev2", "name": "Phone"}])
    out = _play(monkeypatch, client, uris=["spotify:track:abc"])
    assert "is offline" in out.get("error", "")
