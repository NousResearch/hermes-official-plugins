"""Tests that _token_post converts non-JSON 2xx responses into a shaped AuthError."""
import httpx
import pytest

from hermes_cli.auth_constants import AuthError
from spotify_plugin import spotify_auth


def test_refresh_oauth_state_handles_non_json_200_response(monkeypatch):
    def fake_post(*_args, **_kwargs):
        return httpx.Response(200, text="<html>captive portal</html>")

    monkeypatch.setattr(httpx, "post", fake_post)

    with pytest.raises(AuthError) as exc_info:
        spotify_auth.refresh_oauth_state(
            {"refresh_token": "rt-123", "client_id": "cid-123", "accounts_base_url": "https://accounts.spotify.com"}
        )

    assert exc_info.value.code == "spotify_refresh_invalid"
    assert exc_info.value.relogin_required is True
