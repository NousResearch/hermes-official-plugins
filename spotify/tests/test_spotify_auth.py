"""Spotify login state: refresh, quarantine and status, against a real auth.json under a temp home.

Moved from hermes-agent core ``tests/hermes_cli/test_spotify_auth.py``.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from hermes_cli import auth as auth_mod
from hermes_cli.auth_constants import AuthError
from spotify_plugin import spotify_auth, spotify_cli

_STATE = {
    "client_id": "test-client",
    "redirect_uri": "http://127.0.0.1:43827/spotify/callback",
    "api_base_url": spotify_auth.DEFAULT_API_BASE_URL,
    "accounts_base_url": spotify_auth.DEFAULT_ACCOUNTS_BASE_URL,
    "scope": spotify_auth.DEFAULT_SCOPE,
    "granted_scope": spotify_auth.DEFAULT_SCOPE,
    "token_type": "Bearer",
    "access_token": "dead-access-token",
    "refresh_token": "dead-refresh-token",
    "expires_at": "2000-01-01T00:00:00+00:00",
    "expires_in": 3600,
    "obtained_at": "2000-01-01T00:00:00+00:00",
    "auth_type": "oauth_pkce",
}


def _seed(state: dict) -> None:
    with auth_mod._auth_store_lock():
        store = auth_mod._load_auth_store()
        store["active_provider"] = "nous"
        auth_mod._store_provider_state(store, "spotify", state, set_active=False)
        auth_mod._save_auth_store(store)


def test_refresh_persists_fresh_token_without_changing_active_provider(monkeypatch):
    _seed(dict(_STATE))
    monkeypatch.setattr(spotify_auth, "refresh_oauth_state", lambda state, timeout_seconds=20.0: {
        **state, "access_token": "fresh-token", "expires_at": "2099-01-01T00:00:00+00:00"})

    creds = spotify_auth.resolve_runtime_credentials()

    assert creds["access_token"] == "fresh-token"
    assert auth_mod.get_provider_auth_state("spotify")["access_token"] == "fresh-token"
    assert auth_mod.get_active_provider() == "nous"


def test_terminal_refresh_failure_quarantines_dead_tokens(monkeypatch):
    """relogin_required + a refresh_token present: the dead tokens leave auth.json and a
    last_auth_error marker is written, so later calls fail fast without a network retry."""
    _seed(dict(_STATE))

    def _terminal_refresh(_state, **_kw):
        raise AuthError("Spotify token refresh failed.", provider="spotify",
                        code="spotify_refresh_failed", relogin_required=True)

    monkeypatch.setattr(spotify_auth, "refresh_oauth_state", _terminal_refresh)
    with pytest.raises(AuthError) as exc_info:
        spotify_auth.resolve_runtime_credentials(force_refresh=True)
    assert exc_info.value.code == "spotify_refresh_failed"

    persisted = auth_mod.get_provider_auth_state("spotify")
    for key in ("access_token", "refresh_token", "expires_at", "expires_in", "obtained_at"):
        assert key not in persisted
    assert persisted["client_id"] == "test-client"
    assert persisted["last_auth_error"]["code"] == "spotify_refresh_failed"
    assert persisted["last_auth_error"]["reason"] == "runtime_refresh_failure"
    assert auth_mod.get_active_provider() == "nous"


def test_status_and_logout_commands_read_and_clear_the_stored_login(capsys):
    _seed({**_STATE, "expires_at": "2099-01-01T00:00:00+00:00"})

    spotify_cli.handle_cli(SimpleNamespace(action="status"))
    out = capsys.readouterr().out
    assert "spotify: logged in" in out and "client_id: test-client" in out

    spotify_cli.handle_cli(SimpleNamespace(action="logout"))
    assert "Logged out of Spotify." in capsys.readouterr().out
    assert auth_mod.get_provider_auth_state("spotify") is None
    assert auth_mod.get_active_provider() == "nous"

    spotify_cli.handle_cli(SimpleNamespace(action="status"))
    assert "spotify: logged out" in capsys.readouterr().out


def test_relogin_requests_default_scopes_added_after_the_stored_login(monkeypatch):
    """A login saved before ugc-image-upload joined DEFAULT_SCOPE must request it on re-login."""
    old_scope = spotify_auth.DEFAULT_SCOPE.replace("ugc-image-upload", "").strip()
    _seed({**_STATE, "scope": f"{old_scope} custom-extra", "granted_scope": old_scope})
    seen = {}

    def capture(**kwargs):
        seen["scope"] = kwargs["scope"]
        raise SystemExit("stop before the browser")

    monkeypatch.setattr(spotify_auth, "_build_authorize_url", capture)
    with pytest.raises(SystemExit):
        spotify_auth.login(SimpleNamespace(scope=None, client_id=None, redirect_uri=None, no_browser=True))
    requested = seen["scope"].split()
    assert "ugc-image-upload" in requested and "custom-extra" in requested
    assert len(requested) == len(set(requested))
