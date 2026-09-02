"""
Weixin platform adapter.

Connects Hermes Agent to WeChat personal accounts via Tencent's iLink Bot API.

Design notes:
- Long-poll ``getupdates`` drives inbound delivery.
- Every outbound reply must echo the latest ``context_token`` for the peer.
- Media files move through an AES-128-ECB encrypted CDN protocol.
- QR login is exposed as a helper for the gateway setup wizard.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import logging
import mimetypes
import os
import re
import secrets
import struct
import tempfile
import textwrap
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import quote, urlparse

logger = logging.getLogger(__name__)

WEIXIN_COPY_LINE_WIDTH = 120

try:
    import aiohttp

    AIOHTTP_AVAILABLE = True
except ImportError:  # pragma: no cover - dependency gate
    aiohttp = None  # type: ignore[assignment]
    AIOHTTP_AVAILABLE = False

try:
    from cryptography.hazmat.backends import default_backend
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    CRYPTO_AVAILABLE = True
except ImportError:  # pragma: no cover - dependency gate
    default_backend = Cipher = algorithms = modes = None  # type: ignore[assignment]
    CRYPTO_AVAILABLE = False

from gateway.config import Platform, PlatformConfig
from gateway.platforms.helpers import MessageDeduplicator, greedy_pack_blocks
from gateway.platforms.base import (
    gateway_trust_env,
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
    cache_audio_from_bytes,
    cache_document_from_bytes,
    cache_image_from_bytes,
)
from hermes_constants import get_hermes_home
from utils import atomic_json_write
from agent.secret_scope import UnscopedSecretError, get_secret


def _wx_secret(name: str, default: Optional[str] = None) -> Optional[str]:
    """Scope-aware WEIXIN_* read. Secondary profiles run scoped: a miss returns ``default``
    (never borrow ``os.environ``). The DEFAULT profile runs *unscoped* under multiplexing,
    where ``get_secret`` raises; there ``os.environ`` is its own value, so fall back."""
    try:
        return get_secret(name, default)
    except UnscopedSecretError:
        return os.getenv(name, default)


def _extra_or_secret(extra: Dict[str, Any], key: str, env: str, default: str = "") -> str:
    """``config.extra[key]`` first, else the scoped secret ``env``; stripped."""
    return str(extra.get(key) or _wx_secret(env, default)).strip()


def _extra_or_env(extra: Dict[str, Any], key: str, env: str, default: str) -> Any:
    """``config.extra[key]`` first, else plain ``os.getenv(env, default)`` (non-secret tunables)."""
    return extra.get(key) or os.getenv(env, default)


ILINK_BASE_URL = "https://ilinkai.weixin.qq.com"
WEIXIN_CDN_BASE_URL = "https://novac2c.cdn.weixin.qq.com/c2c"
ILINK_APP_ID = "bot"
CHANNEL_VERSION = "2.2.0"
ILINK_APP_CLIENT_VERSION = (2 << 16) | (2 << 8) | 0

EP_GET_UPDATES = "ilink/bot/getupdates"
EP_SEND_MESSAGE = "ilink/bot/sendmessage"
EP_SEND_TYPING = "ilink/bot/sendtyping"
EP_GET_CONFIG = "ilink/bot/getconfig"
EP_GET_UPLOAD_URL = "ilink/bot/getuploadurl"
EP_GET_BOT_QR = "ilink/bot/get_bot_qrcode"
EP_GET_QR_STATUS = "ilink/bot/get_qrcode_status"

LONG_POLL_TIMEOUT_MS = 35_000
API_TIMEOUT_MS = 15_000
CONFIG_TIMEOUT_MS = 10_000
QR_TIMEOUT_MS = 35_000

MAX_CONSECUTIVE_FAILURES = 3
RETRY_DELAY_SECONDS = 2
BACKOFF_DELAY_SECONDS = 30
SESSION_EXPIRED_ERRCODE = -14
RATE_LIMIT_ERRCODE = -2  # iLink frequency limit — backoff and retry
MESSAGE_DEDUP_TTL_SECONDS = 300


def _is_stale_session_ret(ret: "Optional[int]", errcode: "Optional[int]", errmsg: "Optional[str]") -> bool:
    """True when iLink returns ret/errcode=-2 with 'unknown error' — a stale-session
    signal (same as errcode=-14) rather than a genuine rate limit."""
    if ret != RATE_LIMIT_ERRCODE and errcode != RATE_LIMIT_ERRCODE:
        return False
    return (errmsg or "").lower() == "unknown error"


def _is_session_expired(resp: Dict[str, Any], ret: Any, errcode: Any) -> bool:
    return (
        ret == SESSION_EXPIRED_ERRCODE
        or errcode == SESSION_EXPIRED_ERRCODE
        or _is_stale_session_ret(ret, errcode, resp.get("errmsg"))
    )


MEDIA_IMAGE = 1
MEDIA_VIDEO = 2
MEDIA_FILE = 3
MEDIA_VOICE = 4

_LIVE_ADAPTERS: Dict[str, Any] = {}


def _make_ssl_connector() -> Optional["aiohttp.TCPConnector"]:
    """TCPConnector with certifi's CA bundle (``ilinkai.weixin.qq.com`` fails some system
    stores, e.g. Homebrew OpenSSL); None without certifi so aiohttp's default (honors
    ``SSL_CERT_FILE`` under trust_env) applies. ``keepalive_timeout=2`` +
    ``enable_cleanup_closed`` drain idle CLOSE_WAIT sockets behind proxies like Warp."""
    try:
        import ssl
        import certifi
    except ImportError:
        return None
    if not AIOHTTP_AVAILABLE:
        return None
    ssl_ctx = ssl.create_default_context(cafile=certifi.where())
    return aiohttp.TCPConnector(ssl=ssl_ctx, keepalive_timeout=2, enable_cleanup_closed=True)


def _new_session(**kwargs: Any) -> "aiohttp.ClientSession":
    return aiohttp.ClientSession(trust_env=gateway_trust_env(), connector=_make_ssl_connector(), **kwargs)


ITEM_TEXT = 1
ITEM_IMAGE = 2
ITEM_VOICE = 3
ITEM_FILE = 4
ITEM_VIDEO = 5

MSG_TYPE_BOT = 2
MSG_STATE_FINISH = 2

TYPING_START = 1
TYPING_STOP = 2

_HEADER_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_TABLE_RULE_RE = re.compile(r"^\s*\|?(?:\s*:?-{3,}:?\s*\|)+\s*:?-{3,}:?\s*\|?\s*$")
_FENCE_RE = re.compile(r"^```([^\n`]*)\s*$")


def check_weixin_requirements() -> bool:
    """Return True when runtime dependencies for Weixin are available."""
    return AIOHTTP_AVAILABLE and CRYPTO_AVAILABLE


def _safe_id(value: Optional[str], keep: int = 8) -> str:
    raw = str(value or "").strip()
    return raw[:keep] if raw else "?"


def _json_dumps(payload: Dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _pkcs7_pad(data: bytes, block_size: int = 16) -> bytes:
    pad_len = block_size - (len(data) % block_size)
    return data + bytes([pad_len] * pad_len)


def _aes_cipher(key: bytes):
    return Cipher(algorithms.AES(key), modes.ECB(), backend=default_backend())


def _aes128_ecb_encrypt(plaintext: bytes, key: bytes) -> bytes:
    encryptor = _aes_cipher(key).encryptor()
    return encryptor.update(_pkcs7_pad(plaintext)) + encryptor.finalize()


def _aes128_ecb_decrypt(ciphertext: bytes, key: bytes) -> bytes:
    decryptor = _aes_cipher(key).decryptor()
    padded = decryptor.update(ciphertext) + decryptor.finalize()
    if not padded:
        return padded
    pad_len = padded[-1]
    if 1 <= pad_len <= 16 and padded.endswith(bytes([pad_len]) * pad_len):
        return padded[:-pad_len]
    return padded


def _aes_padded_size(size: int) -> int:
    return ((size + 1 + 15) // 16) * 16


def _random_wechat_uin() -> str:
    value = struct.unpack(">I", secrets.token_bytes(4))[0]
    return base64.b64encode(str(value).encode("utf-8")).decode("ascii")


def _base_info() -> Dict[str, Any]:
    return {"channel_version": CHANNEL_VERSION}


def _headers(token: Optional[str], body: str) -> Dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "AuthorizationType": "ilink_bot_token",
        "Content-Length": str(len(body.encode("utf-8"))),
        "X-WECHAT-UIN": _random_wechat_uin(),
        "iLink-App-Id": ILINK_APP_ID,
        "iLink-App-ClientVersion": str(ILINK_APP_CLIENT_VERSION),
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _account_dir(hermes_home: str) -> Path:
    path = Path(hermes_home) / "weixin" / "accounts"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _read_json(path: Path) -> Any:
    """Parse a JSON file; ``None`` when missing or unparseable."""
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def save_weixin_account(hermes_home: str, *, account_id: str, token: str, base_url: str, user_id: str = "") -> None:
    """Persist account credentials for later reuse."""
    saved_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    path = _account_dir(hermes_home) / f"{account_id}.json"
    atomic_json_write(path, {"token": token, "base_url": base_url, "user_id": user_id, "saved_at": saved_at})
    with contextlib.suppress(OSError):
        path.chmod(0o600)


def load_weixin_account(hermes_home: str, account_id: str) -> Optional[Dict[str, Any]]:
    """Load persisted account credentials."""
    return _read_json(_account_dir(hermes_home) / f"{account_id}.json")


class ContextTokenStore:
    """Disk-backed ``context_token`` cache keyed by account + peer."""

    def __init__(self, hermes_home: str):
        self._root = _account_dir(hermes_home)
        self._cache: Dict[str, str] = {}

    def _path(self, account_id: str) -> Path:
        return self._root / f"{account_id}.context-tokens.json"

    def _key(self, account_id: str, user_id: str) -> str:
        return f"{account_id}:{user_id}"

    def restore(self, account_id: str) -> None:
        path = self._path(account_id)
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("weixin: failed to restore context tokens for %s: %s", _safe_id(account_id), exc)
            return
        restored = 0
        for user_id, token in data.items():
            if isinstance(token, str) and token:
                self._cache[self._key(account_id, user_id)] = token
                restored += 1
        if restored:
            logger.info("weixin: restored %d context token(s) for %s", restored, _safe_id(account_id))

    def get(self, account_id: str, user_id: str) -> Optional[str]:
        return self._cache.get(self._key(account_id, user_id))

    def set(self, account_id: str, user_id: str, token: str) -> None:
        self._cache[self._key(account_id, user_id)] = token
        self._persist(account_id)

    def _persist(self, account_id: str) -> None:
        prefix = f"{account_id}:"
        payload = {key[len(prefix):]: value for key, value in self._cache.items() if key.startswith(prefix)}
        try:
            atomic_json_write(self._path(account_id), payload)
        except Exception as exc:
            logger.warning("weixin: failed to persist context tokens for %s: %s", _safe_id(account_id), exc)


class TypingTicketCache:
    """Short-lived typing ticket cache from ``getconfig``."""

    def __init__(self, ttl_seconds: float = 600.0):
        self._ttl_seconds = ttl_seconds
        self._cache: Dict[str, Tuple[str, float]] = {}

    def get(self, user_id: str) -> Optional[str]:
        entry = self._cache.get(user_id)
        if entry and time.time() - entry[1] < self._ttl_seconds:
            return entry[0]
        self._cache.pop(user_id, None)
        return None

    def set(self, user_id: str, ticket: str) -> None:
        self._cache[user_id] = (ticket, time.time())


def _cdn_download_url(cdn_base_url: str, encrypted_query_param: str) -> str:
    return f"{cdn_base_url.rstrip('/')}/download?encrypted_query_param={quote(encrypted_query_param, safe='')}"


def _cdn_upload_url(cdn_base_url: str, upload_param: str, filekey: str) -> str:
    return (
        f"{cdn_base_url.rstrip('/')}/upload"
        f"?encrypted_query_param={quote(upload_param, safe='')}"
        f"&filekey={quote(filekey, safe='')}"
    )


def _parse_aes_key(aes_key_b64: str) -> bytes:
    decoded = base64.b64decode(aes_key_b64)
    if len(decoded) == 16:
        return decoded
    if len(decoded) == 32:
        text = decoded.decode("ascii", errors="ignore")
        if text and all(ch in "0123456789abcdefABCDEF" for ch in text):
            return bytes.fromhex(text)
    raise ValueError(f"unexpected aes_key format ({len(decoded)} decoded bytes)")


def _guess_chat_type(message: Dict[str, Any], account_id: str) -> Tuple[str, str]:
    room_id = str(message.get("room_id") or message.get("chat_room_id") or "").strip()
    to_user_id = str(message.get("to_user_id") or "").strip()
    is_group = bool(room_id) or (to_user_id and account_id and to_user_id != account_id and message.get("msg_type") == 1)
    if is_group:
        return "group", room_id or to_user_id or str(message.get("from_user_id") or "")
    return "dm", str(message.get("from_user_id") or "")


# All HTTP helpers below enforce timeouts via asyncio.wait_for() rather than
# aiohttp ClientTimeout, which raises "Timeout context manager should be used
# inside a task" when invoked via asyncio.run_coroutine_threadsafe() from cron.
async def _api_request(
    session: "aiohttp.ClientSession", method: str, *, base_url: str, endpoint: str, headers: Dict[str, str],
    timeout_ms: int, body: Optional[str] = None,
) -> Dict[str, Any]:
    url = f"{base_url.rstrip('/')}/{endpoint}"
    kwargs = {"data": body} if body is not None else {}

    async def _do() -> Dict[str, Any]:
        async with getattr(session, method.lower())(url, headers=headers, **kwargs) as response:
            raw = await response.text()
            if not response.ok:
                raise RuntimeError(f"iLink {method} {endpoint} HTTP {response.status}: {raw[:200]}")
            return json.loads(raw)
    return await asyncio.wait_for(_do(), timeout=timeout_ms / 1000)


async def _api_post(
    session: "aiohttp.ClientSession", *, base_url: str, endpoint: str,
    payload: Dict[str, Any], token: Optional[str], timeout_ms: int,
) -> Dict[str, Any]:
    body = _json_dumps({**payload, "base_info": _base_info()})
    return await _api_request(
        session, "POST", base_url=base_url, endpoint=endpoint,
        headers=_headers(token, body), timeout_ms=timeout_ms, body=body,
    )


async def _api_get(session: "aiohttp.ClientSession", *, base_url: str, endpoint: str, timeout_ms: int) -> Dict[str, Any]:
    headers = {"iLink-App-Id": ILINK_APP_ID, "iLink-App-ClientVersion": str(ILINK_APP_CLIENT_VERSION)}
    return await _api_request(session, "GET", base_url=base_url, endpoint=endpoint, headers=headers, timeout_ms=timeout_ms)


async def _get_updates(session: "aiohttp.ClientSession", *, base_url: str, token: str, sync_buf: str, timeout_ms: int) -> Dict[str, Any]:
    try:
        return await _api_post(
            session, base_url=base_url, endpoint=EP_GET_UPDATES,
            payload={"get_updates_buf": sync_buf}, token=token, timeout_ms=timeout_ms,
        )
    except asyncio.TimeoutError:
        return {"ret": 0, "msgs": [], "get_updates_buf": sync_buf}


async def _send_items(
    session: "aiohttp.ClientSession", *, base_url: str, token: str, to: str, item_list: List[Dict[str, Any]],
    context_token: Optional[str], client_id: str,
) -> Dict[str, Any]:
    """POST one ``sendmessage`` with the given item list; returns the raw response."""
    message: Dict[str, Any] = {
        "from_user_id": "",
        "to_user_id": to,
        "client_id": client_id,
        "message_type": MSG_TYPE_BOT,
        "message_state": MSG_STATE_FINISH,
        "item_list": item_list,
    }
    if context_token:
        message["context_token"] = context_token
    return await _api_post(
        session, base_url=base_url, endpoint=EP_SEND_MESSAGE,
        payload={"msg": message}, token=token, timeout_ms=API_TIMEOUT_MS,
    )


async def _send_message(
    session: "aiohttp.ClientSession", *, base_url: str, token: str, to: str, text: str, context_token: Optional[str],
    client_id: str,
) -> Dict[str, Any]:
    """Send a text message. Returns the raw API response (may carry ``errcode: -14`` etc.)."""
    if not text or not text.strip():
        raise ValueError("_send_message: text must not be empty")
    return await _send_items(
        session, base_url=base_url, token=token, to=to,
        item_list=[{"type": ITEM_TEXT, "text_item": {"text": text}}],
        context_token=context_token, client_id=client_id,
    )


async def _get_config(
    session: "aiohttp.ClientSession", *, base_url: str, token: str, user_id: str, context_token: Optional[str],
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {"ilink_user_id": user_id}
    if context_token:
        payload["context_token"] = context_token
    return await _api_post(
        session, base_url=base_url, endpoint=EP_GET_CONFIG, payload=payload, token=token, timeout_ms=CONFIG_TIMEOUT_MS,
    )


async def _get_upload_url(
    session: "aiohttp.ClientSession", *, base_url: str, token: str, to_user_id: str, media_type: int,
    filekey: str, rawsize: int, rawfilemd5: str, filesize: int, aeskey_hex: str,
) -> Dict[str, Any]:
    return await _api_post(
        session, base_url=base_url, endpoint=EP_GET_UPLOAD_URL,
        payload={
            "filekey": filekey, "media_type": media_type, "to_user_id": to_user_id, "rawsize": rawsize,
            "rawfilemd5": rawfilemd5, "filesize": filesize, "no_need_thumb": True, "aeskey": aeskey_hex,
        },
        token=token, timeout_ms=API_TIMEOUT_MS,
    )


async def _upload_ciphertext(session: "aiohttp.ClientSession", *, ciphertext: bytes, upload_url: str) -> str:
    """POST encrypted media to the CDN (constructed URL or direct ``upload_full_url``)."""
    async def _do_upload() -> str:
        async with session.post(upload_url, data=ciphertext, headers={"Content-Type": "application/octet-stream"}) as response:
            encrypted_param = response.headers.get("x-encrypted-param") if response.status == 200 else None
            if encrypted_param:
                await response.read()
                return encrypted_param
            raw = await response.text()
            if response.status == 200:
                raise RuntimeError(f"CDN upload missing x-encrypted-param header: {raw[:200]}")
            raise RuntimeError(f"CDN upload HTTP {response.status}: {raw[:200]}")
    return await asyncio.wait_for(_do_upload(), timeout=120)


async def _download_bytes(session: "aiohttp.ClientSession", *, url: str, timeout_seconds: float = 60.0) -> bytes:
    async def _do_download() -> bytes:
        async with session.get(url) as response:
            response.raise_for_status()
            return await response.read()
    return await asyncio.wait_for(_do_download(), timeout=timeout_seconds)


_WEIXIN_CDN_ALLOWLIST: frozenset[str] = frozenset({
    "novac2c.cdn.weixin.qq.com", "ilinkai.weixin.qq.com", "wx.qlogo.cn", "thirdwx.qlogo.cn",
    "res.wx.qq.com", "mmbiz.qpic.cn", "mmbiz.qlogo.cn",
})


def _assert_weixin_cdn_url(url: str) -> None:
    """Raise ValueError if *url* does not point at a known WeChat CDN host."""
    try:
        parsed = urlparse(url)
        scheme = parsed.scheme.lower()
        host = parsed.hostname or ""
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"Unparseable media URL: {url!r}") from exc
    if scheme not in {"http", "https"}:
        raise ValueError(f"Media URL has disallowed scheme {scheme!r}; only http/https are permitted.")
    if host not in _WEIXIN_CDN_ALLOWLIST:
        raise ValueError(f"Media URL host {host!r} is not in the WeChat CDN allowlist. Refusing to fetch to prevent SSRF.")


async def _download_and_decrypt_media(
    session: "aiohttp.ClientSession", *, cdn_base_url: str, encrypted_query_param: Optional[str],
    aes_key_b64: Optional[str], full_url: Optional[str], timeout_seconds: float,
) -> bytes:
    if encrypted_query_param:
        raw = await _download_bytes(
            session, url=_cdn_download_url(cdn_base_url, encrypted_query_param), timeout_seconds=timeout_seconds,
        )
    elif full_url:
        _assert_weixin_cdn_url(full_url)
        raw = await _download_bytes(session, url=full_url, timeout_seconds=timeout_seconds)
    else:
        raise RuntimeError("media item had neither encrypt_query_param nor full_url")
    if aes_key_b64:
        raw = _aes128_ecb_decrypt(raw, _parse_aes_key(aes_key_b64))
    return raw


def _normalize_markdown_blocks(content: str) -> str:
    result: List[str] = []
    in_code_block = False
    blank_run = 0
    for raw_line in content.splitlines():
        line = raw_line.rstrip()
        if _FENCE_RE.match(line.strip()):
            in_code_block = not in_code_block
            result.append(line)
            blank_run = 0
        elif in_code_block:
            result.append(line)
        elif not line.strip():
            blank_run += 1
            if blank_run <= 1:
                result.append("")
        else:
            blank_run = 0
            result.append(line)
    return "\n".join(result).strip()


def _wrap_copy_friendly_lines_for_weixin(content: str) -> str:
    """Wrap long display lines that are hard to copy in WeChat clients."""
    if not content:
        return content
    wrapped: List[str] = []
    in_code_block = False
    for raw_line in content.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        if _FENCE_RE.match(stripped):
            in_code_block = not in_code_block
            wrapped.append(line)
        elif (
            in_code_block
            or len(line) <= WEIXIN_COPY_LINE_WIDTH
            or not stripped
            or stripped.startswith("|")
            or _TABLE_RULE_RE.match(stripped)
        ):
            wrapped.append(line)
        else:
            wrapped.extend(textwrap.wrap(
                line, width=WEIXIN_COPY_LINE_WIDTH, break_long_words=False,
                break_on_hyphens=False, replace_whitespace=False, drop_whitespace=True,
            ) or [line])
    return "\n".join(wrapped).strip()


def _split_markdown_blocks(content: str) -> List[str]:
    """Split on blank lines, keeping each fenced code block as one block."""
    if not content:
        return []
    blocks: List[str] = []
    current: List[str] = []
    in_code_block = False

    def flush() -> None:
        if current:
            blocks.append("\n".join(current).strip())
            current.clear()
    for raw_line in content.splitlines():
        line = raw_line.rstrip()
        if _FENCE_RE.match(line.strip()):
            if not in_code_block:
                flush()
            current.append(line)
            in_code_block = not in_code_block
            if not in_code_block:
                flush()
        elif in_code_block:
            current.append(line)
        elif not line.strip():
            flush()
        else:
            current.append(line)
    flush()
    return [block for block in blocks if block]


def _split_delivery_units_for_weixin(content: str) -> List[str]:
    """Split formatted content into chat-friendly delivery units.

    Top-level line breaks become separate messages; fenced code blocks stay
    intact and indented continuation lines attach to the previous top-level
    line so nested list items are not torn apart.
    """
    units: List[str] = []
    for block in _split_markdown_blocks(content):
        if _FENCE_RE.match(block.splitlines()[0].strip()):
            units.append(block)
            continue
        current: List[str] = []
        for raw_line in block.splitlines():
            line = raw_line.rstrip()
            if current and line.strip() and raw_line.startswith((" ", "\t")):
                current.append(line)  # indented continuation
                continue
            if current:
                units.append("\n".join(current).strip())
            current = [line] if line.strip() else []
        if current:
            units.append("\n".join(current).strip())
    return [unit for unit in units if unit]


def _looks_like_chatty_line_for_weixin(line: str) -> bool:
    """Return True when a line looks like a standalone chat utterance."""
    stripped = line.strip()
    return bool(
        stripped
        and len(stripped) <= 48
        and not line.startswith((" ", "\t"))
        and not stripped.startswith((">", "-", "*", "【", "#", "|"))
        and not _TABLE_RULE_RE.match(stripped)
        and not re.match(r"^\*\*[^*]+\*\*$", stripped)
        and not re.match(r"^\d+\.\s", stripped)
    )


def _looks_like_heading_line_for_weixin(line: str) -> bool:
    """Return True when a short line behaves like a heading."""
    stripped = line.strip()
    if not stripped:
        return False
    return bool(_HEADER_RE.match(stripped)) or (len(stripped) <= 24 and stripped.endswith((":", "：")))


def _should_split_short_chat_block_for_weixin(block: str) -> bool:
    """Split only chat-like multiline blocks into separate bubbles."""
    lines = [line for line in block.splitlines() if line.strip()]
    if not 2 <= len(lines) <= 6:
        return False
    if _looks_like_heading_line_for_weixin(lines[0]):
        return False
    return all(_looks_like_chatty_line_for_weixin(line) for line in lines)


def _pack_markdown_blocks_for_weixin(content: str, max_length: int) -> List[str]:
    if len(content) <= max_length:
        return [content]
    # Block extraction stays weixin-local (anchored _FENCE_RE + per-line rstrip);
    # the greedy packing loop is the shared core's.
    return greedy_pack_blocks(
        _split_markdown_blocks(content), max_length,
        overflow=lambda block: BasePlatformAdapter.truncate_message(block, max_length),
    )


def _split_text_for_weixin_delivery(content: str, max_length: int, split_per_line: bool = False) -> List[str]:
    """Split content into sequential Weixin messages.

    Compact (default): one message when it fits — unless it reads as a short chatty
    exchange, which becomes separate bubbles. Per-line (legacy, via
    ``extra.split_multiline_messages`` / ``WEIXIN_SPLIT_MULTILINE_MESSAGES``): top-level
    line breaks become separate messages. Oversized units use block-aware packing.
    """
    if not content:
        return []
    if split_per_line:
        if len(content) <= max_length and "\n" not in content:
            return [content]
        chunks = [
            c for unit in _split_delivery_units_for_weixin(content)
            for c in ([unit] if len(unit) <= max_length else _pack_markdown_blocks_for_weixin(unit, max_length))
        ]
        return [c for c in chunks if c] or [content]
    if len(content) <= max_length:
        if _should_split_short_chat_block_for_weixin(content):
            return [u for u in _split_delivery_units_for_weixin(content) if u]
        return [content]
    return _pack_markdown_blocks_for_weixin(content, max_length) or [content]


def _coerce_bool(value: Any, default: bool = True) -> bool:
    """Coerce a config value to bool, tolerating strings like ``"true"``."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def _extract_text(item_list: List[Dict[str, Any]]) -> str:
    for item in item_list:
        if item.get("type") == ITEM_TEXT:
            text = str((item.get("text_item") or {}).get("text") or "")
            ref = item.get("ref_msg") or {}
            ref_item = ref.get("message_item") or {}
            if ref_item.get("type") in {ITEM_IMAGE, ITEM_VIDEO, ITEM_FILE, ITEM_VOICE}:
                title = ref.get("title") or ""
                prefix = f"[引用媒体: {title}]\n" if title else "[引用媒体]\n"
                return f"{prefix}{text}".strip()
            if ref_item:
                parts = [p for p in (str(ref["title"]) if ref.get("title") else "", _extract_text([ref_item])) if p]
                if parts:
                    return f"[引用: {' | '.join(parts)}]\n{text}".strip()
            return text
    for item in item_list:
        if item.get("type") == ITEM_VOICE:
            # Tencent's ``voice_item.text`` is their STT output and is wrong for
            # non-Chinese audio. When raw audio exists return "" so the central
            # STT pipeline in gateway/run.py transcribes the download instead.
            voice_item = item.get("voice_item") or {}
            if not (voice_item.get("media") or {}):
                # No audio to download — use Weixin's transcript but mark its
                # voice origin so the agent can tell it apart from typed text.
                voice_text = str(voice_item.get("text") or "")
                if voice_text:
                    return f"[Voice transcription provided by Weixin]\n{voice_text}"
    return ""


