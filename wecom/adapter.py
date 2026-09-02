"""
WeCom (Enterprise WeChat) platform adapter.

Uses the WeCom AI Bot WebSocket gateway: authenticate via ``aibot_subscribe``,
receive ``aibot_msg_callback`` events, send markdown via ``aibot_send_msg`` /
``aibot_respond_msg``, upload media via ``aibot_upload_media_*``. Native
streaming lives in ``streaming.py``, media in ``media.py``, the per-chat
send queue in ``send_queue.py``.

Configuration in config.yaml:
    platforms:
      wecom:
        enabled: true
        extra:
          bot_id: "your-bot-id"          # or WECOM_BOT_ID env var
          secret: "your-secret"          # or WECOM_SECRET env var
          websocket_url: "wss://openws.work.weixin.qq.com"
          dm_policy: "pairing"           # open | allowlist | disabled | pairing
          allow_from: ["user_id_1"]
          group_policy: "pairing"        # open | allowlist | disabled | pairing
          group_allow_from: ["group_id_1"]
          groups:
            group_id_1:
              allow_from: ["user_id_1"]
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

try:
    import aiohttp
    AIOHTTP_AVAILABLE = True
except ImportError:
    AIOHTTP_AVAILABLE = False
    aiohttp = None  # type: ignore[assignment]

try:
    import httpx
    HTTPX_AVAILABLE = True
except ImportError:
    HTTPX_AVAILABLE = False
    httpx = None  # type: ignore[assignment]

from gateway.config import Platform, PlatformConfig
from gateway.platforms.helpers import MessageDeduplicator
from gateway.platforms.base import gateway_trust_env, BasePlatformAdapter, MessageEvent, MessageType, SendResult
from utils import env_float

from gateway.platforms._shared import get_scoped_secret as _get_scoped_secret
from plugins.platforms.wecom.send_queue import ChatSendQueueMixin
from plugins.platforms.wecom.media import WeComMediaMixin, APP_CMD_SEND
from plugins.platforms.wecom.streaming import (  # noqa: F401 — re-exported for tests/stream_consumer
    WeComStreamMixin, WeComStreamExpiredError, ReplyQueue, StreamTurn, APP_CMD_RESPONSE,
    STREAM_EXPIRED_ERRCODE, STREAM_NOT_SUBSCRIBED_ERRCODE, MAX_STREAM_CONTENT_LENGTH, MAX_INTERMEDIATE_FRAMES,
    STREAM_SAFE_DURATION_SECONDS, STREAM_KEEPALIVE_INTERVAL_SECONDS, STREAM_KEEPALIVE_ENABLED_DEFAULT,
)


logger = logging.getLogger(__name__)

DEFAULT_WS_URL = "wss://openws.work.weixin.qq.com"

APP_CMD_SUBSCRIBE = "aibot_subscribe"
APP_CMD_CALLBACK = "aibot_msg_callback"
APP_CMD_LEGACY_CALLBACK = "aibot_callback"
APP_CMD_EVENT_CALLBACK = "aibot_event_callback"
APP_CMD_PING = "ping"

CALLBACK_COMMANDS = {APP_CMD_CALLBACK, APP_CMD_LEGACY_CALLBACK}
NON_RESPONSE_COMMANDS = CALLBACK_COMMANDS | {APP_CMD_EVENT_CALLBACK}

MAX_MESSAGE_LENGTH = 4000
CONNECT_TIMEOUT_SECONDS = 20.0
REQUEST_TIMEOUT_SECONDS = 15.0
HEARTBEAT_INTERVAL_SECONDS = 30.0
RECONNECT_BACKOFF = [2, 5, 10, 30, 60]

DEDUP_MAX_SIZE = 1000

def check_wecom_requirements() -> bool:
    """Check if WeCom runtime dependencies are available."""
    return AIOHTTP_AVAILABLE and HTTPX_AVAILABLE


def _coerce_list(value: Any) -> List[str]:
    """Coerce config values into a trimmed string list."""
    if value is None:
        return []
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()] if str(value).strip() else []


def _normalize_entry(raw: str) -> str:
    """Normalize allowlist entries such as ``wecom:user:foo``."""
    value = re.sub(r"^wecom:", "", str(raw).strip(), flags=re.IGNORECASE)
    return re.sub(r"^(user|group):", "", value, flags=re.IGNORECASE).strip()


def _entry_matches(entries: List[str], target: str) -> bool:
    """Case-insensitive allowlist match with ``*`` support."""
    normalized_target = str(target).strip().lower()
    return any(_normalize_entry(e).lower() in ("*", normalized_target) for e in entries)


def _dict_or_empty(container: Dict[str, Any], key: str) -> Dict[str, Any]:
    value = container.get(key)
    return value if isinstance(value, dict) else {}


def _content_of(container: Dict[str, Any], key: str) -> str:
    return str(_dict_or_empty(container, key).get("content") or "").strip()


def _bounded_put(store: Dict[str, str], key: str, value: str) -> bool:
    """Insert into an insertion-ordered dict bounded at DEDUP_MAX_SIZE; False if key/value empty."""
    key = str(key or "").strip()
    value = str(value or "").strip()
    if not key or not value:
        return False
    store[key] = value
    while len(store) > DEDUP_MAX_SIZE:
        store.pop(next(iter(store)))
    return True


class WeComAdapter(WeComStreamMixin, WeComMediaMixin, ChatSendQueueMixin, BasePlatformAdapter):
    """WeCom AI Bot adapter backed by a persistent WebSocket connection."""

    MAX_MESSAGE_LENGTH = MAX_MESSAGE_LENGTH
    SUPPORTS_MESSAGE_EDITING = False
    # msgtype "stream" via aibot_respond_msg bypasses the edit-based streaming path.
    SUPPORTS_NATIVE_STREAMING = True
    MAX_STREAM_CONTENT_LENGTH = MAX_STREAM_CONTENT_LENGTH
    # Chunks near the 4000-char WeCom client split are almost certainly continued.
    _SPLIT_THRESHOLD = 3900

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform.WECOM)

        extra = config.extra or {}

        def _extra_float(key: str, default: float) -> float:
            try:
                return float(extra.get(key, default))
            except (TypeError, ValueError):
                return default

        self._bot_id = str(extra.get("bot_id") or _get_scoped_secret("WECOM_BOT_ID", "")).strip()
        self._secret = str(extra.get("secret") or _get_scoped_secret("WECOM_SECRET", "")).strip()
        self._ws_url = str(
            extra.get("websocket_url") or extra.get("websocketUrl") or _get_scoped_secret("WECOM_WEBSOCKET_URL", DEFAULT_WS_URL)
        ).strip() or DEFAULT_WS_URL

        self._dm_policy = str(extra.get("dm_policy") or _get_scoped_secret("WECOM_DM_POLICY", "pairing")).strip().lower()
        # Env-only setups (dm_policy=allowlist via env) need the WECOM_ALLOWED_USERS
        # fallback or every authorized DM is dropped at intake.
        self._allow_from = _coerce_list(
            extra.get("allow_from") or extra.get("allowFrom") or _get_scoped_secret("WECOM_ALLOWED_USERS", "")
        )
        self._group_policy = str(extra.get("group_policy") or _get_scoped_secret("WECOM_GROUP_POLICY", "pairing")).strip().lower()
        self._group_allow_from = _coerce_list(extra.get("group_allow_from") or extra.get("groupAllowFrom"))
        self._groups = extra.get("groups") if isinstance(extra.get("groups"), dict) else {}

        self._session: Optional["aiohttp.ClientSession"] = None
        self._ws: Optional["aiohttp.ClientWebSocketResponse"] = None
        self._http_client: Optional["httpx.AsyncClient"] = None
        self._listen_task: Optional[asyncio.Task] = None
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._pending_responses: Dict[str, asyncio.Future] = {}
        self._reply_queues: Dict[str, ReplyQueue] = {}
        self._dedup = MessageDeduplicator(max_size=DEDUP_MAX_SIZE)
        self._reply_req_ids: Dict[str, str] = {}

        # Text batching: WeCom clients split long messages around 4000 chars.
        self._text_batch_delay_seconds = env_float("HERMES_WECOM_TEXT_BATCH_DELAY_SECONDS", 0.6)
        self._text_batch_split_delay_seconds = env_float("HERMES_WECOM_TEXT_BATCH_SPLIT_DELAY_SECONDS", 2.0)
        # WeCom sends "image + text" as two callbacks a few hundred ms apart; hold an
        # attachment-only message this long so the trailing text merges into ONE
        # event (official plugin: ATTACHMENT_TEXT_MERGE_WINDOW_MS = 800).
        self._attachment_text_merge_delay_seconds = _extra_float("attachment_text_merge_delay_seconds", 0.8)
        self._pending_text_batches: Dict[str, MessageEvent] = {}
        self._pending_text_batch_tasks: Dict[str, asyncio.Task] = {}

        # Stream keep-alive config (see streaming.py STREAM_* constants).
        self._stream_safe_duration_seconds = _extra_float("stream_safe_duration_seconds", STREAM_SAFE_DURATION_SECONDS)
        self._stream_keepalive_enabled = bool(extra.get("stream_keepalive_enabled", STREAM_KEEPALIVE_ENABLED_DEFAULT))
        self._stream_keepalive_interval_seconds = _extra_float(
            "stream_keepalive_interval_seconds", STREAM_KEEPALIVE_INTERVAL_SECONDS
        )

        self._device_id = uuid.uuid4().hex
        self._last_chat_req_ids: Dict[str, str] = {}
        # Per-turn stream state keyed f"{chat_id}:{req_id|turn_id}" so concurrent
        # messages (e.g. approval during streaming) never share a stream.
        self._stream_turns: Dict[str, StreamTurn] = {}
        # Chats whose stream session was retired (846608 / 846609 / no req_id);
        # cleared when a fresh inbound callback gives the chat a new req_id.
        self._stream_expired_chats: set[str] = set()
        # Group chats can't receive proactive APP_CMD_SEND (populated in _on_message).
        self._group_chat_ids: set[str] = set()

        # Per-chat FIFO send queues (normal + control lanes) with token-bucket
        # rate limiting — see send_queue.py.
        self._chat_queues: Dict[str, asyncio.Queue] = {}
        self._chat_workers: Dict[str, asyncio.Task] = {}
        self._control_queues: Dict[str, asyncio.Queue] = {}
        self._control_workers: Dict[str, asyncio.Task] = {}
        self._chat_token_usage: Dict[str, Dict[str, float]] = {}

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        """Connect to the WeCom AI Bot gateway."""
        for available, dep in ((AIOHTTP_AVAILABLE, "aiohttp"), (HTTPX_AVAILABLE, "httpx")):
            if not available:
                message = f"WeCom startup failed: {dep} not installed"
                self._set_fatal_error("wecom_missing_dependency", message, retryable=True)
                logger.warning("[%s] %s. Run: pip install %s", self.name, message, dep)
                return False
        if not self._bot_id or not self._secret:
            message = "WeCom startup failed: WECOM_BOT_ID and WECOM_SECRET are required"
            self._set_fatal_error("wecom_missing_credentials", message, retryable=True)
            logger.warning("[%s] %s", self.name, message)
            return False

        try:
            # Tighter keepalive so idle CLOSE_WAIT drains promptly.
            from gateway.platforms._http_client_limits import platform_httpx_limits
            from gateway.platforms.base import _ssrf_redirect_guard
            from tools.url_safety import create_ssrf_safe_async_client

            self._http_client = create_ssrf_safe_async_client(
                timeout=30.0,
                follow_redirects=True,
                event_hooks={"response": [_ssrf_redirect_guard]},
                limits=platform_httpx_limits(),
            )
            await self._open_connection()
            self._mark_connected()
            self._listen_task = asyncio.create_task(self._listen_loop())
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
            logger.info("[%s] Connected to %s", self.name, self._ws_url)
            self._wire_plugin_handlers(None)  # ctx.register_platform_handler hooks
            return True
        except Exception as exc:
            message = f"WeCom startup failed: {exc}"
            self._set_fatal_error("wecom_connect_error", message, retryable=True)
            logger.error("[%s] Failed to connect: %s", self.name, exc, exc_info=True)
            await self._cleanup_ws()
            await self._close_http_client()
            return False

    async def _close_http_client(self) -> None:
        if self._http_client:
            await self._http_client.aclose()
            self._http_client = None

    async def disconnect(self) -> None:
        """Disconnect from WeCom."""
        self._running = False
        self._mark_disconnected()
        for task in list(self._chat_workers.values()) + list(self._control_workers.values()):
            task.cancel()
        for registry in (self._chat_workers, self._control_workers, self._chat_queues, self._control_queues):
            registry.clear()

        for attr in ("_listen_task", "_heartbeat_task"):
            task = getattr(self, attr)
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            setattr(self, attr, None)

        self._fail_pending_responses(RuntimeError("WeCom adapter disconnected"))
        self._fail_reply_queues(RuntimeError("WeCom adapter disconnected"))
        await self._cleanup_ws()
        await self._close_http_client()
        self._dedup.clear()
        logger.info("[%s] Disconnected", self.name)

    async def _cleanup_ws(self) -> None:
        """Close the live websocket/session, if any."""
        if self._ws and not self._ws.closed:
            await self._ws.close()
        self._ws = None
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    async def _open_connection(self) -> None:
        """Open and authenticate a websocket connection."""
        await self._cleanup_ws()
        # certifi's CA bundle so aiohttp trusts the same roots as urllib/requests
        # (avoids SSL_CERTIFICATE_VERIFY_FAILED on macOS with a stale OpenSSL path).
        import ssl as _ssl
        try:
            import certifi
            _ssl_ctx = _ssl.create_default_context(cafile=certifi.where())
        except ImportError:
            _ssl_ctx = _ssl.create_default_context()
        self._session = aiohttp.ClientSession(trust_env=gateway_trust_env(), connector=aiohttp.TCPConnector(ssl=_ssl_ctx))
        self._ws = await self._session.ws_connect(
            self._ws_url, heartbeat=HEARTBEAT_INTERVAL_SECONDS * 2, timeout=CONNECT_TIMEOUT_SECONDS,
        )

        req_id = self._new_req_id("subscribe")
        await self._send_json({
            "cmd": APP_CMD_SUBSCRIBE,
            "headers": {"req_id": req_id},
            "body": {"bot_id": self._bot_id, "secret": self._secret, "device_id": self._device_id},
        })
        auth_payload = await self._wait_for_handshake(req_id)
        errcode = auth_payload.get("errcode", 0)
        if errcode not in {0, None}:
            errmsg = auth_payload.get("errmsg", "authentication failed")
            raise RuntimeError(f"{errmsg} (errcode={errcode})")

    async def _wait_for_handshake(self, req_id: str) -> Dict[str, Any]:
        """Wait for the subscribe acknowledgement."""
        if not self._ws:
            raise RuntimeError("WebSocket not initialized")
        deadline = asyncio.get_running_loop().time() + CONNECT_TIMEOUT_SECONDS
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError("Timed out waiting for WeCom subscribe acknowledgement")
            msg = await asyncio.wait_for(self._ws.receive(), timeout=remaining)
            if msg.type == aiohttp.WSMsgType.TEXT:
                payload = self._parse_json(msg.data)
                if not payload or payload.get("cmd") == APP_CMD_PING:
                    continue
                if self._payload_req_id(payload) == req_id:
                    return payload
                logger.debug("[%s] Ignoring pre-auth payload: %s", self.name, payload.get("cmd"))
            elif msg.type in {aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.ERROR}:
                raise RuntimeError("WeCom websocket closed during authentication")

    async def _listen_loop(self) -> None:
        """Read websocket events forever, reconnecting on errors."""
        backoff_idx = 0
        while self._running:
            try:
                await self._read_events()
                backoff_idx = 0
            except asyncio.CancelledError:
                return
            except Exception as exc:
                if not self._running:
                    return
                logger.warning("[%s] WebSocket error: %s", self.name, exc)
                self._fail_pending_responses(RuntimeError("WeCom connection interrupted"))
                self._fail_reply_queues(RuntimeError("WeCom connection interrupted"))
                delay = RECONNECT_BACKOFF[min(backoff_idx, len(RECONNECT_BACKOFF) - 1)]
                backoff_idx += 1
                await asyncio.sleep(delay)
                try:
                    await self._open_connection()
                    backoff_idx = 0
                    self._mark_connected()
                    logger.info("[%s] Reconnected", self.name)
                except Exception as reconnect_exc:
                    logger.warning("[%s] Reconnect failed: %s", self.name, reconnect_exc)

    async def _read_events(self) -> None:
        """Read websocket frames until the connection closes."""
        if not self._ws:
            raise RuntimeError("WebSocket not connected")
        while self._running and self._ws and not self._ws.closed:
            msg = await self._ws.receive()
            if msg.type in (aiohttp.WSMsgType.TEXT, aiohttp.WSMsgType.BINARY):
                is_binary = msg.type == aiohttp.WSMsgType.BINARY
                data_len = len(msg.data) if isinstance(msg.data, (str, bytes, bytearray)) else -1
                if is_binary:
                    # WeCom is expected to send TEXT; log a decoded preview so an
                    # unhandled transport for group messages isn't silently discarded.
                    try:
                        decoded = msg.data.decode("utf-8", errors="replace")
                    except Exception:
                        decoded = "<undecodable>"
                    logger.info(
                        "[%s] Inbound BINARY frame received (len=%d) head=%r — attempting JSON parse",
                        self.name, data_len, decoded[:200],
                    )
                payload = self._parse_json(msg.data)
                if payload:
                    await self._dispatch_payload(payload)
                elif is_binary:
                    logger.info("[%s] BINARY frame not parseable as JSON — dropped", self.name)
                else:
                    # _parse_json logged the detail; make the DROP itself visible at
                    # INFO so a missing inbound message can be correlated to a bad frame.
                    logger.info("[%s] Inbound TEXT frame dropped (unparseable/non-dict) len=%d", self.name, data_len)
            elif msg.type in {aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSING}:
                raise RuntimeError("WeCom websocket closed")
            else:
                logger.info("[%s] Inbound frame ignored: WSMsgType=%s", self.name, msg.type)

    async def _heartbeat_loop(self) -> None:
        """Send lightweight application-level pings."""
        try:
            while self._running:
                await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)
                if not self._ws or self._ws.closed:
                    continue
                try:
                    await self._send_json({"cmd": APP_CMD_PING, "headers": {"req_id": self._new_req_id("ping")}, "body": {}})
                except Exception as exc:
                    logger.debug("[%s] Heartbeat send failed: %s", self.name, exc)
        except asyncio.CancelledError:
            pass

    async def _dispatch_payload(self, payload: Dict[str, Any]) -> None:
        """Route inbound websocket payloads."""
        req_id = self._payload_req_id(payload)
        cmd = str(payload.get("cmd") or "")
        body_dict = payload.get("body") if isinstance(payload.get("body"), dict) else None

        # Diagnostics for ack-timeout analysis: do WeCom acks arrive at all, and
        # under which cmd?
        if self._reply_queues and cmd != APP_CMD_PING:
            logger.debug(
                "[%s] _dispatch_payload[ALL]: req_id=%s cmd=%r active_queues=%s",
                self.name, req_id or "(none)", cmd or "(empty)",
                list(self._reply_queues.keys()),
            )
        if req_id and self._reply_queues.get(req_id):
            logger.debug(
                "[%s] _dispatch_payload: req_id=%s cmd=%r has_pending_ack=%s "
                "errcode=%s in_NON_RESPONSE=%s payload_keys=%s",
                self.name, req_id, cmd, self._reply_queues[req_id].pending_ack is not None,
                body_dict.get("errcode", "N/A") if body_dict is not None else "N/A",
                cmd in NON_RESPONSE_COMMANDS,
                list(payload.keys()),
            )

        # aibot_respond_msg acks arrive with the inbound req_id and no/other cmd.
        # Reply-queue acks MUST be checked before _pending_responses so the
        # _send_reply_request path can't steal them.
        if req_id and cmd not in NON_RESPONSE_COMMANDS:
            if self._resolve_reply_ack(req_id, payload):
                return
            if req_id in self._pending_responses:
                future = self._pending_responses.get(req_id)
                if future and not future.done():
                    future.set_result(payload)
                return

        if cmd in CALLBACK_COMMANDS:
            await self._on_message(payload)
            return
        if cmd == APP_CMD_PING:
            return
        if cmd == APP_CMD_EVENT_CALLBACK:
            # "Kicked by server": another connection was established elsewhere.
            # Mirror the official SDK — suppress reconnect to avoid mutual kicking.
            body = payload.get("body") or {}
            if str(body.get("event_type") or "") == "disconnected_event":
                logger.warning(
                    "[%s] Kicked by server (another WS connection established). "
                    "Suppressing reconnect to avoid mutual kicking. "
                    "Check for duplicate gateway instances.",
                    self.name,
                )
                self._running = False
            return

        # Unrouted: if WeCom delivered group messages under an unknown cmd they
        # would land here, so log cmd + body keys at INFO.
        logger.info(
            "[%s] Unrouted websocket payload dropped: cmd=%r req_id=%s body_keys=%s",
            self.name, cmd or "(empty)", req_id or "(none)",
            list(body_dict.keys()) if body_dict is not None else None,
        )

    def _fail_pending_responses(self, exc: Exception) -> None:
        """Fail all outstanding request futures."""
        for req_id, future in list(self._pending_responses.items()):
            if not future.done():
                future.set_exception(exc)
            self._pending_responses.pop(req_id, None)

    async def _send_json(self, payload: Dict[str, Any]) -> None:
        """Send a raw JSON frame over the active websocket."""
        if not self._ws or self._ws.closed:
            raise RuntimeError("WeCom websocket is not connected")
        await self._ws.send_json(payload)

    async def _request(self, cmd: str, req_id: str, body: Dict[str, Any], timeout: float) -> Dict[str, Any]:
        future = asyncio.get_running_loop().create_future()
        self._pending_responses[req_id] = future
        try:
            await self._send_json({"cmd": cmd, "headers": {"req_id": req_id}, "body": body})
            return await asyncio.wait_for(future, timeout=timeout)
        finally:
            self._pending_responses.pop(req_id, None)

    async def _send_request(self, cmd: str, body: Dict[str, Any], timeout: float = REQUEST_TIMEOUT_SECONDS) -> Dict[str, Any]:
        """Send a JSON request and await the correlated response."""
        if not self._ws or self._ws.closed:
            raise RuntimeError("WeCom websocket is not connected")
        return await self._request(cmd, self._new_req_id(cmd), body, timeout)

    async def _send_reply_request(
        self, reply_req_id: str, body: Dict[str, Any], cmd: str = APP_CMD_RESPONSE, timeout: float = REQUEST_TIMEOUT_SECONDS,
    ) -> Dict[str, Any]:
        """Send a reply frame correlated to an inbound callback req_id."""
        if not self._ws or self._ws.closed:
            raise RuntimeError("WeCom websocket is not connected")
        normalized_req_id = str(reply_req_id or "").strip()
        if not normalized_req_id:
            raise ValueError("reply_req_id is required")
        return await self._request(cmd, normalized_req_id, body, timeout)

    @staticmethod
    def _new_req_id(prefix: str) -> str:
        return f"{prefix}-{uuid.uuid4().hex}"

    @staticmethod
    def _payload_req_id(payload: Dict[str, Any]) -> str:
        headers = payload.get("headers")
        return str(headers.get("req_id") or "") if isinstance(headers, dict) else ""

    @staticmethod
    def _parse_json(raw: Any) -> Optional[Dict[str, Any]]:
        raw_len = len(raw) if isinstance(raw, (str, bytes)) else -1
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            # WeCom sometimes sends unescaped control chars (raw newlines) inside
            # JSON strings; strict=False accepts them.
            try:
                text = raw if isinstance(raw, str) else raw.decode("utf-8", errors="replace")
                payload = json.JSONDecoder(strict=False).decode(text)
                logger.info("WeCom payload required strict=False fallback (len=%d)", raw_len)
            except Exception as exc2:
                logger.warning(
                    "Failed to parse WeCom payload (strict=False also failed): "
                    "error=%s len=%d tail=%r",
                    exc2, raw_len,
                    raw[-100:] if isinstance(raw, (str, bytes)) and len(raw) > 100 else raw,
                )
                return None
        except Exception as exc:
            logger.warning("Failed to parse WeCom payload: error=%s len=%d", exc, raw_len)
            return None
        return payload if isinstance(payload, dict) else None

    # ------------------------------------------------------------------
    # Inbound message parsing
    # ------------------------------------------------------------------

    async def _on_message(self, payload: Dict[str, Any]) -> None:
        """Process an inbound WeCom message callback event."""
        body = payload.get("body")
        if not isinstance(body, dict):
            return
        req_id = self._payload_req_id(payload)
        msg_id = str(body.get("msgid") or req_id or uuid.uuid4().hex)
        sender = body.get("from") if isinstance(body.get("from"), dict) else {}
        sender_id = str(sender.get("userid") or "").strip()
        if self._dedup.is_duplicate(msg_id):
            # INFO (not debug): is_duplicate marks at check time, so a msgid
            # redelivered after a processing exception is dropped for the TTL —
            # a top suspect for intermittent group non-replies.
            logger.info(
                "[%s] Duplicate message %s ignored (dedup drop) req_id=%s sender=%r chattype=%r",
                self.name, msg_id, req_id, sender.get("userid") if sender else None, body.get("chattype"),
            )
            return
        _bounded_put(self._reply_req_ids, msg_id, req_id)

        chat_id = str(body.get("chatid") or sender_id).strip()
        # Shape of every inbound callback at INFO: group frames may arrive with
        # a chattype other than the literal "group".
        logger.info(
            "[%s] Inbound callback: chattype=%r chatid=%r sender=%r msgtype=%r has_chatid=%s",
            self.name, body.get("chattype"), body.get("chatid"), sender_id, body.get("msgtype"), bool(body.get("chatid")),
        )
        if not chat_id:
            logger.info("[%s] Missing chat id, skipping message; body_keys=%s", self.name, list(body.keys()))
            return

        is_group = str(body.get("chattype") or "").lower() == "group"
        if is_group:
            self._group_chat_ids.add(chat_id)
            if not self._is_group_allowed(chat_id, sender_id):
                logger.info(
                    "[%s] Group message DROPPED by policy: chat=%s sender=%s group_policy=%r "
                    "(set group_policy to 'open' or add to group_allow_from to receive)",
                    self.name, chat_id, sender_id, self._group_policy,
                )
                return
        elif not self._is_dm_intake_allowed(sender_id):
            logger.info("[%s] DM sender %s blocked by policy", self.name, sender_id)
            return

        # After policy checks: cache the req_id so proactive sends can fall back to
        # APP_CMD_RESPONSE (required for groups, where APP_CMD_SEND is blocked).
        self._remember_chat_req_id(chat_id, req_id)

        text, reply_text = self._extract_text(body)
        if is_group and text:
            # Strip leading @mention so "@Bot /approve" is recognized as "/approve".
            text = re.sub(r"^@\S+\s*", "", text).strip()
        media_urls, media_types = await self._extract_media(body)
        message_type = self._derive_message_type(body, text, media_types)
        has_reply_context = bool(reply_text and (text or media_urls))
        if not text and reply_text and not media_urls:
            text = reply_text
        if not text and not media_urls:
            logger.info(
                "[%s] Empty WeCom message skipped: is_group=%s chat=%s msgtype=%r",
                self.name, is_group, chat_id, body.get("msgtype"),
            )
            return

        source = self.build_source(
            chat_id=chat_id, chat_type="group" if is_group else "dm", user_id=sender_id or None, user_name=sender_id or None,
        )
        event = MessageEvent(
            text=text, message_type=message_type, source=source, raw_message=payload, message_id=msg_id,
            media_urls=media_urls, media_types=media_types,
            reply_to_message_id=f"quote:{msg_id}" if has_reply_context else None,
            reply_to_text=reply_text if has_reply_context else None,
            timestamp=datetime.now(tz=timezone.utc),
        )

        # Only plain text is batched (commands/media aren't split by the client),
        # EXCEPT an attachment-only message, which is held for the merge window so
        # the trailing text callback merges into the same event instead of
        # "interrupting" a run the attachment already spawned.
        has_pending_batch = self._text_batch_key(event) in self._pending_text_batches
        is_attachment_only = bool(media_urls) and not (text or "").strip()
        if message_type == MessageType.TEXT and (self._text_batch_delay_seconds > 0 or has_pending_batch):
            self._enqueue_text_event(event)
        elif is_attachment_only and self._attachment_text_merge_delay_seconds > 0:
            self._enqueue_text_event(event)
        else:
            await self.handle_message(event)

    # ------------------------------------------------------------------
    # Text message aggregation (handles WeCom client-side splits)
    # ------------------------------------------------------------------

    def _enqueue_text_event(self, event: MessageEvent) -> None:
        """Buffer an event and reset the flush timer.

        Merges both 4000-char client splits and the "attachment-only frame, then
        text frame" pair: once real text joins a buffered attachment the type is
        promoted to TEXT (and it inherits the text frame's quote context).
        """
        key = self._text_batch_key(event)
        existing = self._pending_text_batches.get(key)
        chunk_len = len(event.text or "")
        if existing is None:
            event._last_chunk_len = chunk_len  # type: ignore[attr-defined]
            self._pending_text_batches[key] = event
        else:
            if event.text:
                existing.text = f"{existing.text}\n{event.text}" if existing.text else event.text
            existing._last_chunk_len = chunk_len  # type: ignore[attr-defined]
            if event.media_urls:
                existing.media_urls.extend(event.media_urls)
                existing.media_types.extend(event.media_types)
            if event.text and (event.text or "").strip():
                existing.message_type = MessageType.TEXT
                if event.reply_to_text and not existing.reply_to_text:
                    existing.reply_to_text = event.reply_to_text
                    existing.reply_to_message_id = event.reply_to_message_id

        prior_task = self._pending_text_batch_tasks.get(key)
        if prior_task and not prior_task.done():
            prior_task.cancel()
        self._pending_text_batch_tasks[key] = asyncio.create_task(self._flush_text_batch(key))

    async def _flush_text_batch(self, key: str) -> None:
        """Wait for the quiet period then dispatch the aggregated text."""
        current_task = asyncio.current_task()
        try:
            pending = self._pending_text_batches.get(key)
            last_len = getattr(pending, "_last_chunk_len", 0) if pending else 0
            if pending and pending.media_urls and not (pending.text or "").strip():
                delay = self._attachment_text_merge_delay_seconds  # attachment-only: wait for text
            elif last_len >= self._SPLIT_THRESHOLD:
                delay = self._text_batch_split_delay_seconds  # continuation almost certain
            else:
                delay = self._text_batch_delay_seconds
            await asyncio.sleep(delay)
            # Cancel-delivery race: if the sleep timer fired just before cancel(),
            # CancelledError is delivered at the NEXT await, after we'd have popped
            # the merged event — so the superseding task would find nothing.
            # This check is synchronous (no await between it and the pop).
            if self._pending_text_batch_tasks.get(key) is not current_task:
                return
            event = self._pending_text_batches.pop(key, None)
            if not event:
                return
            logger.info(
                "[WeCom] Flushing batch %s (%d chars, %d media)",
                key, len(event.text or ""), len(event.media_urls or []),
            )
            await self.handle_message(event)
        finally:
            if self._pending_text_batch_tasks.get(key) is current_task:
                self._pending_text_batch_tasks.pop(key, None)

    @staticmethod
    def _extract_text(body: Dict[str, Any]) -> Tuple[str, Optional[str]]:
        """Extract plain text and quoted text from a callback payload."""
        text_parts: List[str] = []
        msgtype = str(body.get("msgtype") or "").lower()
        if msgtype == "mixed":
            items = _dict_or_empty(body, "mixed").get("msg_item")
            for item in items if isinstance(items, list) else []:
                if isinstance(item, dict) and str(item.get("msgtype") or "").lower() == "text":
                    content = _content_of(item, "text")
                    if content:
                        text_parts.append(content)
        else:
            text_parts.append(_content_of(body, "text"))
            if msgtype == "voice":
                text_parts.append(_content_of(body, "voice"))
            if msgtype == "appmsg":  # attachment title (filename)
                text_parts.append(str(_dict_or_empty(body, "appmsg").get("title") or "").strip())

        quote = _dict_or_empty(body, "quote")
        quote_type = str(quote.get("msgtype") or "").lower()
        reply_text = _content_of(quote, quote_type) or None if quote_type in ("text", "voice") else None
        return "\n".join(part for part in text_parts if part).strip(), reply_text

    @staticmethod
    def _derive_message_type(body: Dict[str, Any], text: str, media_types: List[str]) -> MessageType:
        """Choose the normalized inbound message type."""
        if any(mtype.startswith(("application/", "text/")) for mtype in media_types):
            return MessageType.DOCUMENT
        if any(mtype.startswith("image/") for mtype in media_types):
            return MessageType.TEXT if text else MessageType.PHOTO
        if str(body.get("msgtype") or "").lower() == "voice":
            return MessageType.VOICE
        return MessageType.TEXT

    # ------------------------------------------------------------------
    # Policy helpers
    # ------------------------------------------------------------------

    @property
    def enforces_own_access_policy(self) -> bool:
        """WeCom gates DM/group access at intake via dm_policy/group_policy."""
        return True

    def _open_dm_opted_in(self) -> bool:
        # Scoped reads: the default profile's allow-all flag must not leak into a
        # multiplexed secondary profile's admission gate.
        return any(
            (_get_scoped_secret(var, "") or "").lower() in {"true", "1", "yes"}
            for var in ("GATEWAY_ALLOW_ALL_USERS", "WECOM_ALLOW_ALL_USERS")
        )

    def _is_dm_allowed(self, sender_id: str) -> bool:
        if self._dm_policy == "allowlist":
            return _entry_matches(self._allow_from, sender_id)
        if self._dm_policy == "open":
            return self._open_dm_opted_in()
        return False

    def _is_dm_intake_allowed(self, sender_id: str) -> bool:
        principal = str(sender_id or "").strip()
        if not principal:
            return False
        if self._dm_policy == "pairing":
            return True
        return self._is_dm_allowed(principal)

    def _is_group_allowed(self, chat_id: str, sender_id: str) -> bool:
        if self._group_policy in ("disabled", "pairing"):
            return False
        if self._group_policy == "allowlist" and not _entry_matches(self._group_allow_from, chat_id):
            return False
        group_cfg = self._resolve_group_cfg(chat_id)
        sender_allow = _coerce_list(group_cfg.get("allow_from") or group_cfg.get("allowFrom"))
        return _entry_matches(sender_allow, sender_id) if sender_allow else True

    def _resolve_group_cfg(self, chat_id: str) -> Dict[str, Any]:
        if not isinstance(self._groups, dict):
            return {}
        if isinstance(self._groups.get(chat_id), dict):
            return self._groups[chat_id]
        lowered = chat_id.lower()
        for key, value in self._groups.items():
            if isinstance(key, str) and key.lower() == lowered and isinstance(value, dict):
                return value
        wildcard = self._groups.get("*")
        return wildcard if isinstance(wildcard, dict) else {}

    def _remember_chat_req_id(self, chat_id: str, req_id: str) -> None:
        """Cache the most recent inbound req_id per chat (bounded like _reply_req_ids).

        Fallback reply target for group sends (APP_CMD_SEND is blocked in groups).
        A fresh req_id also resurrects the chat's stream channel.
        """
        if _bounded_put(self._last_chat_req_ids, chat_id, req_id):
            self._stream_expired_chats.discard(str(chat_id).strip())

    def _reply_req_id_for_message(self, reply_to: Optional[str]) -> Optional[str]:
        normalized = str(reply_to or "").strip()
        if not normalized or normalized.startswith("quote:"):
            return None
        return self._reply_req_ids.get(normalized)

    async def _force_reconnect_on_stale_subscription(self, errcode: int) -> None:
        """On 846609 (subscription lost) invalidate cached req_ids bound to the dead session.

        Do NOT close the WS: that makes _listen_loop open a second connection,
        WeCom kicks it and invalidates the first — an infinite kick-reconnect
        loop. The server closes the WS itself and _listen_loop reconnects then.
        """
        if errcode != STREAM_NOT_SUBSCRIBED_ERRCODE:
            return
        logger.warning("[%s] Got errcode %d (subscription lost) — clearing stale state", self.name, errcode)
        self._last_chat_req_ids.clear()
        self._reply_req_ids.clear()

    # ------------------------------------------------------------------
    # Outbound messaging
    # ------------------------------------------------------------------

    @staticmethod
    def _response_error(response: Dict[str, Any]) -> Optional[str]:
        errcode = response.get("errcode", 0)
        if errcode in {0, None}:
            return None
        return f"WeCom errcode {errcode}: {response.get('errmsg') or 'unknown error'}"

    @classmethod
    def _raise_for_wecom_error(cls, response: Dict[str, Any], operation: str) -> None:
        error = cls._response_error(response)
        if error:
            raise RuntimeError(f"{operation} failed: {error}")

    async def _send_reply_markdown(self, reply_req_id: str, content: str) -> Dict[str, Any]:
        response = await self._send_reply_request(
            reply_req_id, {"msgtype": "markdown", "markdown": {"content": content[:self.MAX_MESSAGE_LENGTH]}},
        )
        self._raise_for_wecom_error(response, "send reply markdown")
        return response

    async def _send_proactive_markdown(self, chat_id: str, content: str) -> Dict[str, Any]:
        return await self._send_request(
            APP_CMD_SEND,
            {"chatid": chat_id, "msgtype": "markdown", "markdown": {"content": content[:self.MAX_MESSAGE_LENGTH]}},
        )

    async def send(
        self, chat_id: str, content: str, reply_to: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """Send markdown to a WeCom chat as a standalone message (never touches active streams).

        Serialized per chat to stay under 30 msgs/min/chat (errcode 846607).
        ``metadata["is_approval_prompt"]`` routes through the control lane.
        """
        if not chat_id:
            return SendResult(success=False, error="chat_id is required")
        is_control = False
        force_proactive = False
        if metadata:
            is_control = metadata.pop("is_approval_prompt", False)
            # Approval *confirmations* must not consume the req_id the stream
            # consumer needs for resumed output. The initial approval *prompt*
            # still uses passive reply (required in groups).
            force_proactive = bool(metadata.pop("force_proactive_send", False))
        return await self._enqueue_chat_send(
            chat_id,
            lambda: self._send_inner(chat_id, content, reply_to, force_proactive=force_proactive),
            is_control=is_control,
        )

    async def _send_inner(
        self, chat_id: str, content: str, reply_to: Optional[str] = None, *, force_proactive: bool = False,
    ) -> SendResult:
        """Actual send logic, run under the per-chat queue.

        force_proactive: always use APP_CMD_SEND instead of passive reply
        (except in groups, where APP_CMD_SEND is blocked).
        """
        try:
            reply_req_id = self._reply_req_id_for_message(reply_to)
            if not reply_req_id and chat_id in self._last_chat_req_ids:
                reply_req_id = self._last_chat_req_ids[chat_id]
            if force_proactive and chat_id not in self._group_chat_ids:
                reply_req_id = None

            if reply_req_id:
                try:
                    response = await self._send_reply_markdown(reply_req_id, content)
                except (asyncio.TimeoutError, RuntimeError) as passive_err:
                    # req_id may be stale after a WS reconnect — proactive send
                    # doesn't depend on any prior req_id.
                    logger.warning(
                        "[%s] Passive reply failed (%s), falling back to proactive send",
                        self.name, passive_err,
                    )
                    response = await self._send_proactive_markdown(chat_id, content)
            else:
                if chat_id in self._group_chat_ids:
                    logger.warning(
                        "[%s] No cached req_id for group chat %s — "
                        "cannot send (groups require passive reply via req_id)",
                        self.name, chat_id,
                    )
                    return SendResult(success=False, error="No req_id available for group chat (passive reply required)")
                response = await self._send_proactive_markdown(chat_id, content)
        except asyncio.TimeoutError:
            return SendResult(success=False, error="Timeout sending message to WeCom")
        except Exception as exc:
            logger.error("[%s] Send failed: %s", self.name, exc)
            # 846609 (subscription lost): clear stale req_ids so later sends don't
            # fail for minutes while the dead WS lingers.
            if str(STREAM_NOT_SUBSCRIBED_ERRCODE) in str(exc):
                asyncio.ensure_future(self._force_reconnect_on_stale_subscription(STREAM_NOT_SUBSCRIBED_ERRCODE))
            return SendResult(success=False, error=str(exc))

        error = self._response_error(response)
        if error:
            errcode = response.get("errcode", 0)
            if errcode == STREAM_NOT_SUBSCRIBED_ERRCODE:
                asyncio.ensure_future(self._force_reconnect_on_stale_subscription(errcode))
            return SendResult(success=False, error=error)
        return SendResult(
            success=True,
            message_id=self._payload_req_id(response) or uuid.uuid4().hex[:12],
            raw_response=response,
        )

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        """Return minimal chat info."""
        return {"name": chat_id, "type": "group" if chat_id and chat_id.lower().startswith("group") else "dm"}


# ------------------------------------------------------------------
# QR code scan flow for obtaining bot credentials
# ------------------------------------------------------------------

_QR_GENERATE_URL = "https://work.weixin.qq.com/ai/qc/generate"
_QR_QUERY_URL = "https://work.weixin.qq.com/ai/qc/query_result"
_QR_CODE_PAGE = "https://work.weixin.qq.com/ai/qc/gen?source=hermes&scode="
_QR_POLL_INTERVAL = 3  # seconds
_QR_POLL_TIMEOUT = 300  # 5 minutes


def qr_scan_for_bot_info(*, timeout_seconds: int = _QR_POLL_TIMEOUT) -> Optional[Dict[str, str]]:
    """Fetch a WeCom QR code, render it in the terminal, poll until scanned or timeout.

    Returns ``{"bot_id", "secret"}`` or None. The ``ai/qc/{generate,query_result}``
    endpoints back the admin-console bot-creation UI, not the public API, and
    may change without notice.
    """
    import urllib.request
    import urllib.parse

    def _get_json(url: str, timeout: int) -> Dict[str, Any]:
        req = urllib.request.Request(url, headers={"User-Agent": "HermesAgent/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    print("  Connecting to WeCom...", end="", flush=True)
    try:
        raw = _get_json(f"{_QR_GENERATE_URL}?source=hermes", 15)
    except Exception as exc:
        logger.error("WeCom QR: failed to fetch QR code: %s", exc)
        print(f" failed: {exc}")
        return None
    data = raw.get("data") or {}
    scode = str(data.get("scode") or "").strip()
    auth_url = str(data.get("auth_url") or "").strip()
    if not scode or not auth_url:
        logger.error("WeCom QR: unexpected response format: %s", raw)
        print(" failed: unexpected response format")
        return None
    print(" done.")

    print()
    qr_rendered = False
    try:
        import qrcode as _qrcode
        qr = _qrcode.QRCode()
        qr.add_data(auth_url)
        qr.make(fit=True)
        qr.print_ascii(invert=True)
        qr_rendered = True
    except Exception:
        pass
    page_url = f"{_QR_CODE_PAGE}{urllib.parse.quote(scode)}"
    if qr_rendered:
        print(f"\n  Scan the QR code above, or open this URL directly:\n  {page_url}")
    else:
        print(f"  Open this URL in WeCom on your phone:\n\n  {page_url}\n")
        print("  Tip: pip install qrcode  to display a scannable QR code here next time")
    print()
    print("  Fetching configuration results...", end="", flush=True)

    deadline = time.monotonic() + timeout_seconds
    query_url = f"{_QR_QUERY_URL}?scode={urllib.parse.quote(scode)}"
    while time.monotonic() < deadline:
        try:
            result = _get_json(query_url, 10)
        except Exception as exc:
            logger.debug("WeCom QR poll error: %s", exc)
            time.sleep(_QR_POLL_INTERVAL)
            continue
        print(".", end="", flush=True)  # progress dot on every poll
        result_data = result.get("data") or {}
        if str(result_data.get("status") or "").lower() == "success":
            print()
            bot_info = result_data.get("bot_info") or {}
            bot_id = str(bot_info.get("botid") or bot_info.get("bot_id") or "").strip()
            secret = str(bot_info.get("secret") or "").strip()
            if bot_id and secret:
                return {"bot_id": bot_id, "secret": secret}
            logger.warning("WeCom QR: scan reported success but bot_info missing or incomplete: %s", result_data)
            print(
                "  QR scan reported success but no bot credentials were returned.\n"
                "  This usually means the bot was not actually created on the WeCom side.\n"
                "  Falling back to manual credential entry."
            )
            return None
        time.sleep(_QR_POLL_INTERVAL)

    print()
    print(f"  QR scan timed out ({timeout_seconds // 60} minutes). Please try again.")
    return None


# ──────────────────────────────────────────────────────────────────────────
# Plugin glue: register() exposes both WeCom platforms (wecom + wecom_callback)
# via the registry; env→PlatformConfig seeding stays in core.
# ──────────────────────────────────────────────────────────────────────────


async def _send_via(adapter, chat_id, message, *, live: bool):
    try:
        result = await adapter.send(chat_id, message)
    except Exception as e:
        return {"error": f"WeCom live adapter send failed: {e}" if live else f"WeCom send failed: {e}"}
    if not result.success:
        return {"error": f"WeCom send failed: {result.error}"}
    return {"success": True, "platform": "wecom", "chat_id": chat_id, "message_id": result.message_id}


async def _standalone_send(pconfig, chat_id, message, *, thread_id=None, media_files=None, force_document=False):
    """standalone_sender_fn: reuse the live gateway adapter when in-process, else
    open an ephemeral connection. WeCom allows ONE WebSocket per bot — a second
    connection kicks the first."""
    try:
        from gateway.run import _gateway_runner_ref
        runner = _gateway_runner_ref()
    except Exception:
        runner = None
    if runner is not None:
        from gateway.platforms.base import Platform
        adapter = None
        try:
            adapter = runner.adapters.get(Platform.WECOM)
        except Exception:
            pass
        if adapter is not None:
            return await _send_via(adapter, chat_id, message, live=True)

    if not check_wecom_requirements():
        return {"error": "WeCom requirements not met. Need aiohttp + WECOM_BOT_ID/SECRET."}
    try:
        adapter = WeComAdapter(pconfig)
        if not await adapter.connect():
            return {"error": f"WeCom: failed to connect - {getattr(adapter, 'fatal_error_message', None) or 'unknown error'}"}
        try:
            return await _send_via(adapter, chat_id, message, live=False)
        finally:
            await adapter.disconnect()
    except Exception as e:
        return {"error": f"WeCom send failed: {e}"}


def interactive_setup() -> None:
    """Interactive setup for WeCom — QR scan or manual credential input."""
    from hermes_cli.config import get_env_value, remove_env_value, save_env_value
    from hermes_cli.setup import prompt_choice
    from hermes_cli.cli_output import (
        prompt, prompt_yes_no, print_header, print_info, print_success, print_warning,
    )

    print_header("WeCom (Enterprise WeChat)")
    existing_bot_id = get_env_value("WECOM_BOT_ID")
    existing_secret = get_env_value("WECOM_SECRET")
    if existing_bot_id and existing_secret:
        print_success("WeCom is already configured.")
        if not prompt_yes_no("Reconfigure WeCom?", False):
            return

    method_idx = prompt_choice(
        "How would you like to set up WeCom?",
        ["Scan QR code to obtain Bot ID and Secret automatically (recommended)", "Enter existing Bot ID and Secret manually"],
        0,
    )
    bot_id = None
    secret = None
    if method_idx == 0:
        try:
            credentials = qr_scan_for_bot_info()
        except KeyboardInterrupt:
            print_warning("WeCom setup cancelled.")
            return
        except Exception as exc:
            print_warning(f"QR scan failed: {exc}")
            credentials = None
        if credentials:
            bot_id = credentials.get("bot_id", "")
            secret = credentials.get("secret", "")
            print_success("✔ QR scan successful! Bot ID and Secret obtained.")
        if not bot_id or not secret:
            print_info("QR scan did not complete. Continuing with manual input.")
            bot_id = None
            secret = None

    if not bot_id or not secret:
        print_info("1. Go to WeCom Application → Workspace → Smart Robot -> Create smart robots")
        print_info("2. Select API Mode")
        print_info("3. Copy the Bot ID and Secret from the bot's credentials info")
        print_info("4. The bot connects via WebSocket — no public endpoint needed")
        bot_id = prompt("Bot ID", password=False)
        if not bot_id:
            print_warning("Skipped — WeCom won't work without a Bot ID.")
            return
        secret = prompt("Secret", password=True)
        if not secret:
            print_warning("Skipped — WeCom won't work without a Secret.")
            return

    save_env_value("WECOM_BOT_ID", bot_id)
    save_env_value("WECOM_SECRET", secret)

    print_info("The gateway DENIES all users by default for security.")
    print_info("Enter user IDs to create an allowlist, or leave empty.")
    allowed = prompt("Allowed user IDs (comma-separated, or empty)", password=False)
    if allowed:
        save_env_value("WECOM_ALLOWED_USERS", allowed.replace(" ", ""))
        print_success("Saved — only these users can interact with the bot.")
    else:
        access_idx = prompt_choice(
            "How should unauthorized users be handled?",
            [
                "Enable open access (anyone can message the bot)",
                "Use DM pairing (unknown users request access, you approve with 'hermes pairing approve')",
                "Disable direct messages",
                "Skip for now (bot will deny all users until configured)",
            ],
            1,
        )
        if access_idx == 0:
            save_env_value("WECOM_DM_POLICY", "open")
            save_env_value("GATEWAY_ALLOW_ALL_USERS", "true")
            print_warning("Open access enabled — anyone can use your bot!")
        elif access_idx == 1:
            save_env_value("WECOM_DM_POLICY", "pairing")
            print_success("DM pairing mode — users will receive a code to request access.")
            print_info("Approve with: hermes pairing approve <platform> <code>")
        elif access_idx == 2:
            save_env_value("WECOM_DM_POLICY", "disabled")
            print_warning("Direct messages disabled.")
        else:
            print_info("Skipped — configure later with 'hermes gateway setup'")

    home = prompt("Home chat ID (optional, for cron/notifications)", password=False).strip()
    if home:
        save_env_value("WECOM_HOME_CHANNEL", home)
        print_success(f"Home channel set to {home}")
    elif remove_env_value("WECOM_HOME_CHANNEL"):
        print_info("Home channel cleared.")

    print_success("💬 WeCom configured!")


def _is_connected(config) -> bool:
    """Connected when a bot_id is configured."""
    extra = getattr(config, "extra", {}) or {}
    return bool(extra.get("bot_id"))


def _callback_is_connected(config) -> bool:
    """Callback mode is connected when corp_id (or a multi-app `apps` block) is configured."""
    extra = getattr(config, "extra", {}) or {}
    return bool(extra.get("corp_id") or extra.get("apps"))


def _build_adapter(config):
    return WeComAdapter(config)


def _build_callback_adapter(config):
    from plugins.platforms.wecom.callback_adapter import WecomCallbackAdapter
    return WecomCallbackAdapter(config)


def register(ctx) -> None:
    """Plugin entry point — registers both WeCom platforms."""
    ctx.register_platform(
        name="wecom",
        label="WeCom (Enterprise WeChat)",
        adapter_factory=_build_adapter,
        check_fn=check_wecom_requirements,
        is_connected=_is_connected,
        validate_config=_is_connected,
        required_env=["WECOM_BOT_ID", "WECOM_SECRET"],
        install_hint="Run `hermes setup` to install WeCom support.",
        setup_fn=interactive_setup,
        allowed_users_env="WECOM_ALLOWED_USERS",
        allow_all_env="WECOM_ALLOW_ALL_USERS",
        cron_deliver_env_var="WECOM_HOME_CHANNEL",
        standalone_sender_fn=_standalone_send,
        max_message_length=4000,
        emoji="💼",
        allow_update_command=True,
    )

    from plugins.platforms.wecom.callback_adapter import (
        check_wecom_callback_requirements,
        ensure_wecom_callback_requirements,
    )
    ctx.register_platform(
        name="wecom_callback",
        label="WeCom Callback (self-built apps)",
        adapter_factory=_build_callback_adapter,
        check_fn=check_wecom_callback_requirements,
        ensure_deps_fn=ensure_wecom_callback_requirements,
        is_connected=_callback_is_connected,
        validate_config=_callback_is_connected,
        required_env=["WECOM_CALLBACK_CORP_ID", "WECOM_CALLBACK_CORP_SECRET"],
        install_hint="Run `hermes setup` to install WeCom support.",
        allowed_users_env="WECOM_CALLBACK_ALLOWED_USERS",
        allow_all_env="WECOM_CALLBACK_ALLOW_ALL_USERS",
        emoji="💼",
        allow_update_command=True,
    )
