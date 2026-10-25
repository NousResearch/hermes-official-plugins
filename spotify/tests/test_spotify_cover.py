"""Playlist cover uploads: the JPEG body, the size limit, and the request.

Spotify takes a cover as raw base64-encoded JPEG data in the body
(``Content-Type: image/jpeg``) and refuses anything over 256 KB, so the
image is re-encoded and shrunk to fit.
"""

from __future__ import annotations

import base64
import io
import json

import pytest

from spotify_plugin import spotify_cover, spotify_tools


def _png(width: int = 40, height: int = 30, color=(200, 30, 30)) -> bytes:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buffer, format="PNG")
    return buffer.getvalue()


class _FakeClient:
    def __init__(self):
        self.calls: list[tuple] = []

    def request(self, method, path, *, params=None, json_body=None, content=None,
                content_type="application/json", empty_response=None, **kw):
        self.calls.append((method, path, params, json_body, content, content_type))
        return empty_response or {"success": True}


def _playlists(monkeypatch, client, **args):
    monkeypatch.setattr(spotify_tools, "SpotifyClient", lambda: client)
    return json.loads(spotify_tools._handle_spotify_playlists({"action": "upload_cover", **args}))


# -- the JPEG encoding --------------------------------------------------------


def test_the_cover_body_is_base64_encoded_jpeg():
    encoded = spotify_cover.encode_cover_jpeg(_png())
    raw = base64.b64decode(encoded)
    assert raw.startswith(b"\xff\xd8\xff")  # JPEG magic bytes


def test_a_png_source_is_re_encoded_as_jpeg():
    encoded = spotify_cover.encode_cover_jpeg(_png())
    assert base64.b64decode(encoded).startswith(b"\xff\xd8\xff")


def test_a_transparent_source_is_flattened_rather_than_failing():
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGBA", (20, 20), (255, 0, 0, 0)).save(buffer, format="PNG")
    encoded = spotify_cover.encode_cover_jpeg(buffer.getvalue())
    assert base64.b64decode(encoded).startswith(b"\xff\xd8\xff")


def test_an_oversized_image_is_shrunk_until_it_fits():
    """A large source must come out under Spotify's 256 KB ceiling."""
    import random

    from PIL import Image

    random.seed(7)
    size = (900, 900)
    noise = bytes(random.getrandbits(8) for _ in range(size[0] * size[1] * 3))
    image = Image.frombytes("RGB", size, noise)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    encoded = spotify_cover.encode_cover_jpeg(buffer.getvalue())
    assert len(encoded.encode("ascii")) <= spotify_cover.MAX_COVER_BYTES


def test_a_body_that_is_not_an_image_is_reported():
    with pytest.raises(spotify_tools.SpotifyError, match="Could not read the image"):
        spotify_cover.encode_cover_jpeg(b"not an image at all")


# -- the image source ---------------------------------------------------------


def test_a_local_path_is_rejected():
    with pytest.raises(spotify_tools.SpotifyError, match="local file paths are not accepted"):
        spotify_cover.resolve_cover_image("/etc/passwd")


def test_an_empty_source_is_rejected():
    with pytest.raises(spotify_tools.SpotifyError, match="image_url is required"):
        spotify_cover.resolve_cover_image("   ")


def test_a_data_url_is_accepted_and_encoded(monkeypatch):
    payload = base64.b64encode(_png()).decode("ascii")
    monkeypatch.setattr(spotify_cover, "resolve_image_source", None, raising=False)
    encoded = spotify_cover.resolve_cover_image(f"data:image/png;base64,{payload}")
    assert base64.b64decode(encoded).startswith(b"\xff\xd8\xff")


def test_a_data_url_with_a_bad_payload_is_reported():
    with pytest.raises(spotify_tools.SpotifyError, match="Could not read the image"):
        spotify_cover.resolve_cover_image("data:image/png;base64,not-valid-base64!!")


# -- the request ---------------------------------------------------------------


def test_upload_cover_puts_the_base64_body(monkeypatch):
    client = _FakeClient()
    payload = base64.b64encode(_png()).decode("ascii")
    out = _playlists(monkeypatch, client, playlist_id="3lK9HyA9p04GNo4UcjuKvP",
                     image_url=f"data:image/png;base64,{payload}")
    method, path, params, json_body, content, content_type = client.calls[0]
    assert (method, path) == ("PUT", "/playlists/3lK9HyA9p04GNo4UcjuKvP/images")
    assert content_type == "image/jpeg"
    assert base64.b64decode(content).startswith(b"\xff\xd8\xff")
    assert len(content.encode("ascii")) <= spotify_cover.MAX_COVER_BYTES
    assert out["success"] is True


def test_upload_cover_is_in_the_schema():
    props = spotify_tools.SPOTIFY_PLAYLISTS_SCHEMA["parameters"]["properties"]
    assert "upload_cover" in props["action"]["enum"]
    assert "image_url" in props


def test_the_default_scope_includes_ugc_image_upload():
    from spotify_plugin import spotify_auth

    assert "ugc-image-upload" in spotify_auth.DEFAULT_SCOPE
    assert "playlist-modify-public" in spotify_auth.DEFAULT_SCOPE


def test_a_401_retry_repeats_the_raw_body(monkeypatch):
    """The token refresh must not drop the image on the second attempt."""
    import spotify_plugin.spotify_client as client_mod

    seen = []

    class _Response:
        def __init__(self, status):
            self.status_code = status
            self.headers = {"content-type": "application/json"}
            self.content = b'{"ok": true}'
            self.text = '{"ok": true}'

        def json(self):
            return {"ok": True}

    class _Client(client_mod.SpotifyClient):
        def __init__(self):
            self._runtime = {"access_token": "tok", "base_url": "https://api.spotify.com"}

        def _resolve_runtime(self, *, force_refresh=False, refresh_if_expiring=True):
            return {"access_token": "tok2", "base_url": "https://api.spotify.com"}

    def fake_request(method, url, **kwargs):
        seen.append(kwargs)
        return _Response(401 if len(seen) == 1 else 202)

    monkeypatch.setattr(client_mod.httpx, "request", fake_request)
    client = _Client()
    client.request("PUT", "/playlists/x/images", content="BASE64BODY", content_type="image/jpeg")
    assert len(seen) == 2
    assert seen[0]["content"] == "BASE64BODY"
    assert seen[1]["content"] == "BASE64BODY"
    assert seen[1]["headers"]["Content-Type"] == "image/jpeg"