_MIME_PREFIX_TYPES = (("image/", MessageType.PHOTO), ("video/", MessageType.VIDEO), ("audio/", MessageType.VOICE))


def _message_type_from_media(media_types: List[str], text: str) -> MessageType:
    for prefix, message_type in _MIME_PREFIX_TYPES:
        if any(m.startswith(prefix) for m in media_types):
            return message_type
    if media_types:
        return MessageType.DOCUMENT
    if text.startswith("/"):
        return MessageType.COMMAND
    return MessageType.TEXT


def _sync_buf_path(hermes_home: str, account_id: str) -> Path:
    return _account_dir(hermes_home) / f"{account_id}.sync.json"


def _load_sync_buf(hermes_home: str, account_id: str) -> str:
    data = _read_json(_sync_buf_path(hermes_home, account_id))
    return data.get("get_updates_buf", "") if isinstance(data, dict) else ""


def _save_sync_buf(hermes_home: str, account_id: str, sync_buf: str) -> None:
    atomic_json_write(_sync_buf_path(hermes_home, account_id), {"get_updates_buf": sync_buf})


async def _fetch_qr(session: "aiohttp.ClientSession", bot_type: str) -> Tuple[str, str]:
    """Fetch a login QR; returns (qrcode hex token, qrcode_img_content URL)."""
    qr_resp = await _api_get(
        session, base_url=ILINK_BASE_URL, endpoint=f"{EP_GET_BOT_QR}?bot_type={bot_type}", timeout_ms=QR_TIMEOUT_MS,
    )
    return str(qr_resp.get("qrcode") or ""), str(qr_resp.get("qrcode_img_content") or "")


