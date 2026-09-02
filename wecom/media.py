"""WeCom media: inbound attachment caching and outbound upload/send.

Mixed into :class:`WeComAdapter`. Outbound media goes through the chunked
``aibot_upload_media_*`` flow and is then sent natively (image/video/voice/file).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import mimetypes
import re
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import unquote, urlparse

from gateway.platforms.base import SendResult, cache_document_from_bytes, cache_image_from_bytes

logger = logging.getLogger("plugins.platforms.wecom.adapter")

APP_CMD_SEND = "aibot_send_msg"
APP_CMD_UPLOAD_MEDIA_INIT = "aibot_upload_media_init"
APP_CMD_UPLOAD_MEDIA_CHUNK = "aibot_upload_media_chunk"
APP_CMD_UPLOAD_MEDIA_FINISH = "aibot_upload_media_finish"

IMAGE_MAX_BYTES = 10 * 1024 * 1024
VIDEO_MAX_BYTES = 10 * 1024 * 1024
VOICE_MAX_BYTES = 2 * 1024 * 1024
FILE_MAX_BYTES = 20 * 1024 * 1024
ABSOLUTE_MAX_BYTES = FILE_MAX_BYTES
UPLOAD_CHUNK_SIZE = 512 * 1024
MAX_UPLOAD_CHUNKS = 100
VOICE_SUPPORTED_MIMES = {"audio/amr"}


def _size_verdict(final_type: str, *, reject: Optional[str] = None, downgrade: Optional[str] = None) -> Dict[str, Any]:
    return {
        "final_type": final_type,
        "rejected": reject is not None,
        "reject_reason": reject,
        "downgraded": downgrade is not None,
        "downgrade_note": downgrade,
    }


class WeComMediaMixin:
    """Media helpers for WeComAdapter (expects ``_http_client``, ``_send_request``,
    ``_send_reply_request``, ``send``, ``_reply_req_id_for_message``,
    ``_last_chat_req_ids``, ``_stream_expired_chats``, ``_find_active_turn_for_chat``)."""

    # ── Inbound ──────────────────────────────────────────────────────────

    async def _extract_media(self, body: Dict[str, Any]) -> Tuple[List[str], List[str]]:
        """Best-effort extraction of inbound media to local cache paths."""
        refs: List[Tuple[str, Dict[str, Any]]] = []
        msgtype = str(body.get("msgtype") or "").lower()

        def _ref(kind: str, container: Dict[str, Any]) -> bool:
            if isinstance(container.get(kind), dict):
                refs.append((kind, container[kind]))
                return True
            return False

        if msgtype == "mixed":
            mixed = body.get("mixed") if isinstance(body.get("mixed"), dict) else {}
            items = mixed.get("msg_item") if isinstance(mixed.get("msg_item"), list) else []
            for item in items:
                if isinstance(item, dict) and str(item.get("msgtype") or "").lower() == "image":
                    _ref("image", item)
        else:
            _ref("image", body)
            if msgtype == "file":
                _ref("file", body)
            # appmsg = WeCom AI Bot attachments (PDF/Word/Excel)
            if msgtype == "appmsg" and isinstance(body.get("appmsg"), dict):
                _ref("file", body["appmsg"]) or _ref("image", body["appmsg"])

        quote = body.get("quote") if isinstance(body.get("quote"), dict) else {}
        quote_type = str(quote.get("msgtype") or "").lower()
        if quote_type in ("image", "file"):
            _ref(quote_type, quote)

        media_paths: List[str] = []
        media_types: List[str] = []
        for kind, ref in refs:
            cached = await self._cache_media(kind, ref)
            if cached:
                media_paths.append(cached[0])
                media_types.append(cached[1])
        return media_paths, media_types

    async def _cache_media(self, kind: str, media: Dict[str, Any]) -> Optional[Tuple[str, str]]:
        """Cache an inbound image/file reference (inline base64 or URL) to local storage."""
        if media.get("base64"):
            try:
                raw = self._decode_base64(media["base64"])
            except Exception as exc:
                logger.debug("[%s] Failed to decode %s base64 media: %s", self.name, kind, exc)
                return None
            if kind == "image":
                ext = self._detect_image_ext(raw)
                return self._cache_image(raw, ext, self._mime_for_ext(ext, fallback="image/jpeg"), "")
            filename = str(media.get("filename") or media.get("name") or "wecom_file")
            return cache_document_from_bytes(raw, filename), mimetypes.guess_type(filename)[0] or "application/octet-stream"

        url = str(media.get("url") or "").strip()
        if not url:
            return None
        try:
            raw, headers = await self._download_remote_bytes(url, max_bytes=ABSOLUTE_MAX_BYTES)
        except Exception as exc:
            logger.debug("[%s] Failed to download %s from %s: %s", self.name, kind, url, exc)
            return None
        aes_key = str(media.get("aeskey") or "").strip()
        if aes_key:
            try:
                raw = self._decrypt_file_bytes(raw, aes_key)
            except Exception as exc:
                logger.debug("[%s] Failed to decrypt %s from %s: %s", self.name, kind, url, exc)
                return None
        content_type = str(headers.get("content-type") or "").split(";", 1)[0].strip() or "application/octet-stream"
        if kind == "image":
            ext = self._guess_extension(url, content_type, fallback=self._detect_image_ext(raw))
            return self._cache_image(raw, ext, content_type or self._mime_for_ext(ext, fallback="image/jpeg"), f" from {url}")
        filename = self._guess_filename(url, headers.get("content-disposition"), content_type)
        return cache_document_from_bytes(raw, filename), content_type

    def _cache_image(self, raw: bytes, ext: str, mime: str, origin: str) -> Optional[Tuple[str, str]]:
        try:
            return cache_image_from_bytes(raw, ext), mime
        except ValueError as exc:
            logger.warning("[%s] Rejected non-image bytes%s: %s", self.name, origin, exc)
            return None

    @staticmethod
    def _decode_base64(data: str) -> bytes:
        return base64.b64decode(data.split(",", 1)[-1].strip())

    @staticmethod
    def _detect_image_ext(data: bytes) -> str:
        if data.startswith(b"\x89PNG\r\n\x1a\n"):
            return ".png"
        if data.startswith(b"\xff\xd8\xff"):
            return ".jpg"
        if data.startswith((b"GIF87a", b"GIF89a")):
            return ".gif"
        if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
            return ".webp"
        return ".jpg"

    @staticmethod
    def _mime_for_ext(ext: str, fallback: str = "application/octet-stream") -> str:
        return mimetypes.types_map.get(ext.lower(), fallback)

    @staticmethod
    def _guess_extension(url: str, content_type: str, fallback: str) -> str:
        ext = mimetypes.guess_extension(content_type) if content_type else None
        return ext or Path(urlparse(url).path).suffix or fallback

    @staticmethod
    def _guess_filename(url: str, content_disposition: Optional[str], content_type: str) -> str:
        if content_disposition:
            match = re.search(r'filename="?([^";]+)"?', content_disposition)
            if match:
                return match.group(1)
        name = Path(urlparse(url).path).name or "document"
        if "." not in name:
            name = f"{name}{mimetypes.guess_extension(content_type) or '.bin'}"
        return name

    @staticmethod
    def _decrypt_file_bytes(encrypted_data: bytes, aes_key: str) -> bytes:
        if not encrypted_data:
            raise ValueError("encrypted_data is empty")
        if not aes_key:
            raise ValueError("aes_key is required")
        # WeCom doesn't pad base64 keys; add padding if needed
        aes_key = aes_key + '=' * ((4 - len(aes_key) % 4) % 4)
        key = base64.b64decode(aes_key)
        if len(key) != 32:
            raise ValueError(f"Invalid WeCom AES key length: expected 32 bytes, got {len(key)}")
        try:
            from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        except ImportError as exc:  # pragma: no cover - dependency is environment-specific
            raise RuntimeError("cryptography is required for WeCom media decryption") from exc
        decryptor = Cipher(algorithms.AES(key), modes.CBC(key[:16])).decryptor()
        decrypted = decryptor.update(encrypted_data) + decryptor.finalize()
        pad_len = decrypted[-1]
        if pad_len < 1 or pad_len > 32 or pad_len > len(decrypted):
            raise ValueError(f"Invalid PKCS#7 padding value: {pad_len}")
        if any(byte != pad_len for byte in decrypted[-pad_len:]):
            raise ValueError("Invalid PKCS#7 padding: padding bytes mismatch")
        return decrypted[:-pad_len]

    async def _download_remote_bytes(self, url: str, max_bytes: int) -> Tuple[bytes, Dict[str, str]]:
        from gateway.platforms.base import _ssrf_redirect_guard
        from tools.url_safety import create_ssrf_safe_async_client, is_safe_url
        from plugins.platforms.wecom import adapter as _adapter_mod

        if not is_safe_url(url):
            raise ValueError(f"Blocked unsafe URL (SSRF protection): {url[:80]}")
        if not _adapter_mod.HTTPX_AVAILABLE:
            raise RuntimeError("httpx is required for WeCom media download")

        client = self._http_client or create_ssrf_safe_async_client(
            timeout=30.0, follow_redirects=True, event_hooks={"response": [_ssrf_redirect_guard]},
        )
        created_client = client is not self._http_client
        try:
            async with client.stream(
                "GET", url, headers={"User-Agent": "HermesAgent/1.0", "Accept": "*/*"},
            ) as response:
                response.raise_for_status()
                headers = {key.lower(): value for key, value in response.headers.items()}
                content_length = headers.get("content-length")
                if content_length and content_length.isdigit() and int(content_length) > max_bytes:
                    raise ValueError(
                        f"Remote media exceeds WeCom limit: {int(content_length)} bytes > {max_bytes} bytes"
                    )
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > max_bytes:
                        raise ValueError(
                            f"Remote media exceeds WeCom limit while downloading: {len(data)} bytes > {max_bytes} bytes"
                        )
                return bytes(data), headers
        finally:
            if created_client:
                await client.aclose()

    # ── Outbound classification ──────────────────────────────────────────

    @staticmethod
    def _guess_mime_type(filename: str) -> str:
        mime_type = mimetypes.guess_type(filename)[0]
        if mime_type:
            return mime_type
        if Path(filename).suffix.lower() == ".amr":
            return "audio/amr"
        return "application/octet-stream"

    @staticmethod
    def _normalize_content_type(content_type: str, filename: str) -> str:
        normalized = str(content_type or "").split(";", 1)[0].strip().lower()
        if not normalized or normalized in {"application/octet-stream", "text/plain"}:
            return WeComMediaMixin._guess_mime_type(filename)
        return normalized

    @staticmethod
    def _detect_wecom_media_type(content_type: str) -> str:
        mime_type = str(content_type or "").strip().lower()
        if mime_type.startswith("image/"):
            return "image"
        if mime_type.startswith("video/"):
            return "video"
        if mime_type.startswith("audio/") or mime_type == "application/ogg":
            return "voice"
        return "file"

    @staticmethod
    def _apply_file_size_limits(file_size: int, detected_type: str, content_type: Optional[str] = None) -> Dict[str, Any]:
        file_size_mb = file_size / (1024 * 1024)
        normalized_type = str(detected_type or "file").lower()
        normalized_content_type = str(content_type or "").strip().lower()
        if file_size > ABSOLUTE_MAX_BYTES:
            return _size_verdict(normalized_type, reject=(
                f"文件大小 {file_size_mb:.2f}MB 超过了企业微信允许的最大限制 20MB，无法发送。"
                "请尝试压缩文件或减小文件大小。"
            ))
        if normalized_type == "image" and file_size > IMAGE_MAX_BYTES:
            return _size_verdict("file", downgrade=f"图片大小 {file_size_mb:.2f}MB 超过 10MB 限制，已转为文件格式发送")
        if normalized_type == "video" and file_size > VIDEO_MAX_BYTES:
            return _size_verdict("file", downgrade=f"视频大小 {file_size_mb:.2f}MB 超过 10MB 限制，已转为文件格式发送")
        if normalized_type == "voice":
            if normalized_content_type and normalized_content_type not in VOICE_SUPPORTED_MIMES:
                return _size_verdict("file", downgrade=(
                    f"语音格式 {normalized_content_type} 不支持，企微仅支持 AMR 格式，已转为文件格式发送"
                ))
            if file_size > VOICE_MAX_BYTES:
                return _size_verdict("file", downgrade=f"语音大小 {file_size_mb:.2f}MB 超过 2MB 限制，已转为文件格式发送")
        return _size_verdict(normalized_type)

    @staticmethod
    def _looks_like_url(media_source: str) -> bool:
        return urlparse(str(media_source or "")).scheme in {"http", "https"}

    async def _load_outbound_media(self, media_source: str, file_name: Optional[str] = None) -> Tuple[bytes, str, str]:
        source = str(media_source or "").strip()
        if not source:
            raise ValueError("media source is required")
        if re.fullmatch(r"<[^>\n]+>", source):
            raise ValueError(f"Media placeholder was not replaced with a real file path: {source}")

        parsed = urlparse(source)
        if parsed.scheme in {"http", "https"}:
            data, headers = await self._download_remote_bytes(source, max_bytes=ABSOLUTE_MAX_BYTES)
            content_disposition = headers.get("content-disposition")
            resolved_name = file_name or self._guess_filename(source, content_disposition, headers.get("content-type", ""))
            content_type = self._normalize_content_type(headers.get("content-type", ""), resolved_name)
            return data, content_type, resolved_name

        local_path = Path(unquote(parsed.path) if parsed.scheme == "file" else source).expanduser()
        if not local_path.is_absolute():
            local_path = (Path.cwd() / local_path).resolve()
        if not local_path.exists() or not local_path.is_file():
            raise FileNotFoundError(f"Media file not found: {local_path}")
        data = local_path.read_bytes()
        resolved_name = file_name or local_path.name
        return data, self._normalize_content_type("", resolved_name), resolved_name

    async def _prepare_outbound_media(self, media_source: str, file_name: Optional[str] = None) -> Dict[str, Any]:
        data, content_type, resolved_name = await self._load_outbound_media(media_source, file_name=file_name)
        detected_type = self._detect_wecom_media_type(content_type)
        size_check = self._apply_file_size_limits(len(data), detected_type, content_type)
        return {"data": data, "content_type": content_type, "file_name": resolved_name, "detected_type": detected_type, **size_check}

    # ── Outbound upload + send ───────────────────────────────────────────

    async def _upload_media_bytes(self, data: bytes, media_type: str, filename: str) -> Dict[str, Any]:
        if not data:
            raise ValueError("Cannot upload empty media")
        total_size = len(data)
        total_chunks = (total_size + UPLOAD_CHUNK_SIZE - 1) // UPLOAD_CHUNK_SIZE
        if total_chunks > MAX_UPLOAD_CHUNKS:
            raise ValueError(f"File too large: {total_chunks} chunks exceeds maximum of {MAX_UPLOAD_CHUNKS} chunks")

        init_response = await self._send_request(APP_CMD_UPLOAD_MEDIA_INIT, {
            "type": media_type, "filename": filename, "total_size": total_size, "total_chunks": total_chunks,
            "md5": hashlib.md5(data).hexdigest(),
        })
        self._raise_for_wecom_error(init_response, "media upload init")
        init_body = init_response.get("body") if isinstance(init_response.get("body"), dict) else {}
        upload_id = str(init_body.get("upload_id") or "").strip()
        if not upload_id:
            raise RuntimeError(f"media upload init failed: missing upload_id in response {init_response}")

        for chunk_index, start in enumerate(range(0, total_size, UPLOAD_CHUNK_SIZE)):
            chunk_response = await self._send_request(APP_CMD_UPLOAD_MEDIA_CHUNK, {
                "upload_id": upload_id,
                "chunk_index": chunk_index,  # official SDK uses 0-based chunk indexes
                "base64_data": base64.b64encode(data[start : start + UPLOAD_CHUNK_SIZE]).decode("ascii"),
            })
            self._raise_for_wecom_error(chunk_response, f"media upload chunk {chunk_index}")

        finish_response = await self._send_request(APP_CMD_UPLOAD_MEDIA_FINISH, {"upload_id": upload_id})
        self._raise_for_wecom_error(finish_response, "media upload finish")
        finish_body = finish_response.get("body") if isinstance(finish_response.get("body"), dict) else {}
        media_id = str(finish_body.get("media_id") or "").strip()
        if not media_id:
            raise RuntimeError(f"media upload finish failed: missing media_id in response {finish_response}")
        return {"type": str(finish_body.get("type") or media_type), "media_id": media_id, "created_at": finish_body.get("created_at")}

    async def _send_media_message(self, chat_id: str, media_type: str, media_id: str) -> Dict[str, Any]:
        response = await self._send_request(
            APP_CMD_SEND, {"chatid": chat_id, "msgtype": media_type, media_type: {"media_id": media_id}},
        )
        self._raise_for_wecom_error(response, "send media message")
        return response

    async def _send_reply_media_message(self, reply_req_id: str, media_type: str, media_id: str) -> Dict[str, Any]:
        response = await self._send_reply_request(reply_req_id, {"msgtype": media_type, media_type: {"media_id": media_id}})
        self._raise_for_wecom_error(response, "send reply media message")
        return response

    async def _send_followup_markdown(self, chat_id: str, content: str, reply_to: Optional[str] = None) -> Optional[SendResult]:
        if not content:
            return None
        result = await self.send(chat_id=chat_id, content=content, reply_to=reply_to)
        if not result.success:
            logger.warning("[%s] Follow-up markdown send failed: %s", self.name, result.error)
        return result

    async def _send_media_source(
        self, chat_id: str, media_source: str, caption: Optional[str] = None, file_name: Optional[str] = None,
        reply_to: Optional[str] = None,
    ) -> SendResult:
        if not chat_id:
            return SendResult(success=False, error="chat_id is required")
        try:
            prepared = await self._prepare_outbound_media(media_source, file_name=file_name)
        except FileNotFoundError as exc:
            return SendResult(success=False, error=str(exc))
        except Exception as exc:
            logger.error("[%s] Failed to prepare outbound media %s: %s", self.name, media_source, exc)
            return SendResult(success=False, error=str(exc))

        if prepared["rejected"]:
            await self._send_followup_markdown(chat_id, f"⚠️ {prepared['reject_reason']}", reply_to=reply_to)
            return SendResult(success=False, error=prepared["reject_reason"])

        reply_req_id = self._reply_req_id_for_message(reply_to)
        if not reply_req_id and chat_id in self._last_chat_req_ids:
            reply_req_id = self._last_chat_req_ids[chat_id]
        # Media MUST use the proactive path when a stream was/is active for the
        # chat: passive replyMedia cannot overwrite a replyStream thinking bubble
        # and the stream "owns" the req_id (server ignores or never acks).
        if self._find_active_turn_for_chat(chat_id) or chat_id in self._stream_expired_chats:
            reply_req_id = None

        try:
            upload_result = await self._upload_media_bytes(prepared["data"], prepared["final_type"], prepared["file_name"])
            logger.info("[%s] upload_media_bytes OK: media_id=%s type=%s", self.name, upload_result.get("media_id"), prepared["final_type"])
            if reply_req_id:
                media_response = await self._send_reply_media_message(reply_req_id, prepared["final_type"], upload_result["media_id"])
                logger.info("[%s] send_reply_media OK: %s", self.name, media_response)
            else:
                media_response = await self._send_media_message(chat_id, prepared["final_type"], upload_result["media_id"])
                logger.info("[%s] send_media_message OK: %s", self.name, media_response)
        except asyncio.TimeoutError:
            logger.error("[%s] TIMEOUT in _send_media_source for %s", self.name, media_source)
            return SendResult(success=False, error="Timeout sending media to WeCom")
        except Exception as exc:
            logger.error("[%s] Failed to send media %s: %s", self.name, media_source, exc)
            return SendResult(success=False, error=str(exc))

        caption_result = downgrade_result = None
        if caption:
            caption_result = await self._send_followup_markdown(chat_id, caption, reply_to=reply_to)
        if prepared["downgraded"] and prepared["downgrade_note"]:
            downgrade_result = await self._send_followup_markdown(chat_id, f"ℹ️ {prepared['downgrade_note']}", reply_to=reply_to)

        return SendResult(
            success=True,
            message_id=self._payload_req_id(media_response) or uuid.uuid4().hex[:12],
            raw_response={
                "upload": upload_result,
                "media": media_response,
                "caption": caption_result.raw_response if caption_result else None,
                "caption_error": caption_result.error if caption_result and not caption_result.success else None,
                "downgrade": downgrade_result.raw_response if downgrade_result else None,
                "downgrade_error": downgrade_result.error if downgrade_result and not downgrade_result.success else None,
            },
        )

    async def send_image(
        self, chat_id: str, image_url: str, caption: Optional[str] = None, reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        del metadata
        result = await self._send_media_source(chat_id=chat_id, media_source=image_url, caption=caption, reply_to=reply_to)
        if result.success or not self._looks_like_url(image_url):
            return result
        logger.warning("[%s] Falling back to text send for image URL %s: %s", self.name, image_url, result.error)
        fallback_text = f"{caption}\n{image_url}" if caption else image_url
        return await self.send(chat_id=chat_id, content=fallback_text, reply_to=reply_to)

    async def send_image_file(self, chat_id: str, image_path: str, caption: Optional[str] = None, reply_to: Optional[str] = None, **kwargs) -> SendResult:
        del kwargs
        return await self._send_media_source(chat_id=chat_id, media_source=image_path, caption=caption, reply_to=reply_to)

    async def send_document(
        self, chat_id: str, file_path: str, caption: Optional[str] = None, file_name: Optional[str] = None,
        reply_to: Optional[str] = None, **kwargs,
    ) -> SendResult:
        del kwargs
        logger.info("[%s] send_document called: chat=%s file=%s", self.name, chat_id, file_path)
        return await self._send_media_source(
            chat_id=chat_id, media_source=file_path, caption=caption, file_name=file_name, reply_to=reply_to,
        )

    async def send_voice(self, chat_id: str, audio_path: str, caption: Optional[str] = None, reply_to: Optional[str] = None, **kwargs) -> SendResult:
        del kwargs
        return await self._send_media_source(chat_id=chat_id, media_source=audio_path, caption=caption, reply_to=reply_to)

    async def send_video(self, chat_id: str, video_path: str, caption: Optional[str] = None, reply_to: Optional[str] = None, **kwargs) -> SendResult:
        del kwargs
        return await self._send_media_source(chat_id=chat_id, media_source=video_path, caption=caption, reply_to=reply_to)
