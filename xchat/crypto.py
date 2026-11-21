"""Crypto core for the X Chat platform adapter.

A thin, network-free wrapper around the ``chat_xdk`` binding. Everything
that touches the Chat XDK lives here so it can be unit-tested with a fake
``Chat`` object and so the adapter/API layers stay import-light. The SDK is
declared in this plugin's ``pyproject.toml``; the plugin install prepares it.

Responsibilities:

* key management        -> :meth:`XChatCrypto.load_keys` /
                           :meth:`XChatCrypto.generate_and_register_payload`
* session identity      -> :meth:`XChatCrypto.set_identity`
* signing-key roster    -> :meth:`XChatCrypto.set_signing_keys`
* message encryption    -> :meth:`XChatCrypto.encrypt_text` (with optional
                           attachments) and :meth:`XChatCrypto.encrypt_reply`
* event decryption      -> :meth:`XChatCrypto.decrypt_batch` (decrypt_events)
                           and :meth:`XChatCrypto.decrypt_one` (decrypt_event)
* media encryption      -> :meth:`XChatCrypto.encrypt_media` /
                           :meth:`XChatCrypto.decrypt_media` (stream cipher
                           under the conversation key)
* conversation keys     -> :meth:`XChatCrypto.prepare_conversation_key_change`
                           + :meth:`XChatCrypto.verify_key_binding` (initiate
                           brand-new conversations / rotate keys)

The decrypted-event dict shape follows the Chat XDK: ``{"type": "Message",
"id": ..., "sender_id": ..., "conversation_id": ..., "content": {"text":
...}}`` for messages, ``{"type": "KeyChange", ...}`` for conversation-key
rotations.
"""
from __future__ import annotations

import base64
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


def _as_dict(obj: Any) -> dict[str, Any]:
    """Decrypted events come back as native objects; normalise to a dict."""
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    try:
        return dict(obj)
    except Exception:
        return {}


def _load_chat_xdk():
    """Return the ``chat_xdk`` module (``chatxdk``, declared in this plugin's pyproject)."""
    try:
        import chat_xdk  # type: ignore[import-not-found]
    except ImportError as exc:
        raise ImportError(
            "X Chat needs the chatxdk package. Reinstall the plugin with its dependencies "
            "(hermes plugins install xchat), or run: hermes pm repair"
        ) from exc
    return chat_xdk


def _load_chat_class():
    """Return ``chat_xdk.Chat``."""
    return _load_chat_xdk().Chat


def detect_mime_type(data: bytes) -> Optional[str]:
    """MIME sniff on PLAINTEXT bytes (Chat XDK helper)."""
    try:
        return _load_chat_xdk().detect_mime_type(bytes(data))
    except Exception:
        return None


def detect_image_dimensions(data: bytes) -> Optional[tuple[int, int]]:
    """(width, height) of PLAINTEXT image bytes, or None (Chat XDK helper)."""
    try:
        dims = _load_chat_xdk().detect_image_dimensions(bytes(data))
    except Exception:
        return None
    if not dims:
        return None
    try:
        # Bindings return either a (w, h) tuple or an object with attributes.
        if isinstance(dims, (tuple, list)):
            return int(dims[0]), int(dims[1])
        return int(dims.width), int(dims.height)
    except Exception:
        return None


