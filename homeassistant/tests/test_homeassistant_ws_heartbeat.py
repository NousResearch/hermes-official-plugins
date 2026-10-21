"""The Home Assistant WebSocket ping interval and the close-reason log.

Home Assistant does not answer a ping within half the interval on every
install, so aiohttp closes the socket and the adapter reconnects in a loop
(#139). The interval is configurable, and a dropped connection now names its
reason instead of reconnecting silently.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from gateway.config import PlatformConfig
from homeassistant_plugin.adapter import HomeAssistantAdapter


def _adapter(**extra) -> HomeAssistantAdapter:
    return HomeAssistantAdapter(PlatformConfig(enabled=True, token="tok", extra=extra))


# -- the configured heartbeat -------------------------------------------------


def test_heartbeat_defaults_to_thirty_seconds():
    assert _adapter()._ws_heartbeat == 30


@pytest.mark.parametrize("raw, expected", [
    (60, 60.0),
    (90.5, 90.5),
    ("60", 60.0),
    (" 60 ", 60.0),
])
def test_heartbeat_is_read_from_the_platform_extra(raw, expected):
    assert _adapter(ws_heartbeat_seconds=raw)._ws_heartbeat == expected


@pytest.mark.parametrize("raw", [0, "0", 0.0])
def test_heartbeat_zero_turns_the_ping_off(raw):
    assert _adapter(ws_heartbeat_seconds=raw)._ws_heartbeat is None


@pytest.mark.parametrize("raw", ["", None])
def test_heartbeat_falls_back_to_the_default_when_unset(raw):
    assert _adapter(ws_heartbeat_seconds=raw)._ws_heartbeat == 30


def test_heartbeat_falls_back_to_the_default_when_not_a_number():
    """A typo must not take the connection down; the default still works."""
    assert _adapter(ws_heartbeat_seconds="soon")._ws_heartbeat == 30


def test_a_negative_heartbeat_turns_the_ping_off():
    assert _adapter(ws_heartbeat_seconds=-1)._ws_heartbeat is None


# -- the value reaches the connection ------------------------------------------


class _FakeSession:
    def __init__(self):
        self.heartbeat = "unset"

    async def ws_connect(self, url, **kwargs):
        self.heartbeat = kwargs.get("heartbeat")
        raise RuntimeError("stop here")


@pytest.mark.asyncio
async def test_the_configured_heartbeat_is_passed_to_the_connection(monkeypatch):
    adapter = _adapter(ws_heartbeat_seconds=90)
    session = _FakeSession()
    monkeypatch.setattr(HomeAssistantAdapter, "_new_session", staticmethod(lambda: session))
    with pytest.raises(RuntimeError):
        await adapter._ws_connect()
    assert session.heartbeat == 90.0


@pytest.mark.asyncio
async def test_a_disabled_heartbeat_is_passed_as_none(monkeypatch):
    adapter = _adapter(ws_heartbeat_seconds=0)
    session = _FakeSession()
    monkeypatch.setattr(HomeAssistantAdapter, "_new_session", staticmethod(lambda: session))
    with pytest.raises(RuntimeError):
        await adapter._ws_connect()
    assert session.heartbeat is None


# -- the close reason ---------------------------------------------------------


class _FakeWS:
    def __init__(self, close_code=None, exception=None):
        self.close_code = close_code
        self.closed = False
        self._exception = exception

    def exception(self):
        if isinstance(self._exception, BaseException):
            raise self._exception
        return self._exception


def test_close_reason_names_the_close_code():
    adapter = _adapter()
    adapter._ws = _FakeWS(close_code=1006)
    assert "code 1006" in adapter._close_reason()


def test_close_reason_names_the_socket_error():
    adapter = _adapter()
    adapter._ws = _FakeWS(close_code=1006, exception=RuntimeError("No PONG received after 15.0 seconds"))
    reason = adapter._close_reason()
    assert "code 1006" in reason
    assert "No PONG received" in reason


def test_close_reason_survives_a_broken_socket():
    """A socket that raises while reporting its exception must not take the loop down."""
    adapter = _adapter()
    adapter._ws = _FakeWS(exception=OSError("socket is gone"))
    assert "socket is gone" in adapter._close_reason()


def test_close_reason_without_a_socket():
    assert _adapter()._close_reason() == "no socket"


@pytest.mark.asyncio
async def test_a_dropped_socket_is_logged_with_its_reason(monkeypatch, caplog):
    """The reconnect used to be silent about why the socket went away."""
    import aiohttp

    adapter = _adapter()

    class _Msg:
        type = aiohttp.WSMsgType.CLOSED
        data = ""

    class _Iter:
        def __aiter__(self):
            return self

        async def __anext__(self):
            if getattr(self, "done", False):
                raise StopAsyncIteration
            self.done = True
            return _Msg()

    class _ClosingWS(_FakeWS):
        def __aiter__(self):
            return _Iter()

    adapter._ws = _ClosingWS(close_code=1006, exception=RuntimeError("Cannot write to closing transport"))

    with caplog.at_level(logging.WARNING, logger="homeassistant_plugin.adapter"):
        await adapter._read_events()

    assert any("Cannot write to closing transport" in record.getMessage() for record in caplog.records)