def _print_qr(qrcode_value: str, qrcode_url: str, *, report_render_error: bool) -> None:
    """Print the QR URL and an ASCII rendering. WeChat must scan the full liteapp
    URL (``qrcode_img_content``) when available, not the bare hex token."""
    if qrcode_url:
        print(qrcode_url)
    try:
        import qrcode
        qr = qrcode.QRCode()
        qr.add_data(qrcode_url or qrcode_value)
        qr.make(fit=True)
        qr.print_ascii(invert=True)
    except Exception as _qr_exc:
        if report_render_error:
            print(f"（终端二维码渲染失败: {_qr_exc}，请直接打开上面的二维码链接）")


async def qr_login(hermes_home: str, *, bot_type: str = "3", timeout_seconds: int = 480) -> Optional[Dict[str, str]]:
    """Run the interactive iLink QR login flow; credential dict on success, else ``None``."""
    if not AIOHTTP_AVAILABLE:
        raise RuntimeError("aiohttp is required for Weixin QR login")
    async with _new_session() as session:
        try:
            qrcode_value, qrcode_url = await _fetch_qr(session, bot_type)
        except Exception as exc:
            logger.error("weixin: failed to fetch QR code: %s", exc)
            return None
        if not qrcode_value:
            logger.error("weixin: QR response missing qrcode")
            return None
        print("\n请使用微信扫描以下二维码：")
        _print_qr(qrcode_value, qrcode_url, report_render_error=True)
        deadline = time.monotonic() + timeout_seconds
        current_base_url = ILINK_BASE_URL
        refresh_count = 0
        while time.monotonic() < deadline:
            try:
                status_resp = await _api_get(
                    session, base_url=current_base_url,
                    endpoint=f"{EP_GET_QR_STATUS}?qrcode={qrcode_value}", timeout_ms=QR_TIMEOUT_MS,
                )
            except asyncio.TimeoutError:
                await asyncio.sleep(1)
                continue
            except Exception as exc:
                logger.warning("weixin: QR poll error: %s", exc)
                await asyncio.sleep(1)
                continue
            status = str(status_resp.get("status") or "wait")
            if status == "wait":
                print(".", end="", flush=True)
            elif status == "scaned":
                print("\n已扫码，请在微信里确认...")
            elif status == "scaned_but_redirect":
                redirect_host = str(status_resp.get("redirect_host") or "")
                if redirect_host:
                    current_base_url = f"https://{redirect_host}"
            elif status == "expired":
                refresh_count += 1
                if refresh_count > 3:
                    print("\n二维码多次过期，请重新执行登录。")
                    return None
                print(f"\n二维码已过期，正在刷新... ({refresh_count}/3)")
                try:
                    qrcode_value, qrcode_url = await _fetch_qr(session, bot_type)
                    _print_qr(qrcode_value, qrcode_url, report_render_error=False)
                except Exception as exc:
                    logger.error("weixin: QR refresh failed: %s", exc)
                    return None
            elif status == "confirmed":
                account_id = str(status_resp.get("ilink_bot_id") or "")
                token = str(status_resp.get("bot_token") or "")
                base_url = str(status_resp.get("baseurl") or ILINK_BASE_URL)
                user_id = str(status_resp.get("ilink_user_id") or "")
                if not account_id or not token:
                    logger.error("weixin: QR confirmed but credential payload was incomplete")
                    return None
                save_weixin_account(hermes_home, account_id=account_id, token=token, base_url=base_url, user_id=user_id)
                print(f"\n微信连接成功，account_id={account_id}")
                return {"account_id": account_id, "token": token, "base_url": base_url, "user_id": user_id}
            await asyncio.sleep(1)
        print("\n微信登录超时。")
        return None


