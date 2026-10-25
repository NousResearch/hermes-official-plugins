"""The loopback callback over https: an optional scheme, when the app is registered that way.

Spotify permits plain http on a loopback IP literal and that is the default; an https URI is
supported for dashboards that refuse the http form. In that case the listener terminates TLS
itself with a self-signed certificate for 127.0.0.1, because the loopback flow still requires an
IP literal (RFC 8252).
"""

from __future__ import annotations

import socket
import sys
import threading
import time
import traceback

import httpx
import pytest

from spotify_plugin import spotify_auth


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _thread_failure(thread: threading.Thread | None, error: dict | None) -> BaseException | None:
    """The exception the callback thread died with, if it died before answering."""
    return (error or {}).get("erro")


def _wait_until_listening(
    port: int, thread: threading.Thread | None = None, error: dict | None = None, timeout: float = 10.0
) -> None:
    """Wait for the callback listener, and say why it never came up.

    A failure inside the callback thread is invisible from this side: the socket never binds, so
    the test can only say "never came up", which cannot tell a slow start from a certificate that
    was never generated (``load_cert_chain`` raising, for instance) or a bind that failed outright.
    Surfacing that thread's exception turns the failure into the real cause on every CI runner.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket() as sock:
            sock.settimeout(0.2)
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                return
        if _thread_failure(thread, error) is not None or (thread is not None and not thread.is_alive()):
            break
        time.sleep(0.05)
    cause = _thread_failure(thread, error)
    if cause is not None:
        raise AssertionError(
            f"loopback listener never came up on 127.0.0.1:{port}: "
            f"callback thread raised {type(cause).__name__}: {cause}"
        ) from cause
    if thread is not None and not thread.is_alive():
        raise AssertionError(
            f"loopback listener never came up on 127.0.0.1:{port}: callback thread exited without binding"
        )
    stack = ""
    if thread is not None and thread.ident in sys._current_frames():
        stack = "".join(traceback.format_stack(sys._current_frames()[thread.ident]))
    raise AssertionError(f"loopback listener never came up on 127.0.0.1:{port}; callback thread is at:\n{stack}")


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


def test_https_loopback_receives_the_authorization_code(monkeypatch):
    # HTTPServer.server_bind calls socket.getfqdn(host) before listen(); on macOS CI runners that
    # reverse lookup of 127.0.0.1 stalls past 30 s, so the listener never comes up in time. It only
    # sets server_name, which the callback never reads.
    monkeypatch.setattr(socket, "getfqdn", lambda name="": name)
    port = _free_port()
    uri = f"https://127.0.0.1:{port}/spotify/callback"
    resultado: dict = {}

    def corre():
        try:
            resultado["valor"] = spotify_auth._wait_for_callback(uri, timeout_seconds=45)
        except BaseException as exc:  # noqa: BLE001 - re-raised with context by the helper
            resultado["erro"] = exc

    thread = threading.Thread(target=corre, daemon=True)
    thread.start()
    _wait_until_listening(port, thread, error=resultado)

    # verify=False is the point of this test: the listener serves a self-signed certificate
    # generated on the spot, exactly as the real login does, so there is no CA to trust. The
    # request never leaves 127.0.0.1, so nothing can be intercepted.
    try:
        response = httpx.get(f"{uri}?code=abc123&state=xyz", verify=False, timeout=10)  # noqa: S501
    except httpx.HTTPError as exc:
        # The socket can bind and still be unusable (the TLS wrap runs after the bind), so the
        # request itself has to carry the callback thread's exception into the failure message.
        causa = _thread_failure(thread, resultado)
        if causa is not None:
            raise AssertionError(
                f"https callback request failed with {type(exc).__name__}: {exc}; "
                f"callback thread raised {type(causa).__name__}: {causa}"
            ) from causa
        raise
    thread.join(timeout=45)

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

# --- the cached pair itself: owner-only, atomic, and regenerated when corrupt -------------------

def _cache_pair():
    from hermes_constants import get_hermes_home
    cache = get_hermes_home() / "cache" / "spotify-loopback"
    return cache, cache / "cert.pem", cache / "key.pem"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes only")
def test_the_cached_key_is_owner_only():
    """The pair is a secret private key: it may not be readable by another local account."""
    assert spotify_auth._loopback_tls_context() is not None
    _cache, _cert, key = _cache_pair()
    assert key.stat().st_mode & 0o077 == 0, oct(key.stat().st_mode)


def test_no_partial_write_is_left_behind():
    spotify_auth._loopback_tls_context()
    cache, _cert, _key = _cache_pair()
    assert [p.name for p in cache.glob("*.part")] == []


def test_a_corrupt_cached_pair_is_regenerated():
    """A truncated pair (an old crash, a swapped key/cert) used to wedge every login until the
    cache was deleted by hand: the exists-check accepted it and load_cert_chain kept failing."""
    cache, cert, key = _cache_pair()
    cache.mkdir(parents=True, exist_ok=True)
    cert.write_bytes(b"-----BEGIN CERTIFICATE-----\ntruncated")
    key.write_bytes(b"-----BEGIN PRIVATE KEY-----\ntruncated")

    assert spotify_auth._loopback_tls_context() is not None
    assert b"BEGIN CERTIFICATE" in cert.read_bytes()
    assert b"PRIVATE KEY" in key.read_bytes()
