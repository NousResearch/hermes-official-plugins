"""One ElevenLabs request: settings, key, deadline, byte cap and readable errors.

Every tool goes through ``call()``: it reads the profile's key from its secret scope, sends one request
to ``{api_base_url}{path}`` with the ``xi-api-key`` header, streams the answer under a byte cap and an
overall deadline (run on a daemon worker: closing a client does not interrupt a blocked socket read on
every OS), and turns ElevenLabs' error bodies into a sentence the model can act on. Exception text from
httpx can echo request headers, so only the exception class is ever reported.
"""

from __future__ import annotations

import ipaddress
import json
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from agent.secret_scope import get_secret

API_KEY_ENV = "ELEVENLABS_API_KEY"
PLUGIN_ID = "elevenlabs-music"
SETTINGS_KEY = f"plugins.entries.{PLUGIN_ID}.settings"
MODELS = ("music_v1", "music_v2", "music_v2_5")
# Models that take a chunk composition plan (and so section edits, extensions and audio references).
PLAN_MODELS = ("music_v2", "music_v2_5")
# output_format choices (plugin.yaml) → file extension. The Music API offers no WAV container (only raw
# PCM), so WAV comes from the Desktop download menu's ffmpeg conversion instead.
FORMATS = {"mp3_44100_128": "mp3", "mp3_44100_192": "mp3", "mp3_48000_192": "mp3", "mp3_48000_320": "mp3",
           "opus_48000_128": "opus", "opus_48000_192": "opus"}
# Formats the stem-separation and video-to-music endpoints accept (their enum has no 48 kHz MP3).
LEGACY_FORMATS = {"mp3_44100_128", "mp3_44100_192", "opus_48000_128", "opus_48000_192"}
DEFAULTS = {
    "music_model": "music_v2_5",
    "output_format": "mp3_44100_128",
    "api_base_url": "https://api.elevenlabs.io",
    "store_for_editing": True,
    "chat_app_wait_seconds": 20,
}
CONNECT_READ_TIMEOUT = 120.0
MAX_RESPONSE_BYTES = 200 * 1024 * 1024
NO_RETRY = "Retrying right away will not help: tell the user and let them decide when to try again."
_KEYS_URL = "https://elevenlabs.io/app/settings/api-keys"


class ResponseRefused(Exception):
    """The endpoint answered, but with a body over the size cap or past the deadline."""


class ToolFailure(Exception):
    """A failure the model should read as the tool's error text."""


@dataclass(frozen=True)
class Settings:
    model: str
    output_format: str
    base_url: str
    store_for_editing: bool
    chat_app_wait_seconds: float


