"""Unit tests for the X Chat API client (httpx MockTransport — no network).

Covers the auth lifecycle the adapter depends on: reactive 401 refresh,
refresh-token rotation persistence, proactive expiry refresh, and 429
rate-limit surfacing.
"""
from __future__ import annotations

import time
from typing import Any, Dict, List

import httpx
import pytest

from xchat_plugin.api import (
    _EVENT_FIELDS,
    XChatApi,
    XChatApiError,
    XChatRateLimited,
)

# Documented chat_message_event.fields values — the endpoint 400s on
# anything else (created_at_msec was the original bug).
_VALID_EVENT_FIELDS = {
    "id",
    "conversation_id",
    "conversation_token",
    "created_at",
    "encoded_event",
    "is_trusted",
    "previous_id",
    "sender_id",
    "message_event_signature",
}


def test_event_fields_are_all_documented():
    requested = set(_EVENT_FIELDS.split(","))
    unknown = requested - _VALID_EVENT_FIELDS
    assert not unknown, f"undocumented event fields would 400 every poll: {unknown}"


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_401_triggers_refresh_and_retry():
    calls: List[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path == "/2/oauth2/token":
            return httpx.Response(
                200,
                json={
                    "access_token": "new-access",
                    "refresh_token": "new-refresh",
                    "expires_in": 7200,
                },
            )
        auth = request.headers.get("Authorization", "")
        if auth == "Bearer stale":
            return httpx.Response(401, json={"title": "Unauthorized"})
        return httpx.Response(200, json={"data": {"id": "42"}})

    persisted: Dict[str, str] = {}

    async def on_refresh(access: str, refresh: str) -> None:
        persisted["access"] = access
        persisted["refresh"] = refresh

    api = XChatApi(
        "stale",
        refresh_token="old-refresh",
        client_id="cid",
        on_token_refresh=on_refresh,
        client=_client(handler),
    )
    out = await api.get_my_user()
    assert out == {"id": "42"}

    # 401 → token endpoint → retried request with the new bearer.
    paths = [c.url.path for c in calls]
    assert paths == ["/2/users/me", "/2/oauth2/token", "/2/users/me"]
    assert calls[-1].headers["Authorization"] == "Bearer new-access"
    # Rotated pair persisted via the callback.
    assert persisted == {"access": "new-access", "refresh": "new-refresh"}
    await api.aclose()


@pytest.mark.asyncio
async def test_401_without_refresh_config_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"title": "Unauthorized"})

    api = XChatApi("stale", client=_client(handler))
    with pytest.raises(XChatApiError) as ei:
        await api.get_my_user()
    assert ei.value.status == 401
    await api.aclose()