def _encrypted_media(kw: Dict[str, Any]) -> Dict[str, Any]:
    return {"encrypt_query_param": kw["encrypt_query_param"], "aes_key": kw["aes_key_for_api"], "encrypt_type": 1}


def _file_item(**kw: Any) -> Dict[str, Any]:
    return {
        "type": ITEM_FILE,
        "file_item": {"media": _encrypted_media(kw), "file_name": kw["filename"], "len": str(kw["plaintext_size"])},
    }


def _image_item(**kw: Any) -> Dict[str, Any]:
    return {"type": ITEM_IMAGE, "image_item": {"media": _encrypted_media(kw), "mid_size": kw["ciphertext_size"]}}


def _video_item(**kw: Any) -> Dict[str, Any]:
    return {
        "type": ITEM_VIDEO,
        "video_item": {
            "media": _encrypted_media(kw),
            "video_size": kw["ciphertext_size"],
            "play_length": kw.get("play_length", 0),
            "video_md5": kw.get("rawfilemd5", ""),
        },
    }


def _voice_item(**kw: Any) -> Dict[str, Any]:
    return {
        "type": ITEM_VOICE,
        "voice_item": {
            "media": _encrypted_media(kw),
            "encode_type": kw.get("encode_type"),
            "bits_per_sample": kw.get("bits_per_sample"),
            "sample_rate": kw.get("sample_rate"),
            "playtime": kw.get("playtime", 0),
        },
    }


# Inbound media dispatch: item type -> (item key, download timeout, cache fn, mime or None (= guess from
# file_name), log label). Cache fns are lambdas so monkeypatching the module names takes effect at call time.
_INBOUND_MEDIA: Dict[int, Tuple[str, float, Callable[[bytes, str], str], Optional[str], str]] = {
    ITEM_IMAGE: ("image_item", 30.0, lambda data, _name: cache_image_from_bytes(data, ".jpg"), "image/jpeg", "image"),
    ITEM_VIDEO: ("video_item", 120.0, lambda data, _name: cache_document_from_bytes(data, "video.mp4"), "video/mp4", "video"),
    ITEM_FILE: ("file_item", 60.0, lambda data, name: cache_document_from_bytes(data, name), None, "file"),
    ITEM_VOICE: ("voice_item", 60.0, lambda data, _name: cache_audio_from_bytes(data, ".silk"), "audio/silk", "voice"),
}

_AUDIO_EXTS = {".ogg", ".opus", ".mp3", ".wav", ".m4a", ".flac"}
_VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".3gp"}
_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
_DIRECT_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}