def _is_loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _flag_setting(value: Any, key: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in {"true", "false"}:
        return value.strip().lower() == "true"
    raise ValueError(f"{SETTINGS_KEY}.{key} must be true or false (got {value!r})")


def read_settings(setting: Callable[[str], Any]) -> Settings:
    """Every setting, checked against its declared choices: a hand-edited config.yaml must not make
    ElevenLabs bill for audio that is then saved under the wrong extension or refused."""
    model = setting("music_model") or DEFAULTS["music_model"]
    output_format = setting("output_format") or DEFAULTS["output_format"]
    base_url = str(setting("api_base_url") or DEFAULTS["api_base_url"]).strip().rstrip("/")
    if model not in MODELS:
        raise ValueError(f"{SETTINGS_KEY}.music_model is {model!r}; use one of {', '.join(MODELS)}")
    if output_format not in FORMATS:
        raise ValueError(f"{SETTINGS_KEY}.output_format is {output_format!r}; use one of {', '.join(FORMATS)}")
    store = setting("store_for_editing")
    store = DEFAULTS["store_for_editing"] if store is None else _flag_setting(store, "store_for_editing")
    wait = setting("chat_app_wait_seconds")
    wait = DEFAULTS["chat_app_wait_seconds"] if wait in (None, "") else wait
    if isinstance(wait, bool) or not isinstance(wait, (int, float, str)):
        raise ValueError(f"{SETTINGS_KEY}.chat_app_wait_seconds must be a number from 0 to 240")
    try:
        wait = float(wait)
    except ValueError:
        raise ValueError(f"{SETTINGS_KEY}.chat_app_wait_seconds must be a number from 0 to 240") from None
    if not 0 <= wait <= 240:
        raise ValueError(f"{SETTINGS_KEY}.chat_app_wait_seconds must be a number from 0 to 240")
    parts = urlsplit(base_url)
    try:
        _ = parts.port  # raises on a non-numeric or out-of-range port
    except ValueError:
        parts = None
    if parts is None or parts.scheme not in ("https", "http") or not parts.hostname:
        raise ValueError(f"{SETTINGS_KEY}.api_base_url must be an https URL")
    if parts.scheme == "http" and not _is_loopback(parts.hostname):
        # Plain http would send the xi-api-key header in cleartext; only a local stand-in may use it.
        raise ValueError(f"{SETTINGS_KEY}.api_base_url must use https (http is allowed only for localhost)")
    return Settings(model, output_format, base_url, store, wait)


def display_url(url: str) -> str:
    """``url`` without userinfo, for error text."""
    parts = urlsplit(url)
    host = f"[{parts.hostname}]" if ":" in (parts.hostname or "") else parts.hostname or ""
    return urlunsplit(parts._replace(netloc=f"{host}:{parts.port}" if parts.port else host))


def api_key() -> str:
    """The profile's key, or ``ToolFailure`` saying how to set it."""
    key = (get_secret(API_KEY_ENV, "") or "").strip()
    if not key:
        from hermes_constants import display_hermes_home

        raise ToolFailure(f"{API_KEY_ENV} is not set for this profile. Add your ElevenLabs API key to "
                          f"{display_hermes_home()}/.env (`hermes [-p <profile>] config set {API_KEY_ENV} <key>`) "
                          f"or in Settings → Plugins.")
    if not (key.isascii() and key.isprintable()):
        raise ToolFailure(f"{API_KEY_ENV} contains characters an API key cannot have; re-enter the key.")
    return key


def check_available() -> bool:
    return bool((get_secret(API_KEY_ENV, "") or "").strip())


# By the ``detail.status`` ElevenLabs sends; a 401 alone can be a bad key, a missing permission or no
# credits, and each needs a different fix.
_ERROR_HINTS = {
    "invalid_api_key": f" (ELEVENLABS_API_KEY is not a valid ElevenLabs key; create one at {_KEYS_URL})",
    "missing_permissions": f" (enable Music access for this API key at {_KEYS_URL})",
    "quota_exceeded": " (the ElevenLabs account is out of credits)",
}
_STATUS_HINTS = {401: " (check ELEVENLABS_API_KEY and your plan's remaining credits)",
                 402: " (the Music API needs a paid ElevenLabs plan)",
                 403: " (the Music API needs a paid ElevenLabs plan, or this feature needs a higher tier)",
                 429: " (rate limited or out of credits; try again later)"}


def api_error(status: int, body: bytes, reason: str) -> str:
    """ElevenLabs errors come as ``{"detail": {"status", "message"}}`` or FastAPI's
    ``{"detail": [{"loc", "msg"}]}``; fall back to the raw body."""
    try:
        payload = json.loads(body)
    except ValueError:
        payload = None
    detail = payload.get("detail") if isinstance(payload, dict) else None
    if isinstance(detail, dict):
        message = detail.get("message") or detail.get("status") or str(detail)
    elif isinstance(detail, list):
        message = "; ".join(
            f"{'.'.join(str(p) for p in item.get('loc', [])[1:]) or 'request'}: {item.get('msg')}"
            for item in detail if isinstance(item, dict)) or str(detail)
    elif isinstance(detail, str):
        message = detail
    else:
        message = body.decode("utf-8", "replace").strip()[:500] or reason
    vendor_status = str(detail.get("status") or "") if isinstance(detail, dict) else ""
    hint = _ERROR_HINTS.get(vendor_status) or _STATUS_HINTS.get(status, "")
    return f"ElevenLabs returned HTTP {status}: {message}{hint}"


@dataclass
class Response:
    status: int
    body: bytes
    reason: str
    headers: dict[str, str]

    def json(self) -> Any:
        return json.loads(self.body)


def _fetch(client, method: str, url: str, kwargs: dict, cap: int) -> Response:
    with client.stream(method, url, **kwargs) as response:
        declared = response.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > cap:
            raise ResponseRefused(f"response of {declared} bytes is over the {cap} byte cap")
        chunks, size = [], 0
        for chunk in response.iter_bytes():
            size += len(chunk)
            if size > cap:
                raise ResponseRefused(f"response is over the {cap} byte cap")
            chunks.append(chunk)
        return Response(response.status_code, b"".join(chunks), response.reason_phrase,
                        {k.lower(): v for k, v in response.headers.items()})


def call(settings: Settings, method: str, path: str, *, deadline: float, params: dict | None = None,
         json_body: Any = None, data: dict | None = None, files: list | None = None,
         cap: int = MAX_RESPONSE_BYTES) -> Response:
    """One request under ``deadline`` seconds. Raises ``ToolFailure`` for anything but an answer; a
    non-2xx answer is returned for the caller to map (``api_error``)."""
    import httpx

    key = api_key()
    url = f"{settings.base_url}{path}"
    kwargs: dict[str, Any] = {"headers": {"xi-api-key": key}}
    if params:
        kwargs["params"] = params
    if json_body is not None:
        kwargs["json"] = json_body
    if data is not None:
        kwargs["data"] = data
    if files is not None:
        kwargs["files"] = files
    client = httpx.Client(timeout=CONNECT_READ_TIMEOUT)
    outcome: dict = {}

    def work():
        try:
            outcome["value"] = _fetch(client, method, url, kwargs, cap)
        except Exception as exc:
            outcome["error"] = exc

    worker = threading.Thread(target=work, name="elevenlabs-music-request", daemon=True)
    worker.start()
    worker.join(deadline)
    where = display_url(settings.base_url)
    if worker.is_alive():
        client.close()
        raise ToolFailure(f"ElevenLabs ({where}): no complete response within {deadline:g} seconds. {NO_RETRY}")
    client.close()
    error = outcome.get("error")
    if isinstance(error, ResponseRefused):
        raise ToolFailure(f"ElevenLabs ({where}): {error}. {NO_RETRY}")
    if isinstance(error, httpx.TimeoutException):
        raise ToolFailure(f"ElevenLabs ({where}) did not answer within {CONNECT_READ_TIMEOUT:g} seconds "
                          f"({type(error).__name__}). {NO_RETRY}")
    if isinstance(error, (httpx.HTTPError, httpx.InvalidURL)):
        raise ToolFailure(f"Could not reach ElevenLabs ({where}): {type(error).__name__}. {NO_RETRY}")
    if error is not None:
        raise error
    return outcome["value"]


def ok(response: Response) -> Response:
    """``response`` when it is a 2xx with a body, else ``ToolFailure`` with ElevenLabs' message."""
    if not 200 <= response.status < 300:
        raise ToolFailure(api_error(response.status, response.body, response.reason))
    if not response.body:
        raise ToolFailure("ElevenLabs returned an empty response")
    return response


def split_multipart(response: Response) -> tuple[dict, bytes, str]:
    """``(metadata, audio bytes, audio content type)`` from ``/v1/music/detailed``'s multipart/mixed
    answer: a JSON part, then the audio part."""
    ctype = response.headers.get("content-type", "")
    boundary = next((p.split("=", 1)[1].strip().strip('"') for p in ctype.split(";")
                     if p.strip().lower().startswith("boundary=")), "")
    if not ctype.lower().startswith("multipart/") or not boundary:
        raise ToolFailure(f"ElevenLabs answered with {ctype or 'no content type'}, not the multipart reply "
                          f"this tool reads. {NO_RETRY}")
    metadata: dict = {}
    audio, audio_type = b"", ""
    for part in response.body.split(b"--" + boundary.encode("latin-1")):
        if part.startswith(b"--") or b"\r\n\r\n" not in part:
            continue
        head, _, content = part.lstrip(b"\r\n").partition(b"\r\n\r\n")
        content = content[:-2] if content.endswith(b"\r\n") else content
        headers = {}
        for line in head.decode("latin-1").split("\r\n"):
            name, _, value = line.partition(":")
            headers[name.strip().lower()] = value.strip()
        kind = headers.get("content-type", "").lower()
        if kind.startswith("application/json"):
            try:
                parsed = json.loads(content)
            except ValueError:
                parsed = None
            if isinstance(parsed, dict):
                metadata = parsed
        elif kind.startswith("audio/") or "attachment" in headers.get("content-disposition", ""):
            audio, audio_type = content, kind
    if not audio:
        raise ToolFailure(f"ElevenLabs' reply held no audio part. {NO_RETRY}")
    return metadata, audio, audio_type
