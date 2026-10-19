"""The loopback callback over https: an optional scheme, when the app is registered that way.

Spotify permits plain http on a loopback IP literal and that is the default; an https URI is
supported for dashboards that refuse the http form. In that case the listener terminates TLS
itself with a self-signed certificate for 127.0.0.1, because the loopback flow still requires an
IP literal (RFC 8252).
"""

from __future__ import annotations

import socket
import threading
import time

import httpx
import pytest

from spotify_plugin import spotify_auth


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_until_listening(port: int, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket() as sock:
            sock.settimeout(0.2)
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.05)
    raise AssertionError(f"loopback listener never came up on 127.0.0.1:{port}")


@pytest.mark.parametrize("uri, expected", [
    ("https://127.0.0.1:8899/spotify/callback", ("127.0.0.1", 8899, "/spotify/callback")),
    ("http://127.0.0.1:8899/spotify/callback", ("127.0.0.1", 8899, "/spotify/callback")),
    ("https://localhost:1234/cb", ("localhost", 1234, "/cb")),
])
def test_validate_redirect_uri_accepts_both_schemes_on_loopback(uri, expected):
    assert spotify_auth._validate_redirect_uri(uri) == expected


@pytest.mark.parametrize("uri", [
    "https://example.com:443/callback",
    "ftp://127.0.0.1:8899/callback",
    "https://127.0.0.1/callback",
])
def test_validate_redirect_uri_rejects_everything_but_loopback_with_a_port(uri):
    with pytest.raises(Exception):
        spotify_auth._validate_redirect_uri(uri)


def test_https_loopback_receives_the_authorization_code():
    port = _free_port()
    uri = f"https://127.0.0.1:{port}/spotify/callback"
    resultado: dict = {}

    def corre():
        resultado["valor"] = spotify_auth._wait_for_callback(uri, timeout_seconds=15)

    thread = threading.Thread(target=corre, daemon=True)
    thread.start()
    _wait_until_listening(port)

    # verify=False is the point of this test: the listener serves a self-signed certificate
    # generated on the spot, exactly as the real login does, so there is no CA to trust. The
    # request never leaves 127.0.0.1, so nothing can be intercepted.
    response = httpx.get(f"{uri}?code=abc123&state=xyz", verify=False, timeout=10)  # noqa: S501
    thread.join(timeout=15)

    assert response.status_code == 200
    assert resultado["valor"]["code"] == "abc123"
    assert resultado["valor"]["state"] == "xyz"


def test_tls_context_ignores_a_tls_verifying_sslcontext_shim(monkeypatch):
    """Hermes injects ``truststore``, whose server-side ``wrap_socket`` raises; we must not use it."""
    truststore = pytest.importorskip("truststore")
    truststore.inject_into_ssl()
    assert spotify_auth._server_ssl_context_class().__module__ == "ssl"


def test_default_redirect_uri_stays_http_on_the_loopback_literal():
    """http is what Spotify documents for a loopback IP literal, and what existing apps registered.

    Changing the default would send https to an app that only allow-listed the http URI and end
    in "Invalid redirect URI" for anyone logging in on a new machine.
    """
    assert spotify_auth.DEFAULT_REDIRECT_URI == "http://127.0.0.1:43827/spotify/callback"