class XChatCrypto:
    """Wraps a single unlocked ``chat_xdk.Chat`` instance for one bot identity."""

    def __init__(self, chat: Any = None) -> None:
        # ``chat`` injection keeps unit tests free of the native SDK.
        self.chat = chat if chat is not None else _load_chat_class()()
        self.signing_key_version: str = "1"
        self._identity_set = False

    # -- Key management -----------------------------------------------------

    def load_keys(self, private_keys_b64: str, signing_key_version: str = "1") -> None:
        """Import an existing private-key blob (from ``export_keys``) and adopt it.

        ``private_keys_b64`` is the base64 blob produced during registration
        (``hermes xchat setup``). Raises on a malformed blob.
        """
        blob = base64.b64decode(private_keys_b64.strip())
        self.chat.import_keys(blob, version=signing_key_version)
        self.signing_key_version = str(signing_key_version)

    def set_identity(self, user_id: str) -> None:
        """Set the session identity — every later encrypt call signs as this user."""
        self.chat.set_identity(str(user_id), self.signing_key_version)
        self._identity_set = True

    def set_cache_keys(self, enabled: bool = True) -> None:
        """Opt in to the SDK's verified conversation-key cache."""
        self.chat.set_cache_keys(enabled)

    def set_signing_keys(self, signing_keys: list[dict[str, str]]) -> None:
        """Replace the SDK's participant signing-key store (full roster each call)."""
        self.chat.set_signing_keys(signing_keys)

    def generate_and_register_payload(self) -> dict[str, Any]:
        """Generate fresh keypairs for a brand-new bot identity.

        Returns the registration body for ``POST /2/users/{id}/public_keys``
        plus the exported private-key blob (base64) to persist locally.
        Used by ``hermes xchat setup`` only — the adapter never generates keys.
        """
        reg = self.chat.generate_keypairs()
        version = str(reg.version) if getattr(reg, "version", None) is not None else "1"
        body = {
            "public_key": {
                "public_key": reg.public_key.public_key,
                "signing_public_key": reg.public_key.signing_public_key,
                "identity_public_key_signature": reg.public_key.identity_public_key_signature,
                "signing_public_key_signature": reg.public_key.signing_public_key_signature,
                "registration_method": reg.public_key.registration_method,
            },
            "version": version,
            "generate_version": bool(getattr(reg, "generate_version", False)),
        }
        exported = self.chat.export_keys()
        blob_b64 = base64.b64encode(bytes(exported)).decode("ascii") if exported else ""
        return {"registration": body, "version": version, "private_keys_b64": blob_b64}

    def verify_key_binding(
        self,
        identity_public_key_b64: str,
        signing_public_key_b64: str,
        identity_public_key_signature_b64: str,
    ) -> bool:
        """Verify a fetched public-key record's identity↔signing binding.

        MUST be called on every record before wrapping a conversation key to
        it (``prepare_conversation_key_change`` encrypts to whatever you pass
        — a substituted identity key would silently receive the key).
        """
        try:
            return bool(
                self.chat.verify_key_binding(
                    identity_public_key_b64,
                    signing_public_key_b64,
                    identity_public_key_signature_b64,
                )
            )
        except Exception:
            return False

    # -- Conversation-key setup (initiation / rotation) -----------------------

    def prepare_conversation_key_change(
        self,
        public_keys: list[dict[str, str]],
        *,
        conversation_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """Generate + wrap a fresh conversation key for every participant.

        ``public_keys``: ``[{"user_id", "public_key", "key_version"}, ...]``
        (verified via :meth:`verify_key_binding` first). Returns the API body
        for ``POST /2/chat/conversations/{id}/keys`` plus the raw key:

        ``{"body": {...}, "conversation_key": bytes,
          "conversation_key_version": str}``
        """
        prepared = _as_dict(
            self.chat.prepare_conversation_key_change(
                public_keys, conversation_id=conversation_id
            )
        )
        body = {
            "conversation_key_version": prepared["conversation_key_version"],
            "conversation_participant_keys": [
                {
                    "user_id": pk["user_id"],
                    "encrypted_conversation_key": pk["encrypted_key"],
                    "public_key_version": pk["public_key_version"],
                }
                for pk in (prepared.get("participant_keys") or [])
            ],
            "action_signatures": [
                {
                    "message_id": sig["message_id"],
                    "encoded_message_event_detail": sig["encoded_message_event_detail"],
                    "message_event_signature": {
                        "signature": sig["signature"],
                        "public_key_version": sig["public_key_version"],
                        "signature_version": sig["signature_version"],
                    },
                }
                for sig in (prepared.get("action_signatures") or [])
            ],
        }
        return {
            "body": body,
            "conversation_key": prepared.get("conversation_key"),
            "conversation_key_version": str(prepared["conversation_key_version"]),
        }

    # -- Decryption ----------------------------------------------------------

    def decrypt_batch(self, events_b64: list[str]) -> dict[str, Any]:
        """Batch path — initial backlog load and KeyChange processing.

        ``decrypt_events`` extracts conversation keys from any KeyChange
        events in the batch (feeding the SDK's key cache when enabled), then
        decrypts every message. Signing keys come from the
        ``set_signing_keys`` store.

        NOTE: the events endpoint returns KeyChange events SEPARATELY in
        ``meta.conversation_key_events`` — callers must prepend those to the
        batch or no conversation key is ever extracted.
        """
        result = self.chat.decrypt_events(events_b64, None)
        messages = [
            {"event": _as_dict(m.get("event") if isinstance(m, dict) else m)}
            for m in (result.get("messages") or [])
        ]
        conv_keys = result.get("conversation_keys") or {}
        return {
            "messages": messages,
            "conversation_keys": conv_keys,
            "latest_key_version": (
                str(conv_keys.get("latest_version"))
                if conv_keys.get("latest_version") is not None
                else None
            ),
            "errors": result.get("errors") or {},
        }

    def decrypt_one(
        self, event_b64: str, conversation_keys: Optional[dict[str, bytes]] = None
    ) -> dict[str, Any]:
        """Single-event path — per-poll decryption with cached conversation keys."""
        return _as_dict(self.chat.decrypt_event(event_b64, conversation_keys, None))

    # -- Encryption ----------------------------------------------------------

    def encrypt_text(
        self,
        conversation_id: str,
        text: str,
        *,
        attachments: Optional[list[dict[str, Any]]] = None,
        conversation_key: Optional[bytes] = None,
        conversation_key_version: Optional[str] = None,
    ) -> dict[str, str]:
        """Encrypt + sign ``text`` (optionally with media attachments).

        Returns the X API send-message body. The conversation key is resolved
        from the SDK's verified-key cache (``set_cache_keys``) unless an
        explicit ``conversation_key`` + version pair is given (used right
        after key initiation, before any KeyChange event has been polled).
        Raises ``ValueError`` when no verified key is available.
        """
        kwargs: dict[str, Any] = {}
        if attachments:
            kwargs["attachments"] = attachments
        if conversation_key is not None:
            kwargs["conversation_key"] = conversation_key
            kwargs["conversation_key_version"] = conversation_key_version
        payload = self.chat.encrypt_message(str(conversation_id), text, **kwargs)
        return {
            "message_id": payload.message_id,
            "encoded_message_create_event": payload.encrypted_content,
            "encoded_message_event_signature": payload.encoded_event_signature,
        }

    def encrypt_reply(
        self,
        conversation_id: str,
        text: str,
        reply_to_event: str,
        *,
        attachments: Optional[list[dict[str, Any]]] = None,
    ) -> dict[str, str]:
        """Encrypt + sign ``text`` as a native threaded reply.

        ``reply_to_event`` is the base64 ``encoded_event`` of the message being
        replied to, as the events endpoint served it; the SDK verifies it and
        derives the reply preview from it.
        """
        kwargs: dict[str, Any] = {}
        if attachments:
            kwargs["attachments"] = attachments
        payload = self.chat.encrypt_reply(
            str(conversation_id), text, reply_to_event=reply_to_event, **kwargs
        )
        return {
            "message_id": payload.message_id,
            "encoded_message_create_event": payload.encrypted_content,
            "encoded_message_event_signature": payload.encoded_event_signature,
        }

    # -- Media (stream cipher under the conversation key) ---------------------

    def encrypt_media(self, plaintext: bytes, conversation_key: bytes) -> bytes:
        """Encrypt attachment bytes for upload (whole payload in memory)."""
        return bytes(self.chat.encrypt_stream(bytes(plaintext), conversation_key))

    def decrypt_media(self, ciphertext: bytes, conversation_key: bytes) -> bytes:
        """Decrypt a downloaded attachment blob.

        The key MUST be the conversation key for the *event's* key version —
        after a rotation, the latest key cannot decrypt older media.
        """
        return bytes(self.chat.decrypt_stream(bytes(ciphertext), conversation_key))


def message_text(event: dict[str, Any]) -> Optional[str]:
    """Pull the plain text out of a decrypted Message/MessageEdit event.

    Edits matter: the feed sometimes returns an edited message only as the
    edit event (the original is dropped), so skipping edits would make the
    message invisible to the bot forever.
    """
    if event.get("type") not in ("Message", "MessageEdit"):
        return None
    content = event.get("content") or {}
    if not isinstance(content, dict):
        return None
    text = content.get("text")
    # A shared post is a card in the attachments, not media: message_attachments only keeps
    # entries with media_hash_key, so the card was dropped and the typed comment reached the
    # agent with no referent (#477). Append its canonical URL so the existing post reader sees it.
    urls = []
    raw = content.get("attachments") or event.get("attachments") or []
    for att in raw if isinstance(raw, (list, tuple)) else []:
        att = _as_dict(att)
        post = att.get("post")
        if not isinstance(post, dict):
            continue
        url = str(post.get("post_url") or "").strip()
        if url.startswith("https://x.com/") and url not in urls:
            urls.append(url)
    if not urls:
        return text
    extra = " ".join(urls)
    return extra if not text else f"{text}\n{extra}"


def message_attachments(event: dict[str, Any]) -> list[dict[str, Any]]:
    """Media attachment descriptors from a decrypted Message/MessageEdit event.

    Each entry carries ``media_hash_key`` (+ optional filename/width/height/
    filesize_bytes). Returns [] for non-message events or text-only messages.
    """
    if event.get("type") not in ("Message", "MessageEdit"):
        return []
    content = event.get("content") or {}
    if not isinstance(content, dict):
        return []
    raw = content.get("attachments") or event.get("attachments") or []
    out: list[dict[str, Any]] = []
    for att in raw if isinstance(raw, (list, tuple)) else []:
        att = _as_dict(att)
        att = _as_dict(att.get("media")) if att.get("media") else att
        if att.get("media_hash_key"):
            out.append(att)
    return out
