"""Async X API client for the X Chat platform adapter.

A thin httpx-based client for the handful of X API v2 endpoints the adapter
needs. The official ``xdk`` Python client is synchronous (requests-based),
which doesn't fit the async gateway — the Chat endpoints are plain
OAuth2-bearer REST, so direct calls are wire-identical. Only the E2EE layer
needs a real SDK (``chatxdk``, see ``crypto.py``).

Also owns OAuth2 token refresh: X user access tokens expire (~2h). When a
refresh token + client id are configured, :meth:`XChatApi.ensure_token`
renews the access token through ``POST /2/oauth2/token`` and persists the
rotated pair via a caller-supplied callback (X rotates refresh tokens on
every use).
"""
from __future__ import annotations

import asyncio
import base64
import logging
import time
from typing import Any, Awaitable, Callable, Optional

try:
    import httpx
    HTTPX_AVAILABLE = True
except ImportError:  # pragma: no cover - httpx is a core Hermes dependency
    HTTPX_AVAILABLE = False
    httpx = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

BASE_URL = "https://api.x.com"

# Fields we always request on the events endpoint — the decrypt path needs
# encoded_event; sender_id/conversation_id drive session routing. The
# endpoint 400s on unknown fields, so only documented ones may appear here
# (id, conversation_id, conversation_token, created_at, encoded_event,
# is_trusted, previous_id, sender_id, message_event_signature).
_EVENT_FIELDS = (
    "conversation_id,created_at,encoded_event,id,sender_id"
)

# Refresh the access token this many seconds before its reported expiry.
_TOKEN_REFRESH_SLACK = 300


class XChatApiError(Exception):
    """Raised for non-2xx responses from the X API."""

    def __init__(self, status: int, detail: str) -> None:
        self.status = status
        self.detail = detail
        super().__init__(f"X API HTTP {status}: {detail}")


class XChatRateLimited(XChatApiError):
    """HTTP 429 — carries the reset epoch when the API reports one."""

    def __init__(self, detail: str, reset_epoch: Optional[int]) -> None:
        super().__init__(429, detail)
        self.reset_epoch = reset_epoch