@pytest.mark.asyncio
async def test_401_refresh_retries_once_only():
    """A still-401 response after refresh surfaces the error instead of
    looping the refresh grant forever."""
    calls: List[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/2/oauth2/token":
            return httpx.Response(200, json={"access_token": "still-bad"})
        return httpx.Response(401, json={"title": "Unauthorized"})

    api = XChatApi(
        "stale", refresh_token="r", client_id="cid", client=_client(handler)
    )
    with pytest.raises(XChatApiError) as ei:
        await api.get_my_user()
    assert ei.value.status == 401
    assert calls.count("/2/oauth2/token") == 1
    await api.aclose()


@pytest.mark.asyncio
async def test_proactive_refresh_near_expiry():
    calls: List[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/2/oauth2/token":
            return httpx.Response(
                200, json={"access_token": "fresh", "expires_in": 7200}
            )
        return httpx.Response(200, json={"data": {"id": "42"}})

    api = XChatApi(
        "old",
        refresh_token="r",
        client_id="cid",
        token_expires_at=time.time() + 10,  # inside the refresh slack window
        client=_client(handler),
    )
    await api.get_my_user()
    assert calls[0] == "/2/oauth2/token"  # refreshed BEFORE the request
    assert calls[1] == "/2/users/me"
    await api.aclose()


@pytest.mark.asyncio
async def test_429_raises_rate_limited_with_reset():
    reset = int(time.time()) + 600

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            headers={"x-rate-limit-reset": str(reset)},
            json={"title": "Too Many Requests"},
        )

    api = XChatApi("tok", client=_client(handler))
    with pytest.raises(XChatRateLimited) as ei:
        await api.add_public_key("42", {"public_key": {}})
    assert ei.value.status == 429
    assert ei.value.reset_epoch == reset
    await api.aclose()


@pytest.mark.asyncio
async def test_get_events_requests_documented_fields_and_hyphenates_id():
    seen: Dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["fields"] = request.url.params.get("chat_message_event.fields")
        return httpx.Response(200, json={"data": []})

    api = XChatApi("tok", client=_client(handler))
    await api.get_events("111:999")
    # Colon-form conversation ids are hyphenated for the URL path.
    assert seen["path"] == "/2/chat/conversations/111-999/events"
    requested = set((seen["fields"] or "").split(","))
    assert requested and requested <= _VALID_EVENT_FIELDS
    await api.aclose()


@pytest.mark.asyncio
async def test_media_upload_three_step_flow():
    import base64 as _b64

    calls: List[Dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json

        body = _json.loads(request.content) if request.content else {}
        calls.append({"path": request.url.path, "body": body})
        if request.url.path == "/2/chat/media/upload/initialize":
            # Media bodies take the colon form (X's reference client, chat-xdk examples/python/x_api.py).
            assert body["conversation_id"] == "111:999"
            return httpx.Response(
                200,
                json={"data": {"session_id": "sess1", "media_hash_key": "mhk9",
                               "conversation_id": "111-999"}},
            )
        return httpx.Response(200, json={"data": {}})

    api = XChatApi("tok", client=_client(handler))
    blob = b"E" * (3 * 1024)  # 3 chunks at 1KB chunk size
    out = await api.media_upload("111:999", blob, chunk_size=1024)
    assert out == "mhk9"

    paths = [c["path"] for c in calls]
    assert paths[0] == "/2/chat/media/upload/initialize"
    appends = [c for c in calls if c["path"].endswith("/append")]
    assert len(appends) == 3
    assert [a["body"]["segment_index"] for a in appends] == [0, 1, 2]
    reassembled = b"".join(_b64.b64decode(a["body"]["media"]) for a in appends)
    assert reassembled == blob
    assert paths[-1] == "/2/chat/media/upload/sess1/finalize"
    assert calls[-1]["body"]["num_parts"] == "3"  # required by X's OpenAPI finalize schema
    await api.aclose()


@pytest.mark.asyncio
async def test_media_download_returns_raw_bytes():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/2/chat/media/111-999/mhk9"
        return httpx.Response(200, content=b"\x00ciphertext\xff")

    api = XChatApi("tok", client=_client(handler))
    blob = await api.media_download("111:999", "mhk9")
    assert blob == b"\x00ciphertext\xff"
    await api.aclose()


@pytest.mark.asyncio
async def test_mark_read_posts_sequence_id():
    seen: Dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json

        seen["path"] = request.url.path
        seen["body"] = _json.loads(request.content)
        return httpx.Response(200, json={"data": {}})

    api = XChatApi("tok", client=_client(handler))
    await api.mark_read("111:999", "sq42")
    assert seen["path"] == "/2/chat/conversations/111-999/read"
    assert seen["body"] == {"seen_until_sequence_id": "sq42"}
    await api.aclose()

RT = {"refresh_token": "rt"}


@pytest.mark.asyncio
async def test_401_refresh_is_not_skipped_by_a_live_cached_expiry():
    """A 401 must refresh even while the cached expiry is still in the future.

    The refresh helper skips when the cached expiry is ahead of now, which is
    the normal state for hours after any successful refresh. The reactive 401
    path calls the same helper, so it no-ops and retries with the same bearer.
    """
    calls: List[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/2/oauth2/token":
            return httpx.Response(
                200,
                json={"access_token": "fresh", "expires_in": 7200, **RT},
            )
        auth = request.headers.get("Authorization", "")
        if auth == "Bearer stale":
            return httpx.Response(401, json={"title": "Unauthorized"})
        return httpx.Response(200, json={"data": {"id": "42"}})

    api = XChatApi(
        "stale",
        client_id="cid",
        # ~1h of cached validity left: far outside the refresh slack window.
        token_expires_at=time.time() + 3600,
        client=_client(handler),
        **RT,
    )
    out = await api.get_my_user()
    assert out == {"id": "42"}
    assert calls == ["/2/users/me", "/2/oauth2/token", "/2/users/me"]
    await api.aclose()


@pytest.mark.asyncio
async def test_concurrent_401s_share_one_refresh_grant():
    """Two in-flight requests that both 401 must rotate the token once."""
    import asyncio as _asyncio

    calls: List[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/2/oauth2/token":
            return httpx.Response(
                200,
                json={"access_token": "fresh", "expires_in": 7200, **RT},
            )
        auth = request.headers.get("Authorization", "")
        if auth == "Bearer stale":
            return httpx.Response(401, json={"title": "Unauthorized"})
        return httpx.Response(200, json={"data": {"id": "42"}})

    api = XChatApi(
        "stale",
        client_id="cid",
        token_expires_at=time.time() + 3600,
        client=_client(handler),
        **RT,
    )
    await _asyncio.gather(api.get_my_user(), api.get_my_user())
    assert calls.count("/2/oauth2/token") == 1
    await api.aclose()
