"""Regression: re-entering ``connect()`` must not orphan the previous connection.

The gateway's reconnect watcher re-enters ``connect(is_reconnect=True)`` while the previous
connection is still live (``gateway/run_adapters.py``). The old code overwrote ``self._ws``,
``self._session`` and ``self._rest_session`` and started a second ``_listen_loop`` without
closing or cancelling the previous ones, so:

* every reconnect leaked the websocket ``ClientSession`` and the REST ``ClientSession`` —
  aiohttp then logs ``Unclosed client session`` when GC collects them, and the file
  descriptors stay open until then;
* two listeners ran against the same adapter, each dispatching every watched
  ``state_changed`` event.

Offline: the connection primitives are stubbed, so nothing here touches a real HA instance.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import PlatformConfig

from homeassistant_plugin.adapter import HomeAssistantAdapter


def _fake_session() -> MagicMock:
    """A ``ClientSession`` stand-in that records whether ``close()`` was awaited."""
    session = MagicMock()
    session.closed = False

    ws = MagicMock()
    ws.closed = False
    ws.send_json = AsyncMock()
    ws.close = AsyncMock()
    ws.receive_json = AsyncMock(side_effect=[
        {"type": "auth_required"},
        {"type": "auth_ok"},
        {"success": True},
    ])
    session.ws_connect = AsyncMock(return_value=ws)

    async def _close() -> None:
        session.closed = True

    session.close = AsyncMock(side_effect=_close)
    session.fake_ws = ws
    return session


def _patched_adapter(monkeypatch):
    """An adapter whose connect() never leaves the process: no sockets, no listener."""
    adapter = HomeAssistantAdapter(PlatformConfig(enabled=True, token="tok"))
    created: list[MagicMock] = []

    def _new_session() -> MagicMock:
        session = _fake_session()
        created.append(session)
        return session

    monkeypatch.setattr(adapter, "_new_session", _new_session)
    monkeypatch.setattr(adapter, "_listen_loop", AsyncMock())
    monkeypatch.setattr(adapter, "_wire_plugin_handlers", lambda _state: None)
    return adapter, created


class TestConnectReconnectLifecycle:
    @pytest.mark.asyncio
    async def test_reconnect_closes_superseded_sessions(self, monkeypatch):
        adapter, created = _patched_adapter(monkeypatch)

        assert await adapter.connect() is True
        first_ws_session, first_rest_session = created
        first_listener = adapter._listen_task
        assert first_listener is not None

        assert await adapter.connect(is_reconnect=True) is True

        assert first_ws_session.close.await_count == 1
        assert first_ws_session.closed is True
        assert first_rest_session.close.await_count == 1
        assert first_rest_session.closed is True
        assert created[0].closed and created[1].closed

        # the superseded listener must be gone, and the live one is the new task
        assert first_listener.done() is True
        assert adapter._listen_task is not first_listener
        assert adapter._listen_task.done() is False

        # two connections, one websocket + one REST session each
        assert len(created) == 4
        assert created[2].closed is False and created[3].closed is False

        await adapter.disconnect()

    @pytest.mark.asyncio
    async def test_repeated_reconnects_do_not_accumulate_sessions(self, monkeypatch):
        adapter, created = _patched_adapter(monkeypatch)

        assert await adapter.connect() is True
        for _ in range(3):
            assert await adapter.connect(is_reconnect=True) is True

        # every session but the two belonging to the live connection is closed
        for session in created[:-2]:
            assert session.closed is True, "a reconnect leaked its predecessor's session"
        assert created[-1].closed is False and created[-2].closed is False

        await adapter.disconnect()

    @pytest.mark.asyncio
    async def test_cold_connect_then_disconnect_closes_everything(self, monkeypatch):
        adapter, created = _patched_adapter(monkeypatch)

        assert await adapter.connect() is True
        await adapter.disconnect()

        assert all(session.closed for session in created)