class XChatApi:
    """Async client bound to one bot account's OAuth2 user token."""

    def __init__(
        self,
        access_token: str,
        *,
        refresh_token: str = "",
        client_id: str = "",
        client_secret: str = "",
        token_expires_at: float = 0.0,
        on_token_refresh: Optional[Callable[[str, str], Awaitable[None]]] = None,
        base_url: str = BASE_URL,
        client: Optional["httpx.AsyncClient"] = None,
    ) -> None:
        self._access_token = access_token
        self._refresh_token = refresh_token
        self._client_id = client_id
        self._client_secret = client_secret
        # 0 = unknown expiry; refresh only reactively on 401.
        self._token_expires_at = token_expires_at
        self._on_token_refresh = on_token_refresh
        self._base_url = base_url.rstrip("/")
        self._client = client
        self._refresh_lock = asyncio.Lock()

    # -- lifecycle -----------------------------------------------------------

    def _http(self) -> "httpx.AsyncClient":
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=30.0)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:
                pass
            self._client = None

    # -- auth ----------------------------------------------------------------

    @property
    def can_refresh(self) -> bool:
        return bool(self._refresh_token and self._client_id)

    async def ensure_token(self) -> None:
        """Proactively refresh the access token when close to expiry."""
        if not self.can_refresh or not self._token_expires_at:
            return
        if time.time() < self._token_expires_at - _TOKEN_REFRESH_SLACK:
            return
        await self._refresh_access_token()

    async def _refresh_access_token(self) -> None:
        """POST /2/oauth2/token (refresh_token grant). Rotates both tokens."""
        async with self._refresh_lock:
            # Another task may have refreshed while we waited on the lock.
            if self._token_expires_at and time.time() < self._token_expires_at - _TOKEN_REFRESH_SLACK:
                return
            data = {
                "grant_type": "refresh_token",
                "refresh_token": self._refresh_token,
                "client_id": self._client_id,
            }
            auth = None
            if self._client_secret:
                auth = (self._client_id, self._client_secret)
            resp = await self._http().post(
                f"{self._base_url}/2/oauth2/token", data=data, auth=auth
            )
            if resp.status_code >= 300:
                raise XChatApiError(resp.status_code, resp.text[:300])
            tok = resp.json()
            self._access_token = tok.get("access_token") or self._access_token
            # X rotates refresh tokens on every use — always adopt the new one.
            new_refresh = tok.get("refresh_token")
            if new_refresh:
                self._refresh_token = new_refresh
            expires_in = tok.get("expires_in")
            if expires_in:
                self._token_expires_at = time.time() + float(expires_in)
            logger.info("[xchat] OAuth2 access token refreshed")
            if self._on_token_refresh is not None:
                try:
                    await self._on_token_refresh(self._access_token, self._refresh_token)
                except Exception:
                    logger.warning("[xchat] token persist callback failed", exc_info=True)

    # -- request core ----------------------------------------------------------

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict[str, Any]] = None,
        json_body: Optional[dict[str, Any]] = None,
        _retried_auth: bool = False,
    ) -> dict[str, Any]:
        await self.ensure_token()
        headers = {"Authorization": f"Bearer {self._access_token}"}
        resp = await self._http().request(
            method,
            f"{self._base_url}{path}",
            params=params,
            json=json_body,
            headers=headers,
        )
        if resp.status_code == 401 and self.can_refresh and not _retried_auth:
            # Reactive refresh — covers the no-known-expiry case.
            await self._refresh_access_token()
            return await self._request(
                method, path, params=params, json_body=json_body, _retried_auth=True
            )
        if resp.status_code == 429:
            reset = resp.headers.get("x-user-limit-24hour-reset") or resp.headers.get(
                "x-rate-limit-reset"
            )
            raise XChatRateLimited(
                resp.text[:300], int(reset) if reset and reset.isdigit() else None
            )
        if resp.status_code >= 300:
            raise XChatApiError(resp.status_code, resp.text[:300])
        if not resp.content:
            return {}
        try:
            return resp.json()
        except ValueError:
            return {}

    # -- identity ------------------------------------------------------------

    async def get_my_user(self) -> dict[str, Any]:
        """GET /2/users/me — the bot account's own id/username."""
        out = await self._request("GET", "/2/users/me")
        return out.get("data") or {}

    async def get_public_keys(self, user_id: str) -> list[dict[str, Any]]:
        """GET /2/users/{id}/public_keys — a user's registered E2EE keys."""
        out = await self._request("GET", f"/2/users/{user_id}/public_keys")
        data = out.get("data") or []
        return data if isinstance(data, list) else [data]

    async def add_public_key(self, user_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """POST /2/users/{id}/public_keys — register the bot's public keys.

        Rate limited to a handful of writes per 24h; raises
        :class:`XChatRateLimited` on 429 so callers stop instead of burning
        the daily budget.
        """
        return await self._request("POST", f"/2/users/{user_id}/public_keys", json_body=body)

    # -- conversations ---------------------------------------------------------

    @staticmethod
    def _conv_path_id(conversation_id: str) -> str:
        # Events embed the colon form; URL paths take the hyphen form.
        return str(conversation_id).replace(":", "-")

    async def get_conversations(
        self, *, max_results: int = 100, pagination_token: Optional[str] = None
    ) -> dict[str, Any]:
        """GET /2/chat/conversations — list the bot's conversations."""
        params: dict[str, Any] = {
            "max_results": max_results,
            "chat_conversation.fields": "id,newest_event_id",
        }
        if pagination_token:
            params["pagination_token"] = pagination_token
        return await self._request("GET", "/2/chat/conversations", params=params)

    async def get_events(
        self,
        conversation_id: str,
        *,
        max_results: int = 50,
        pagination_token: Optional[str] = None,
    ) -> dict[str, Any]:
        """GET /2/chat/conversations/{id}/events — raw (encrypted) events."""
        params: dict[str, Any] = {
            "max_results": max_results,
            "chat_message_event.fields": _EVENT_FIELDS,
        }
        if pagination_token:
            params["pagination_token"] = pagination_token
        return await self._request(
            "GET",
            f"/2/chat/conversations/{self._conv_path_id(conversation_id)}/events",
            params=params,
        )

    async def send_message(self, conversation_id: str, body: dict[str, str]) -> dict[str, Any]:
        """POST /2/chat/conversations/{id}/messages — send encrypted ciphertext.

        ``body`` is the dict produced by ``XChatCrypto.encrypt_text``. For a
        1:1 conversation ``conversation_id`` may be the recipient's bare user
        id; the server derives the canonical conversation id.
        """
        return await self._request(
            "POST",
            f"/2/chat/conversations/{self._conv_path_id(conversation_id)}/messages",
            json_body=body,
        )

    async def send_typing(self, conversation_id: str) -> None:
        """POST /2/chat/conversations/{id}/typing — best-effort typing indicator."""
        await self._request(
            "POST",
            f"/2/chat/conversations/{self._conv_path_id(conversation_id)}/typing",
        )

    async def add_conversation_keys(
        self, conversation_id: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        """POST /2/chat/conversations/{id}/keys — initialize/rotate a conversation key."""
        return await self._request(
            "POST",
            f"/2/chat/conversations/{self._conv_path_id(conversation_id)}/keys",
            json_body=body,
        )

    async def mark_read(self, conversation_id: str, seen_until_sequence_id: str) -> None:
        """POST /2/chat/conversations/{id}/read — read receipt up to a sequence id."""
        await self._request(
            "POST",
            f"/2/chat/conversations/{self._conv_path_id(conversation_id)}/read",
            json_body={"seen_until_sequence_id": str(seen_until_sequence_id)},
        )

    # -- media (encrypted attachments) ------------------------------------------

    async def media_upload(
        self, conversation_id: str, encrypted_blob: bytes, *, chunk_size: int = 1024 * 1024
    ) -> str:
        """Three-step encrypted-media upload; returns the ``media_hash_key``.

        initialize → append (base64 JSON segments) → finalize. The size
        reported to initialize is the ENCRYPTED blob size. Requires the
        ``media.write`` OAuth scope.
        """
        conv = str(conversation_id).replace("-", ":")
        init = await self._request(
            "POST",
            "/2/chat/media/upload/initialize",
            json_body={"conversation_id": conv, "total_bytes": len(encrypted_blob)},
        )
        data = init.get("data") or {}
        session_id = str(data.get("session_id") or "")
        media_hash_key = str(data.get("media_hash_key") or "")
        if not session_id or not media_hash_key:
            raise XChatApiError(500, f"media upload initialize returned no session: {init}")

        num_parts = (len(encrypted_blob) + chunk_size - 1) // chunk_size
        for index in range(num_parts):
            segment = encrypted_blob[index * chunk_size:(index + 1) * chunk_size]
            await self._request(
                "POST",
                f"/2/chat/media/upload/{session_id}/append",
                json_body={
                    "conversation_id": conv,
                    "media_hash_key": media_hash_key,
                    "segment_index": index,
                    "media": base64.b64encode(segment).decode("ascii"),
                },
            )

        await self._request(
            "POST",
            f"/2/chat/media/upload/{session_id}/finalize",
            json_body={
                "conversation_id": conv,
                "media_hash_key": media_hash_key,
                "num_parts": str(num_parts),
            },
        )
        return media_hash_key

    async def media_download(self, conversation_id: str, media_hash_key: str) -> bytes:
        """GET /2/chat/media/{conversation_id}/{media_hash_key} — encrypted blob.

        The body is read under the gateway's inbound-media cap rather than
        buffered whole: a lying (or absent) ``Content-Length`` must not be able
        to hold an oversized attachment in the gateway's memory. The same
        streaming read core's own platform plugins use.
        """
        from gateway.platforms.base import get_inbound_media_max_bytes, validate_inbound_media_size

        max_bytes = get_inbound_media_max_bytes()
        await self.ensure_token()
        url = f"{self._base_url}/2/chat/media/{self._conv_path_id(conversation_id)}/{media_hash_key}"
        headers = {"Authorization": f"Bearer {self._access_token}"}
        async with self._http().stream("GET", url, headers=headers) as resp:
            if resp.status_code == 401 and self.can_refresh:
                await self._refresh_access_token()
                headers = {"Authorization": f"Bearer {self._access_token}"}
                async with self._http().stream("GET", url, headers=headers) as retry:
                    if retry.status_code >= 300:
                        raise XChatApiError(retry.status_code, (await retry.aread()).decode("utf-8", "replace")[:300])
                    return await self._read_body_with_limit(retry, max_bytes)
            if resp.status_code >= 300:
                raise XChatApiError(resp.status_code, (await resp.aread()).decode("utf-8", "replace")[:300])
            return await self._read_body_with_limit(resp, max_bytes)

    @staticmethod
    async def _read_body_with_limit(response: Any, max_bytes: int) -> bytes:
        """Read a response body under the inbound-media cap, refusing it while reading.

        The declared ``Content-Length`` is checked first, then the running
        total on every chunk, so a lying (or absent) header cannot smuggle more
        than the cap past the check.
        """
        from gateway.platforms.base import validate_inbound_media_size

        content_length = response.headers.get("content-length")
        if content_length:
            try:
                declared_size = int(content_length)
            except ValueError:
                logger.debug("[xchat] ignoring invalid Content-Length for inbound media: %r", content_length)
            else:
                validate_inbound_media_size(declared_size, media_type="attachment", max_bytes=max_bytes)
        chunks: list[bytes] = []
        total = 0
        async for chunk in response.aiter_bytes():
            total += len(chunk)
            validate_inbound_media_size(total, media_type="attachment", max_bytes=max_bytes)
            chunks.append(chunk)
        return b"".join(chunks)