class WeixinAdapter(BasePlatformAdapter):
    """Native Hermes adapter for Weixin personal accounts."""

    supports_code_blocks = True  # Weixin renders fenced code blocks
    splits_long_messages = True  # send() chunks via _split_text()

    MAX_MESSAGE_LENGTH = 2000

    # WeChat cannot edit sent messages — streaming must use the send-final-only
    # path so the cursor (▉) is never left visible.
    SUPPORTS_MESSAGE_EDITING = False

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform.WEIXIN)
        extra = config.extra or {}
        hermes_home = str(get_hermes_home())
        self._hermes_home = hermes_home
        self._token_store = ContextTokenStore(hermes_home)
        self._typing_cache = TypingTicketCache()
        self._poll_session: Optional[aiohttp.ClientSession] = None
        self._send_session: Optional[aiohttp.ClientSession] = None
        self._poll_task: Optional[asyncio.Task] = None
        self._dedup = MessageDeduplicator(ttl_seconds=MESSAGE_DEDUP_TTL_SECONDS)
        self._account_id = _extra_or_secret(extra, "account_id", "WEIXIN_ACCOUNT_ID")
        self._token = str(config.token or extra.get("token") or _wx_secret("WEIXIN_TOKEN", "")).strip()
        self._base_url = _extra_or_secret(extra, "base_url", "WEIXIN_BASE_URL", ILINK_BASE_URL).rstrip("/")
        self._cdn_base_url = _extra_or_secret(extra, "cdn_base_url", "WEIXIN_CDN_BASE_URL", WEIXIN_CDN_BASE_URL).rstrip("/")
        self._send_chunk_delay_seconds = float(
            _extra_or_env(extra, "send_chunk_delay_seconds", "WEIXIN_SEND_CHUNK_DELAY_SECONDS", "1.5")
        )
        self._send_chunk_retries = int(_extra_or_env(extra, "send_chunk_retries", "WEIXIN_SEND_CHUNK_RETRIES", "4"))
        self._send_chunk_retry_delay_seconds = float(
            _extra_or_env(extra, "send_chunk_retry_delay_seconds", "WEIXIN_SEND_CHUNK_RETRY_DELAY_SECONDS", "1.0")
        )
        self._send_text_gate = asyncio.Lock()
        self._rate_limit_circuit_threshold = max(
            1, int(_extra_or_env(extra, "rate_limit_circuit_threshold", "WEIXIN_RATE_LIMIT_CIRCUIT_THRESHOLD", "1"))
        )
        self._rate_limit_circuit_window_seconds = float(
            _extra_or_env(extra, "rate_limit_circuit_window_seconds", "WEIXIN_RATE_LIMIT_CIRCUIT_WINDOW_SECONDS", "30.0")
        )
        self._rate_limit_circuit_open_seconds = float(
            _extra_or_env(extra, "rate_limit_circuit_open_seconds", "WEIXIN_RATE_LIMIT_CIRCUIT_OPEN_SECONDS", "30.0")
        )
        self._rate_limit_circuit_until = 0.0
        self._rate_limit_events: List[float] = []
        self._dm_policy = _extra_or_secret(extra, "dm_policy", "WEIXIN_DM_POLICY", "pairing").lower()
        self._group_policy = _extra_or_secret(extra, "group_policy", "WEIXIN_GROUP_POLICY", "disabled").lower()
        # ``extra`` wins even when falsy (an explicit empty list disables the env allowlist).
        allow_from, group_allow_from = extra.get("allow_from"), extra.get("group_allow_from")
        self._allow_from = self._coerce_list(_wx_secret("WEIXIN_ALLOWED_USERS", "") if allow_from is None else allow_from)
        self._group_allow_from = self._coerce_list(
            _wx_secret("WEIXIN_GROUP_ALLOWED_USERS", "") if group_allow_from is None else group_allow_from
        )
        self._split_multiline_messages = _coerce_bool(
            extra.get("split_multiline_messages") or os.getenv("WEIXIN_SPLIT_MULTILINE_MESSAGES"), default=False,
        )

        # Text debounce batching (Telegram pattern): iLink delivers messages individually, so rapid
        # bursts would each trigger a separate agent run. 3s / 5s (after a ~2048-char split chunk)
        # suit iLink's cadence; tunable via ``extra.text_batch_delay_seconds`` / ``text_batch_split_delay_seconds``.
        self._text_batch_delay_seconds = self._coerce_float_extra("text_batch_delay_seconds", 3.0)
        self._text_batch_split_delay_seconds = self._coerce_float_extra("text_batch_split_delay_seconds", 5.0)
        self._pending_text_batches: Dict[str, MessageEvent] = {}
        self._pending_text_batch_tasks: Dict[str, asyncio.Task] = {}
        if self._account_id and not self._token:
            persisted = load_weixin_account(hermes_home, self._account_id)
            if persisted:
                self._token = str(persisted.get("token") or "").strip()
                self._base_url = str(persisted.get("base_url") or self._base_url).strip().rstrip("/")

    def _coerce_float_extra(self, key: str, default: float) -> float:
        """Float from ``config.extra``; fed to ``asyncio.sleep()``, so NaN/Inf/negative/unparseable → default."""
        import math
        value = self.config.extra.get(key) if getattr(self.config, "extra", None) else None
        try:
            parsed = float(value) if value is not None else float(default)
        except (TypeError, ValueError):
            return float(default)
        return parsed if math.isfinite(parsed) and parsed >= 0 else float(default)

    @staticmethod
    def _coerce_list(value: Any) -> List[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        if isinstance(value, (list, tuple, set)):
            return [str(item).strip() for item in value if str(item).strip()]
        return [str(value).strip()] if str(value).strip() else []

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        preflight = (
            (check_weixin_requirements(), "weixin_missing_dependency", "aiohttp and cryptography are required"),
            (self._token, "weixin_missing_token", "WEIXIN_TOKEN is required"),
            (self._account_id, "weixin_missing_account", "WEIXIN_ACCOUNT_ID is required"),
        )
        for ok, code, reason in preflight:
            if not ok:
                message = f"Weixin startup failed: {reason}"
                self._set_fatal_error(code, message, retryable=False)
                logger.warning("[%s] %s", self.name, message)
                return False
        try:
            if not self._acquire_platform_lock('weixin-bot-token', self._token, 'Weixin bot token'):
                return False
        except Exception as exc:
            logger.debug("[%s] Token lock unavailable (non-fatal): %s", self.name, exc)
        self._poll_session = _new_session()
        # total=None disables aiohttp's ClientTimeout so send() works via run_coroutine_threadsafe()
        # from cron; _api_post/_api_get enforce timeouts with asyncio.wait_for() instead.
        self._send_session = _new_session(
            timeout=aiohttp.ClientTimeout(total=None, connect=None, sock_connect=None, sock_read=None)
        )
        self._token_store.restore(self._account_id)
        self._poll_task = asyncio.create_task(self._poll_loop(), name="weixin-poll")
        self._mark_connected()
        _LIVE_ADAPTERS[self._token] = self
        logger.info("[%s] Connected account=%s base=%s", self.name, _safe_id(self._account_id), self._base_url)
        if self._group_policy != "disabled":
            logger.warning(
                "[%s] WEIXIN_GROUP_POLICY=%s is set, but QR-login connects an iLink bot identity (e.g. ...@im.bot) "
                "which typically cannot be invited into ordinary WeChat groups. iLink usually does not deliver "
                "ordinary-group events for these accounts, so group messages may never reach Hermes regardless of "
                "this policy. If group delivery doesn't work, the limitation is on the iLink side, not in Hermes.",
                self.name, self._group_policy,
            )
        # Plugin-registered native handlers (ctx.register_platform_handler).
        self._wire_plugin_handlers(None)
        return True

    async def disconnect(self) -> None:
        _LIVE_ADAPTERS.pop(self._token, None)
        self._running = False
        for task in self._pending_text_batch_tasks.values():
            if not task.done():
                task.cancel()
        self._pending_text_batches.clear()
        self._pending_text_batch_tasks.clear()
        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._poll_task
        self._poll_task = None
        for attr in ("_poll_session", "_send_session"):
            session = getattr(self, attr)
            if session and not session.closed:
                await session.close()
            setattr(self, attr, None)
        self._release_platform_lock()
        self._mark_disconnected()
        logger.info("[%s] Disconnected", self.name)

    @staticmethod
    async def _poll_backoff(consecutive_failures: int) -> int:
        """Sleep for the failure streak; returns the new streak count (0 after a full streak)."""
        streak_done = consecutive_failures >= MAX_CONSECUTIVE_FAILURES
        await asyncio.sleep(BACKOFF_DELAY_SECONDS if streak_done else RETRY_DELAY_SECONDS)
        return 0 if streak_done else consecutive_failures

    async def _poll_loop(self) -> None:
        assert self._poll_session is not None
        sync_buf = _load_sync_buf(self._hermes_home, self._account_id)
        timeout_ms = LONG_POLL_TIMEOUT_MS
        consecutive_failures = 0
        while self._running:
            try:
                response = await _get_updates(
                    self._poll_session, base_url=self._base_url, token=self._token, sync_buf=sync_buf, timeout_ms=timeout_ms,
                )
                suggested_timeout = response.get("longpolling_timeout_ms")
                if isinstance(suggested_timeout, int) and suggested_timeout > 0:
                    timeout_ms = suggested_timeout
                ret = response.get("ret", 0)
                errcode = response.get("errcode", 0)
                if ret not in {0, None} or errcode not in {0, None}:
                    if _is_session_expired(response, ret, errcode):
                        logger.error("[%s] Session expired; pausing for 10 minutes", self.name)
                        await asyncio.sleep(600)
                        consecutive_failures = 0
                        continue
                    consecutive_failures += 1
                    logger.warning(
                        "[%s] getUpdates failed ret=%s errcode=%s errmsg=%s (%d/%d)", self.name, ret, errcode,
                        response.get("errmsg", ""), consecutive_failures, MAX_CONSECUTIVE_FAILURES,
                    )
                    consecutive_failures = await self._poll_backoff(consecutive_failures)
                    continue
                consecutive_failures = 0
                new_sync_buf = str(response.get("get_updates_buf") or "")
                if new_sync_buf:
                    sync_buf = new_sync_buf
                    _save_sync_buf(self._hermes_home, self._account_id, sync_buf)
                for message in response.get("msgs") or []:
                    asyncio.create_task(self._process_message_safe(message))
            except asyncio.CancelledError:
                break
            except Exception as exc:
                consecutive_failures += 1
                logger.error("[%s] poll error (%d/%d): %s", self.name, consecutive_failures, MAX_CONSECUTIVE_FAILURES, exc)
                consecutive_failures = await self._poll_backoff(consecutive_failures)
                if consecutive_failures == 0:
                    # Full failure streak: recycle the session. Failed connects through a
                    # local proxy (e.g. Clash) strand sockets the keepalive reaper never
                    # sees; on macOS the 256-fd soft limit then yields EMFILE and a crash.
                    # Closing the session tears down its connector and every socket.
                    await self._recycle_poll_session()

    async def _recycle_poll_session(self) -> None:
        """Swap in a fresh ``_poll_session`` *then* close the old one, so concurrent
        ``_process_message`` tasks never observe a closed session."""
        if not self._running or aiohttp is None:
            return
        old = self._poll_session
        self._poll_session = _new_session()
        if old is not None and not old.closed:
            try:
                await old.close()
            except Exception as exc:
                logger.debug("[%s] old poll session close failed: %s", self.name, exc)

    async def _process_message_safe(self, message: Dict[str, Any]) -> None:
        try:
            await self._process_message(message)
        except Exception as exc:
            logger.error("[%s] unhandled inbound error from=%s: %s", self.name, _safe_id(message.get("from_user_id")), exc, exc_info=True)

    async def _process_message(self, message: Dict[str, Any]) -> None:
        assert self._poll_session is not None
        sender_id = str(message.get("from_user_id") or "").strip()
        if not sender_id or sender_id == self._account_id:
            return
        message_id = str(message.get("message_id") or "").strip()
        if message_id and self._dedup.is_duplicate(message_id):
            return

        # Secondary content-fingerprint dedup: upstream re-sends identical text under new message_ids.
        item_list = message.get("item_list") or []
        text = _extract_text(item_list)
        if text:
            content_key = f"content:{sender_id}:{hashlib.md5(text.encode()).hexdigest()}"
            if self._dedup.is_duplicate(content_key):
                logger.debug("[%s] Content-dedup: skipping duplicate message from %s", self.name, sender_id)
                return
        chat_type, effective_chat_id = _guess_chat_type(message, self._account_id)
        if chat_type == "group":
            if self._group_policy == "disabled" or self._group_policy == "pairing":
                return
            if self._group_policy == "allowlist" and effective_chat_id not in self._group_allow_from:
                return
        elif not self._is_dm_intake_allowed(sender_id):
            return
        context_token = str(message.get("context_token") or "").strip()
        if context_token:
            self._token_store.set(self._account_id, sender_id, context_token)
        asyncio.create_task(self._maybe_fetch_typing_ticket(sender_id, context_token or None))
        media_paths: List[str] = []
        media_types: List[str] = []
        for item in item_list:
            await self._collect_media(item, media_paths, media_types)
            ref_item = (item.get("ref_msg") or {}).get("message_item")
            if isinstance(ref_item, dict):
                await self._collect_media(ref_item, media_paths, media_types)
        if not text and not media_paths:
            return
        source = self.build_source(chat_id=effective_chat_id, chat_type=chat_type, user_id=sender_id, user_name=sender_id)
        event = MessageEvent(
            text=text, message_type=_message_type_from_media(media_types, text), source=source,
            raw_message=message, message_id=message_id or None, media_urls=media_paths,
            media_types=media_types, timestamp=datetime.now(),
        )
        logger.info("[%s] inbound from=%s type=%s media=%d", self.name, _safe_id(sender_id), source.chat_type, len(media_paths))
        if event.message_type == MessageType.TEXT:
            self._enqueue_text_event(event)
        else:
            await self.handle_message(event)

    def _open_dm_opted_in(self) -> bool:
        # Scoped reads: the default profile's allow-all flag must not leak into a
        # multiplexed secondary profile's admission gate.
        if (_wx_secret("GATEWAY_ALLOW_ALL_USERS", "") or "").lower() in {"true", "1", "yes"}:
            return True
        return (_wx_secret("WEIXIN_ALLOW_ALL_USERS", "") or "").lower() in {"true", "1", "yes"}

    def _is_dm_allowed(self, sender_id: str) -> bool:
        if self._dm_policy == "allowlist":
            return sender_id in self._allow_from
        if self._dm_policy == "open":
            return self._open_dm_opted_in()
        return False

    def _is_dm_intake_allowed(self, sender_id: str) -> bool:
        """Like ``_is_dm_allowed`` but ``pairing`` admits everyone at intake (pairing gate runs later)."""
        return self._dm_policy == "pairing" or self._is_dm_allowed(sender_id)

    @property
    def enforces_own_access_policy(self) -> bool:
        """Weixin gates DM/group access at intake via dm_policy/group_policy."""
        return True

    # ------------------------------------------------------------------
    # Text debounce batching
    # ------------------------------------------------------------------

    _SPLIT_THRESHOLD = 1800  # iLink chunks at ~2048 chars

    def _text_batch_key(self, event: MessageEvent) -> str:
        """Session-scoped key for text message batching."""
        from gateway.session import build_session_key
        return build_session_key(
            event.source,
            group_sessions_per_user=self.config.extra.get("group_sessions_per_user", True),
            thread_sessions_per_user=self.config.extra.get("thread_sessions_per_user", False),
            profile=event.source.profile,
        )

    async def _flush_text_batch(self, key: str) -> None:
        """Wait for quiet period then dispatch aggregated text."""
        current_task = asyncio.current_task()
        try:
            pending = self._pending_text_batches.get(key)
            last_len = getattr(pending, "_last_chunk_len", 0) if pending else 0
            split = last_len >= self._SPLIT_THRESHOLD
            await asyncio.sleep(self._text_batch_split_delay_seconds if split else self._text_batch_delay_seconds)
            if self._pending_text_batch_tasks.get(key) is not current_task:
                return
            event = self._pending_text_batches.pop(key, None)
            if not event:
                return
            await self.handle_message(event)
        finally:
            if self._pending_text_batch_tasks.get(key) is current_task:
                self._pending_text_batch_tasks.pop(key, None)

    async def _collect_media(self, item: Dict[str, Any], media_paths: List[str], media_types: List[str]) -> None:
        spec = _INBOUND_MEDIA.get(item.get("type"))
        if spec is None:
            return
        path, mime = await self._download_media(item, spec)
        if path:
            media_paths.append(path)
            media_types.append(mime)

    async def _download_media(self, item: Dict[str, Any], spec: Tuple[Any, ...]) -> Tuple[Optional[str], str]:
        """Download + decrypt one inbound media item; returns (cached path or None, mime)."""
        item_key, timeout_seconds, cache_fn, mime, label = spec
        payload = item.get(item_key) or {}
        media = payload.get("media") or {}
        filename = str(payload.get("file_name") or "document.bin")
        if mime is None:
            mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        try:
            aes_key_b64 = media.get("aes_key")
            if item_key == "image_item" and payload.get("aeskey"):
                # image_item may carry a raw hex ``aeskey`` beside the media block.
                aes_key_b64 = base64.b64encode(bytes.fromhex(str(payload.get("aeskey")))).decode("ascii") or aes_key_b64
            data = await _download_and_decrypt_media(
                self._poll_session, cdn_base_url=self._cdn_base_url, encrypted_query_param=media.get("encrypt_query_param"),
                aes_key_b64=aes_key_b64, full_url=media.get("full_url"), timeout_seconds=timeout_seconds,
            )
            return cache_fn(data, filename), mime
        except Exception as exc:
            logger.warning("[%s] %s download failed: %s", self.name, label, exc)
            return None, mime

    async def _download_voice(self, item: Dict[str, Any]) -> Optional[str]:
        # Always download raw audio (never trust Tencent's ``voice_item.text``) so
        # gateway/run.py's central STT can re-transcribe with the user's backend.
        return (await self._download_media(item, _INBOUND_MEDIA[ITEM_VOICE]))[0]

    async def _fetch_typing_ticket(self, session: Any, user_id: str, context_token: Optional[str], failure_label: str) -> Optional[str]:
        try:
            response = await _get_config(
                session, base_url=self._base_url, token=self._token, user_id=user_id, context_token=context_token,
            )
            typing_ticket = str(response.get("typing_ticket") or "")
            if typing_ticket:
                self._typing_cache.set(user_id, typing_ticket)
                return typing_ticket
        except Exception as exc:
            logger.debug("[%s] %s for %s: %s", self.name, failure_label, _safe_id(user_id), exc)
        return None

    async def _maybe_fetch_typing_ticket(self, user_id: str, context_token: Optional[str]) -> None:
        if not self._poll_session or not self._token or self._typing_cache.get(user_id):
            return
        await self._fetch_typing_ticket(self._poll_session, user_id, context_token, "getConfig failed")

    def _split_text(self, content: str) -> List[str]:
        return _split_text_for_weixin_delivery(content, self.MAX_MESSAGE_LENGTH, self._split_multiline_messages)

    def _rate_limit_cooldown_remaining(self) -> float:
        return max(0.0, self._rate_limit_circuit_until - time.monotonic())

    def _rate_limit_error(self) -> RuntimeError:
        return RuntimeError(
            f"iLink sendmessage rate limited; cooldown active for {self._rate_limit_cooldown_remaining():.1f}s"
        )

    def _record_rate_limit_event(self) -> bool:
        """Record a genuine iLink rate limit and return True if breaker opened."""
        now = time.monotonic()
        window_start = now - self._rate_limit_circuit_window_seconds
        self._rate_limit_events = [ts for ts in self._rate_limit_events if ts >= window_start]
        self._rate_limit_events.append(now)
        if len(self._rate_limit_events) >= self._rate_limit_circuit_threshold:
            if self._rate_limit_circuit_open_seconds > 0:
                self._rate_limit_circuit_until = max(
                    self._rate_limit_circuit_until, time.monotonic() + self._rate_limit_circuit_open_seconds,
                )
            return self._rate_limit_cooldown_remaining() > 0
        return False

    def _reset_rate_limit_circuit(self) -> None:
        self._rate_limit_events.clear()
        self._rate_limit_circuit_until = 0.0

    async def _send_text_chunk(self, *, chat_id: str, chunk: str, context_token: Optional[str], client_id: str) -> None:
        """Send one text chunk with retry/backoff under the adapter-wide text gate. On session-expired
        (errcode -14) retry once *without* ``context_token`` — iLink accepts tokenless sends as a
        degraded fallback, which keeps cron pushes working when no user message refreshed the session."""
        async with self._send_text_gate:
            await self._send_text_chunk_locked(chat_id=chat_id, chunk=chunk, context_token=context_token, client_id=client_id)

    async def _send_text_chunk_locked(self, *, chat_id: str, chunk: str, context_token: Optional[str], client_id: str) -> None:
        last_error: Optional[Exception] = None
        retried_without_token = False
        for attempt in range(self._send_chunk_retries + 1):
            if self._rate_limit_cooldown_remaining() > 0:
                raise self._rate_limit_error()
            try:
                resp = await _send_message(
                    self._send_session, base_url=self._base_url, token=self._token, to=chat_id,
                    text=chunk, context_token=context_token, client_id=client_id,
                )
                if resp and isinstance(resp, dict):
                    ret = resp.get("ret")
                    errcode = resp.get("errcode")
                    if (ret is not None and ret not in {0}) or (errcode is not None and errcode not in {0}):
                        if _is_session_expired(resp, ret, errcode) and not retried_without_token and context_token:
                            retried_without_token = True
                            context_token = None
                            self._token_store._cache.pop(self._token_store._key(self._account_id, chat_id), None)
                            logger.warning(
                                "[%s] session expired for %s; retrying without context_token", self.name, _safe_id(chat_id),
                            )
                            continue
                        if ret == RATE_LIMIT_ERRCODE or errcode == RATE_LIMIT_ERRCODE:
                            errmsg = resp.get("errmsg") or resp.get("msg") or "rate limited"
                            # Keep a descriptive error for when the loop exhausts while still limited.
                            last_error = RuntimeError(
                                f"iLink sendmessage rate limited: ret={ret} errcode={errcode} errmsg={errmsg}"
                            )
                            if self._record_rate_limit_event():
                                last_error = self._rate_limit_error()
                                break
                            if attempt >= self._send_chunk_retries:
                                break
                            wait = self._send_chunk_retry_delay_seconds * 3  # 3x backoff for rate limit
                            logger.warning(
                                "[%s] rate limited for %s; backing off %.1fs before retry", self.name, _safe_id(chat_id), wait,
                            )
                            await asyncio.sleep(wait)
                            continue
                        errmsg = resp.get("errmsg") or resp.get("msg") or "unknown error"
                        raise RuntimeError(f"iLink sendmessage error: ret={ret} errcode={errcode} errmsg={errmsg}")
                self._reset_rate_limit_circuit()
                return
            except Exception as exc:
                last_error = exc
                if attempt >= self._send_chunk_retries:
                    break
                wait = self._send_chunk_retry_delay_seconds * (attempt + 1)
                logger.warning(
                    "[%s] send chunk failed to=%s attempt=%d/%d, retrying in %.2fs: %s",
                    self.name, _safe_id(chat_id), attempt + 1, self._send_chunk_retries + 1, wait, exc,
                )
                if wait > 0:
                    await asyncio.sleep(wait)
        assert last_error is not None
        raise last_error

    async def send(
        self, chat_id: str, content: str, reply_to: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        if not self._send_session or not self._token:
            return SendResult(success=False, error="Not connected")
        context_token = self._token_store.get(self._account_id, chat_id)
        last_message_id: Optional[str] = None

        # Extract MEDIA: tags and bare local file paths before text delivery.
        media_files, cleaned_content = self.extract_media(content)
        media_files = self.filter_media_delivery_paths(media_files)
        _, image_cleaned = self.extract_images(cleaned_content)
        local_files, final_content = self.extract_local_files(image_cleaned)
        local_files = self.filter_local_delivery_paths(local_files)

        deliveries = [(p, v, "media") for p, v in media_files] + [(p, False, "local file") for p in local_files]
        try:
            for path, is_voice, label in deliveries:
                ext = Path(path).suffix.lower()
                if is_voice or ext in _AUDIO_EXTS:
                    sender, key = self.send_voice, "audio_path"
                elif ext in _VIDEO_EXTS:
                    sender, key = self.send_video, "video_path"
                elif ext in _IMAGE_EXTS:
                    sender, key = self.send_image_file, "image_path"
                else:
                    sender, key = self.send_document, "file_path"
                try:
                    await sender(chat_id=chat_id, metadata=metadata, **{key: path})
                except Exception as exc:
                    logger.warning("[%s] %s delivery failed for %s: %s", self.name, label, path, exc)
            chunks = [c for c in self._split_text(self.format_message(final_content)) if c and c.strip()]
            for idx, chunk in enumerate(chunks):
                client_id = f"hermes-weixin-{uuid.uuid4().hex}"
                await self._send_text_chunk(chat_id=chat_id, chunk=chunk, context_token=context_token, client_id=client_id)
                last_message_id = client_id
                if idx < len(chunks) - 1 and self._send_chunk_delay_seconds > 0:
                    await asyncio.sleep(self._send_chunk_delay_seconds)
            return SendResult(success=True, message_id=last_message_id)
        except Exception as exc:
            logger.error("[%s] send failed to=%s: %s", self.name, _safe_id(chat_id), exc)
            return SendResult(success=False, error=str(exc))

    async def _ensure_typing_ticket(self, chat_id: str) -> Optional[str]:
        """Return a valid typing ticket, refreshing via getConfig once the 600s TTL evicts it —
        otherwise ``stop_typing`` no-ops and the WeChat client shows the indicator forever."""
        ticket = self._typing_cache.get(chat_id)
        if ticket:
            return ticket
        if not self._send_session or not self._token:
            return None
        context_token = self._token_store.get(self._account_id, chat_id)
        return await self._fetch_typing_ticket(self._send_session, chat_id, context_token, "typing ticket refresh failed")

    async def _set_typing(self, chat_id: str, status: int, label: str) -> None:
        if not self._send_session or not self._token:
            return
        typing_ticket = await self._ensure_typing_ticket(chat_id)
        if not typing_ticket:
            return
        try:
            await _api_post(
                self._send_session, base_url=self._base_url, endpoint=EP_SEND_TYPING,
                payload={"ilink_user_id": chat_id, "typing_ticket": typing_ticket, "status": status},
                token=self._token, timeout_ms=CONFIG_TIMEOUT_MS,
            )
        except Exception as exc:
            logger.debug("[%s] typing %s failed for %s: %s", self.name, label, _safe_id(chat_id), exc)

    async def send_typing(self, chat_id: str, metadata: Optional[Dict[str, Any]] = None) -> None:
        await self._set_typing(chat_id, TYPING_START, "start")

    async def stop_typing(self, chat_id: str) -> None:
        await self._set_typing(chat_id, TYPING_STOP, "stop")

    async def send_image(
        self, chat_id: str, image_url: str, caption: str, reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        if image_url.startswith(("http://", "https://")):
            file_path = await self._download_remote_media(image_url)
            cleanup = True
        else:
            file_path = image_url.replace("file://", "")
            if not os.path.isabs(file_path):
                file_path = os.path.abspath(file_path)
            cleanup = False
        try:
            return await self.send_document(chat_id, file_path, caption=caption, metadata=metadata)
        finally:
            if cleanup and file_path and os.path.exists(file_path):
                with contextlib.suppress(OSError):
                    os.unlink(file_path)

    async def send_image_file(
        self, chat_id: str, image_path: str, caption: Optional[str] = None, reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None, **kwargs,
    ) -> SendResult:
        del reply_to, kwargs
        return await self.send_document(chat_id=chat_id, file_path=image_path, caption=caption, metadata=metadata)

    async def _send_file_result(self, chat_id: str, path: str, caption: str, label: str, **kwargs: Any) -> SendResult:
        if not self._send_session or not self._token:
            return SendResult(success=False, error="Not connected")
        try:
            message_id = await self._send_file(chat_id, path, caption, **kwargs)
            return SendResult(success=True, message_id=message_id)
        except Exception as exc:
            logger.error("[%s] %s failed to=%s: %s", self.name, label, _safe_id(chat_id), exc)
            return SendResult(success=False, error=str(exc))

    async def send_document(
        self, chat_id: str, file_path: str, caption: Optional[str] = None, file_name: Optional[str] = None,
        reply_to: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None, **kwargs,
    ) -> SendResult:
        del file_name, reply_to, metadata, kwargs
        return await self._send_file_result(chat_id, file_path, caption or "", "send_document")

    async def send_video(
        self, chat_id: str, video_path: str, caption: Optional[str] = None, reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        return await self._send_file_result(chat_id, video_path, caption or "", "send_video")

    async def send_voice(
        self, chat_id: str, audio_path: str, caption: Optional[str] = None, reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        # Native outbound voice bubbles are not proven-working upstream; send a
        # file attachment so users at least receive playable audio (even .silk).
        return await self._send_file_result(
            chat_id, audio_path, caption or "[voice message as attachment]", "send_voice", force_file_attachment=True,
        )

    async def _download_remote_media(self, url: str) -> str:
        from tools.url_safety import is_safe_url
        if not is_safe_url(url):
            raise ValueError(f"Blocked unsafe URL (SSRF protection): {url}")
        assert self._send_session is not None
        data = await _download_bytes(self._send_session, url=url, timeout_seconds=30)
        suffix = Path(url.split("?", 1)[0]).suffix or ".bin"
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as handle:
            handle.write(data)
            return handle.name

    async def _send_file(self, chat_id: str, path: str, caption: str, force_file_attachment: bool = False) -> str:
        assert self._send_session is not None and self._token is not None
        plaintext = Path(path).read_bytes()
        media_type, item_builder = self._outbound_media_builder(path, force_file_attachment=force_file_attachment)
        filekey = secrets.token_hex(16)
        aes_key = secrets.token_bytes(16)
        rawsize = len(plaintext)
        rawfilemd5 = hashlib.md5(plaintext).hexdigest()
        upload_response = await _get_upload_url(
            self._send_session, base_url=self._base_url, token=self._token, to_user_id=chat_id,
            media_type=media_type, filekey=filekey, rawsize=rawsize, rawfilemd5=rawfilemd5,
            filesize=_aes_padded_size(rawsize), aeskey_hex=aes_key.hex(),
        )
        upload_param = str(upload_response.get("upload_param") or "")
        upload_full_url = str(upload_response.get("upload_full_url") or "")
        ciphertext = _aes128_ecb_encrypt(plaintext, aes_key)

        # Prefer upload_full_url (direct CDN), else construct from upload_param.
        # Both use POST — PUT to upload_full_url 404s on the WeChat CDN.
        upload_url = upload_full_url or (upload_param and _cdn_upload_url(self._cdn_base_url, upload_param, filekey))
        if not upload_url:
            raise RuntimeError(f"getUploadUrl returned neither upload_param nor upload_full_url: {upload_response}")
        encrypted_query_param = await _upload_ciphertext(self._send_session, ciphertext=ciphertext, upload_url=upload_url)
        context_token = self._token_store.get(self._account_id, chat_id)
        # iLink expects aes_key as base64(hex_string), not base64(raw_bytes) —
        # otherwise images render as grey boxes because the key doesn't match.
        item_kwargs = {
            "encrypt_query_param": encrypted_query_param,
            "aes_key_for_api": base64.b64encode(aes_key.hex().encode("ascii")).decode("ascii"),
            "ciphertext_size": len(ciphertext),
            "plaintext_size": rawsize,
            "filename": Path(path).name,
            "rawfilemd5": rawfilemd5,
        }
        if media_type == MEDIA_VOICE and path.endswith(".silk"):
            item_kwargs.update(encode_type=6, sample_rate=24000, bits_per_sample=16)
        media_item = item_builder(**item_kwargs)
        if caption:
            await _send_message(
                self._send_session, base_url=self._base_url, token=self._token, to=chat_id,
                text=self.format_message(caption), context_token=context_token,
                client_id=f"hermes-weixin-{uuid.uuid4().hex}",
            )
        last_message_id = f"hermes-weixin-{uuid.uuid4().hex}"
        await _send_items(
            self._send_session, base_url=self._base_url, token=self._token, to=chat_id,
            item_list=[media_item], context_token=context_token, client_id=last_message_id,
        )
        return last_message_id

    def _outbound_media_builder(self, path: str, force_file_attachment: bool = False):
        """Return (iLink media_type, item builder) for an outbound file."""
        mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
        if mime.startswith("image/"):
            return MEDIA_IMAGE, _image_item
        if mime.startswith("video/"):
            return MEDIA_VIDEO, _video_item
        if path.endswith(".silk") and not force_file_attachment:
            return MEDIA_VOICE, _voice_item
        return MEDIA_FILE, _file_item  # audio/* and everything else ship as file attachments

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        chat_type = "group" if chat_id.endswith("@chatroom") else "dm"
        return {"name": chat_id, "type": chat_type, "chat_id": chat_id}

    def format_message(self, content: Optional[str]) -> str:
        if content is None:
            return ""
        return _wrap_copy_friendly_lines_for_weixin(_normalize_markdown_blocks(content))


async def _deliver_direct(
    adapter: WeixinAdapter, chat_id: str, message: str,
    media_files: Optional[List[Tuple[str, bool]]], context_token: Optional[str],
) -> Dict[str, Any]:
    last_result: Optional[SendResult] = None
    cleaned = adapter.format_message(message)
    if cleaned:
        last_result = await adapter.send(chat_id, cleaned)
        if not last_result.success:
            return {"error": f"Weixin send failed: {last_result.error}"}
    for media_path, _is_voice in media_files or []:
        if Path(media_path).suffix.lower() in _DIRECT_IMAGE_EXTS:
            last_result = await adapter.send_image_file(chat_id, media_path)
        else:
            last_result = await adapter.send_document(chat_id, media_path)
        if not last_result.success:
            return {"error": f"Weixin media send failed: {last_result.error}"}
    return {
        "success": True,
        "platform": "weixin",
        "chat_id": chat_id,
        "message_id": last_result.message_id if last_result else None,
        "context_token_used": bool(context_token),
    }


async def send_weixin_direct(
    *, extra: Dict[str, Any], token: Optional[str], chat_id: str, message: str,
    media_files: Optional[List[Tuple[str, bool]]] = None,
) -> Dict[str, Any]:
    """One-shot send helper for ``send_message`` and cron delivery.

    Reuses the live adapter's session when one is connected on this loop;
    otherwise builds a throwaway adapter over a fresh session.
    """
    account_id = _extra_or_secret(extra, "account_id", "WEIXIN_ACCOUNT_ID")
    base_url = _extra_or_secret(extra, "base_url", "WEIXIN_BASE_URL", ILINK_BASE_URL).rstrip("/")
    cdn_base_url = _extra_or_secret(extra, "cdn_base_url", "WEIXIN_CDN_BASE_URL", WEIXIN_CDN_BASE_URL).rstrip("/")
    resolved_token = str(token or extra.get("token") or _wx_secret("WEIXIN_TOKEN", "")).strip()
    if not resolved_token:
        return {"error": "Weixin token missing. Configure WEIXIN_TOKEN or platforms.weixin.token."}
    if not account_id:
        return {"error": "Weixin account ID missing. Configure WEIXIN_ACCOUNT_ID or platforms.weixin.extra.account_id."}
    token_store = ContextTokenStore(str(get_hermes_home()))
    token_store.restore(account_id)
    context_token = token_store.get(account_id, chat_id)
    live_adapter = _LIVE_ADAPTERS.get(resolved_token)
    send_session = getattr(live_adapter, '_send_session', None)
    if (live_adapter is not None and send_session is not None
            and not send_session.closed
            and send_session._loop is asyncio.get_running_loop()):
        return await _deliver_direct(live_adapter, chat_id, message, media_files, context_token)
    async with _new_session() as session:
        adapter = WeixinAdapter(
            PlatformConfig(
                enabled=True,
                token=resolved_token,
                extra={**dict(extra or {}), "account_id": account_id, "base_url": base_url, "cdn_base_url": cdn_base_url},
            )
        )
        adapter._send_session = adapter._session = session
        adapter._token, adapter._account_id = resolved_token, account_id
        adapter._base_url, adapter._cdn_base_url = base_url, cdn_base_url
        adapter._token_store = token_store
        return await _deliver_direct(adapter, chat_id, message, media_files, context_token)
