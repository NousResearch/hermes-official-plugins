"""Response-returning services (``weather.get_forecasts``) need ``?return_response``.

Home Assistant answers a plain POST to such a service with HTTP 400 ("Service call requires
responses but caller did not ask for responses. Add ?return_response to query parameters."),
while ordinary services reject the flag. ``ha_call_service`` must work for both, against a real
HTTP server that reproduces HA's behavior (verified against HA 2026.10 on 2026-10-09).
"""

from __future__ import annotations

import asyncio
import socket

from aiohttp import web

import homeassistant_plugin.tools as tools

_NEEDS_RESPONSE = (
    "Service call requires responses but caller did not ask for responses. "
    "Add ?return_response to query parameters."
)
_FORECAST = {"weather.home": {"forecast": [{"datetime": "2026-10-10", "temperature": 61}]}}


def _ha_app(seen: list) -> web.Application:
    async def get_forecasts(request: web.Request) -> web.Response:
        seen.append(("weather", request.query_string))
        if "return_response" not in request.query:
            return web.json_response({"message": _NEEDS_RESPONSE}, status=400)
        return web.json_response({"changed_states": [], "service_response": _FORECAST})

    async def turn_on(request: web.Request) -> web.Response:
        seen.append(("light", request.query_string))
        if "return_response" in request.query:
            return web.json_response({"message": "Service does not support responses."}, status=400)
        return web.json_response([{"entity_id": "light.kitchen", "state": "on"}])

    app = web.Application()
    app.router.add_post("/api/services/weather/get_forecasts", get_forecasts)
    app.router.add_post("/api/services/light/turn_on", turn_on)
    return app


def _run(monkeypatch, coro_factory):
    seen: list = []

    async def scenario():
        runner = web.AppRunner(_ha_app(seen))
        await runner.setup()
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        await web.SockSite(runner, sock).start()
        monkeypatch.setattr(tools, "_get_config", lambda: (f"http://127.0.0.1:{port}", "test-token"))
        try:
            return await coro_factory()
        finally:
            await runner.cleanup()

    return asyncio.run(scenario()), seen


def test_response_service_retries_with_return_response(monkeypatch):
    result, seen = _run(monkeypatch, lambda: tools._async_call_service(
        "weather", "get_forecasts", entity_id="weather.home", data={"type": "daily"}))
    assert result["success"] is True
    assert result["response"] == _FORECAST
    assert seen == [("weather", ""), ("weather", "return_response")]


def test_ordinary_service_sends_one_plain_request(monkeypatch):
    result, seen = _run(monkeypatch, lambda: tools._async_call_service(
        "light", "turn_on", entity_id="light.kitchen"))
    assert result == {
        "success": True, "service": "light.turn_on",
        "affected_entities": [{"entity_id": "light.kitchen", "state": "on"}]}
    assert seen == [("light", "")]


def test_parse_service_response_handles_both_shapes():
    plain = tools._parse_service_response("light", "turn_on", [{"entity_id": "light.a", "state": "on"}])
    assert plain["affected_entities"] == [{"entity_id": "light.a", "state": "on"}]
    assert "response" not in plain
    rich = tools._parse_service_response("weather", "get_forecasts", {
        "changed_states": [{"entity_id": "weather.home", "state": "sunny"}],
        "service_response": _FORECAST})
    assert rich["affected_entities"] == [{"entity_id": "weather.home", "state": "sunny"}]
    assert rich["response"] == _FORECAST
