"""The inbound media download is bounded by the gateway's cap while it is read.

The gateway caps inbound media (``gateway.max_inbound_media_bytes``) and every
other platform plugin streams the body so an oversized attachment is refused
*while* it is read. A lying or absent ``Content-Length`` must not be able to
hold the whole blob in the gateway's memory.
"""
from __future__ import annotations

from typing import List

import httpx
import pytest

from xchat_plugin.api import XChatApi, XChatApiError


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.fixture
def capped(monkeypatch):
    """A small inbound-media cap, as the gateway's ``max_inbound_media_bytes``."""
    from gateway.platforms import base as gw_base

    monkeypatch.setattr(gw_base, "get_inbound_media_max_bytes", lambda: 1024)
    return gw_base


@pytest.mark.asyncio
async def test_an_oversized_attachment_is_refused_while_it_is_read(capped):
    """The body must not be buffered whole before the cap is applied."""

    def handler(request: httpx.Request) -> httpx.Response:
        # An honest Content-Length, and a body well over the cap.
        return httpx.Response(200, content=b"x" * (256 * 1024), headers={"content-length": str(256 * 1024)})

    api = XChatApi("tok", client=_client(handler))
    with pytest.raises(ValueError, match="too large"):
        await api.media_download("111:999", "mhk9")
    await api.aclose()


@pytest.mark.asyncio
async def test_a_lying_content_length_cannot_smuggle_an_oversized_body(capped):
    """No Content-Length at all: the running total is what has to catch it."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = b"x" * (256 * 1024)
        return httpx.Response(200, content=body, headers={"content-length": "1"})

    api = XChatApi("tok", client=_client(handler))
    with pytest.raises(ValueError, match="too large"):
        await api.media_download("111:999", "mhk9")
    await api.aclose()


@pytest.mark.asyncio
async def test_an_attachment_within_the_cap_is_returned(capped):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"\x00ciphertext\xff")

    api = XChatApi("tok", client=_client(handler))
    blob = await api.media_download("111:999", "mhk9")
    assert blob == b"\x00ciphertext\xff"
    await api.aclose()


@pytest.mark.asyncio
async def test_an_error_status_still_carries_the_body(capped):
    calls: List[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(404, content=b"no such media")

    api = XChatApi("tok", client=_client(handler))
    with pytest.raises(XChatApiError) as raised:
        await api.media_download("111:999", "mhk9")
    assert raised.value.status == 404
    assert "no such media" in raised.value.detail
    await api.aclose()
