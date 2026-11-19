"""The ``elevenlabs_sound_effect`` tool: text → audio file through ElevenLabs' Sound Effects API.

``POST {api_base_url}/v1/sound-generation?output_format=<fmt>`` with ``{text, duration_seconds?,
prompt_influence?, loop, model_id}`` and the ``xi-api-key`` header returns the audio bytes
(https://elevenlabs.io/docs/api-reference/text-to-sound-effects/convert). The file lands in
``$HERMES_HOME/cache/generated/audio/``, which the gateway may deliver from, and the result carries a
``MEDIA:<path>`` line the model puts in its reply to send it. Every surface plays that line; Hermes
Desktop draws it as this plugin's sound card through its tool-media renderer seam (``src/plugin.tsx``).

Model, format and endpoint are user settings (``plugins.entries.<id>.settings``), read per call so a
multiplexed gateway serves each profile its own; the key comes from the profile's secret scope.
Everything that could waste a billed call (bad input, a setting outside the declared choices, a
malformed key) is refused before the request goes out.
"""

from __future__ import annotations

import datetime
import ipaddress
import json
import math
import os
import re
import threading
import uuid
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from agent.secret_scope import get_secret
from tools.registry import tool_error, tool_result

from . import audio_length as _audio_length
from . import provenance as _provenance

API_KEY_ENV = "ELEVENLABS_API_KEY"
SETTINGS_KEY = "plugins.entries.elevenlabs-sound-effects.settings"
MODELS = ("eleven_text_to_sound_v2", "eleven_text_to_sound_v3")
# output_format choices (plugin.yaml) → file extension; ElevenLabs' opus is Ogg Opus. Every
# extension is on the gateway's deliverable-media list.
FORMATS = {"mp3_44100_128": "mp3", "mp3_44100_192": "mp3", "opus_48000_128": "opus",
           "opus_48000_192": "opus", "wav_44100": "wav", "wav_48000": "wav"}
DEFAULTS = {
    "sound_model": MODELS[0],
    "output_format": "mp3_44100_128",
    "api_base_url": "https://api.elevenlabs.io",
}
MIN_SECONDS, MAX_SECONDS = 0.5, 30.0
# The API answers a longer description with HTTP 400 (same limit on v2 and v3).
MAX_PROMPT_CHARS = 450
_CONNECT_READ_TIMEOUT = 60.0
_DEADLINE_SECONDS = 180.0
# 30 s of 48 kHz WAV is ~6 MB; the cap only stops a misbehaving endpoint from filling memory.
MAX_AUDIO_BYTES = 50 * 1024 * 1024


# Live, a model retried an unanswered call 3-4 times (7-9 minutes of waiting) without this.
_NO_RETRY = "Retrying right away will not help: tell the user and let them decide when to try again."


class ResponseRefused(Exception):
    """The endpoint answered, but with a body over the size cap or past the deadline."""

SCHEMA = {
    "name": "elevenlabs_sound_effect",
    "description": (
        "Generate a sound effect with ElevenLabs (up to 30 s): foley, impacts, ambience, UI sounds, creature "
        "noises, stingers, drum loops. Describe it concretely (source, material, space, intensity), e.g. "
        "'heavy wooden door creaking open in a stone hall'; a length the user names goes in duration_seconds, "
        "never in the prompt. Put the result's media_tag line on its own line in your reply to deliver the "
        "audio (it plays on every surface). Billed to the user's ElevenLabs account."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "prompt": {"type": "string", "maxLength": MAX_PROMPT_CHARS, "description": "What the sound is."},
            "duration_seconds": {
                "type": "number", "minimum": MIN_SECONDS, "maximum": MAX_SECONDS,
                "description": "Length in seconds; omitted, ElevenLabs picks one at a higher flat price.",
            },
            "loop": {"type": "boolean", "description": "Seamless loop (ambience, music beds)."},
            "prompt_influence": {"type": "number", "minimum": 0, "maximum": 1,
                                 "description": "Higher follows the prompt more literally. Default 0.3."},
            "model": {"type": "string", "enum": list(MODELS), "description": "Omit for the user's setting."},
        },
        "required": ["prompt"],
    },
}


def check_available() -> bool:
    return bool((get_secret(API_KEY_ENV, "") or "").strip())


def _number(args: dict, key: str, low: float, high: float) -> float | None:
    raw = args.get(key)
    if raw is None or raw == "":
        return None
    try:
        if isinstance(raw, bool):
            raise TypeError
        value = float(raw)
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"{key} must be a number between {low:g} and {high:g}") from None
    if not (math.isfinite(value) and low <= value <= high):
        raise ValueError(f"{key} must be between {low:g} and {high:g} (got {raw!r})")
    return value


def _flag(raw: Any, key: str) -> bool:
    if raw is None or isinstance(raw, bool):
        return bool(raw)
    cleaned = raw.strip().lower() if isinstance(raw, str) else None
    if cleaned in {"true", "1", "yes", "on"}:
        return True
    if cleaned in {"false", "0", "no", "off", ""}:
        return False
    raise ValueError(f"{key} must be true or false (got {raw!r})")


# Live, models wrote "about 1 second" into the prompt instead of setting duration_seconds (4 of 14
# turns), paying the automatic-length rate (50 credits) and usually regenerating at the right length.
# It also matches timing inside a sound ("a beep every 2 seconds"); that costs one retry with a
# duration set, which such a call should carry anyway, so the message keeps that text in the prompt.
_LENGTH_IN_PROMPT = re.compile(r"\b\d+(?:\.\d+)?\s*-?\s*(?:secs?|seconds?)\b", re.IGNORECASE)


def build_request(args: dict, model: str) -> dict:
    """The JSON body for one call; raises ``ValueError`` with a model-readable message."""
    prompt = str(args.get("prompt") or "").strip()
    if not prompt:
        raise ValueError("prompt is required: describe the sound effect to generate")
    if len(prompt) > MAX_PROMPT_CHARS:
        raise ValueError(f"prompt is {len(prompt)} characters long; shorten it to at most {MAX_PROMPT_CHARS} characters")
    body: dict[str, Any] = {"text": prompt, "loop": _flag(args.get("loop"), "loop"), "model_id": model}
    duration = _number(args, "duration_seconds", MIN_SECONDS, MAX_SECONDS)
    if duration is not None:
        body["duration_seconds"] = duration
    elif named := _LENGTH_IN_PROMPT.search(prompt):
        raise ValueError(
            f"the prompt names a length ({named.group(0)!r}) but duration_seconds is not set: ElevenLabs "
            f"ignores lengths written in the prompt and bills an unset length at a higher flat rate. "
            f"Pass duration_seconds ({MIN_SECONDS:g}-{MAX_SECONDS:g}) for the clip's total length; drop that "
            f"length from the prompt, but keep timing that describes the sound itself (e.g. 'a beep every 2 "
            f"seconds').")
    influence = _number(args, "prompt_influence", 0.0, 1.0)
    if influence is not None:
        body["prompt_influence"] = influence
    return body


def read_settings(settings: Callable[[str], Any]) -> tuple[str, str, str]:
    """``(model, output_format, base_url)``. A value outside the declared choices (possible through a
    hand-edited config.yaml) is refused: ElevenLabs would bill for audio that is then rejected or
    saved under the wrong extension."""
    model = settings("sound_model") or DEFAULTS["sound_model"]
    output_format = settings("output_format") or DEFAULTS["output_format"]
    base_url = str(settings("api_base_url") or DEFAULTS["api_base_url"]).strip().rstrip("/")
    if model not in MODELS:
        raise ValueError(f"{SETTINGS_KEY}.sound_model is {model!r}; use one of {', '.join(MODELS)}")
    if output_format not in FORMATS:
        raise ValueError(f"{SETTINGS_KEY}.output_format is {output_format!r}; use one of {', '.join(FORMATS)}")
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
    return model, output_format, base_url


def _is_loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _display_url(url: str) -> str:
    """``url`` without userinfo, for error text."""
    parts = urlsplit(url)
    host = f"[{parts.hostname}]" if ":" in (parts.hostname or "") else parts.hostname or ""
    return urlunsplit(parts._replace(netloc=f"{host}:{parts.port}" if parts.port else host))


_KEYS_URL = "https://elevenlabs.io/app/settings/api-keys"
# By the ``detail.status`` ElevenLabs sends; a 401 alone can be a bad key, a missing permission or
# no credits, and each needs a different fix.
_ERROR_HINTS = {
    "invalid_api_key": f" (ELEVENLABS_API_KEY is not a valid ElevenLabs key; create one at {_KEYS_URL})",
    "missing_permissions": f" (enable Sound Effects access for this API key at {_KEYS_URL})",
    "quota_exceeded": " (the ElevenLabs account is out of credits)",
}
_STATUS_HINTS = {401: " (check ELEVENLABS_API_KEY and your plan's remaining credits)",
                 429: " (rate limited or out of credits; try again later)"}


def _api_error(status: int, body: bytes, reason: str) -> str:
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
    else:
        message = body.decode("utf-8", "replace").strip()[:500] or reason
    hint = _ERROR_HINTS.get(detail.get("status") if isinstance(detail, dict) else None) or _STATUS_HINTS.get(status, "")
    return f"ElevenLabs returned HTTP {status}: {message}{hint}"


def _fetch(client, base_url: str, output_format: str, body: dict, api_key: str):
    """One streamed request under the byte cap; returns ``(status, body bytes, reason, headers)``."""
    with client.stream(
            "POST", f"{base_url}/v1/sound-generation", params={"output_format": output_format}, json=body,
            headers={"xi-api-key": api_key, "accept": "audio/*"}) as response:
        declared = response.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > MAX_AUDIO_BYTES:
            raise ResponseRefused(f"response of {declared} bytes is over the {MAX_AUDIO_BYTES} byte cap")
        chunks, size = [], 0
        for chunk in response.iter_bytes():
            size += len(chunk)
            if size > MAX_AUDIO_BYTES:
                raise ResponseRefused(f"response is over the {MAX_AUDIO_BYTES} byte cap")
            chunks.append(chunk)
        return response.status_code, b"".join(chunks), response.reason_phrase, response.headers


def _download(base_url: str, output_format: str, body: dict, api_key: str):
    """``_fetch`` under an overall deadline that covers the wait for headers too. The request runs on
    a daemon worker and the caller waits at most ``_DEADLINE_SECONDS``: closing a client does not
    interrupt a blocked socket read on every OS, so the deadline cannot rely on it. A worker left
    behind ends at its own read timeout. Raises ``ResponseRefused`` for an oversize or overdue
    response, ``httpx.HTTPError`` otherwise."""
    import httpx

    client = httpx.Client(timeout=_CONNECT_READ_TIMEOUT)
    outcome: dict = {}

    def work():
        try:
            outcome["value"] = _fetch(client, base_url, output_format, body, api_key)
        except Exception as exc:  # noqa: BLE001 -- re-raised in the caller below
            outcome["error"] = exc

    worker = threading.Thread(target=work, name="elevenlabs-sfx-download", daemon=True)
    worker.start()
    worker.join(_DEADLINE_SECONDS)
    if worker.is_alive():
        client.close()
        raise ResponseRefused(f"no complete response within {_DEADLINE_SECONDS:g} seconds")
    client.close()
    if "error" in outcome:
        raise outcome["error"]
    return outcome["value"]


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "sound"


def _is_audio(audio: bytes, output_format: str) -> bool:
    """Recognize the container requested from ElevenLabs without trusting its MIME header."""
    kind = FORMATS[output_format]
    if kind == "wav":
        return len(audio) >= 12 and audio[:4] == b"RIFF" and audio[8:12] == b"WAVE"
    if kind == "opus":
        return audio.startswith(b"OggS") and b"OpusHead" in audio[:512]
    return audio.startswith(b"ID3") or (
        len(audio) >= 2 and audio[0] == 0xff and audio[1] & 0xe0 == 0xe0)


def _save(audio: bytes, prompt: str, output_format: str, sound_id: str) -> str:
    """Write under ``cache/generated/audio``: outside the transient caches the gateway sweeps, and
    inside the roots it delivers ``MEDIA:`` files from. Resolved per call (multiplexed profiles)."""
    from hermes_constants import get_hermes_home

    directory = get_hermes_home() / "cache" / "generated" / "audio"
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    path = directory / f"sfx_{_slug(prompt)}_{stamp}_{sound_id}.{FORMATS[output_format]}"
    path.write_bytes(audio)
    return str(path)


def generate(args: dict, settings: Callable[[str], Any], session_id: str = "") -> str:
    import httpx

    api_key = (get_secret(API_KEY_ENV, "") or "").strip()
    if not api_key:
        from hermes_constants import display_hermes_home

        return tool_error(f"{API_KEY_ENV} is not set for this profile. Add your ElevenLabs API key to "
                          f"{display_hermes_home()}/.env (`hermes [-p <profile>] config set {API_KEY_ENV} <key>`) "
                          f"or in Settings → Plugins.")
    if not (api_key.isascii() and api_key.isprintable()):
        return tool_error(f"{API_KEY_ENV} contains characters an API key cannot have; re-enter the key.")
    try:
        model, output_format, base_url = read_settings(settings)
        if args.get("model") not in (None, ""):
            if args["model"] not in MODELS:
                raise ValueError(f"model must be one of {', '.join(MODELS)} (got {args['model']!r})")
            model = args["model"]
        body = build_request(args, model)
    except ValueError as exc:
        return tool_error(str(exc))
    where = _display_url(base_url)
    try:
        status, audio, reason, headers = _download(base_url, output_format, body, api_key)
    except ResponseRefused as exc:
        return tool_error(f"ElevenLabs ({where}): {exc}. {_NO_RETRY}")
    except httpx.TimeoutException as exc:
        return tool_error(f"ElevenLabs ({where}) did not answer within {_CONNECT_READ_TIMEOUT:g} seconds "
                          f"({type(exc).__name__}). {_NO_RETRY}")
    except (httpx.HTTPError, httpx.InvalidURL) as exc:  # exception text can echo request headers: name the class only
        return tool_error(f"Could not reach ElevenLabs ({where}): {type(exc).__name__}. {_NO_RETRY}")
    if status != 200:
        return tool_error(_api_error(status, audio, reason))
    if not audio:
        return tool_error("ElevenLabs returned an empty audio response")
    if not _is_audio(audio, output_format):
        content_type = headers.get("content-type", "missing Content-Type").split(";", 1)[0]
        return tool_error(
            f"ElevenLabs returned HTTP 200 with {content_type}, but the response is not recognizable "
            f"{FORMATS[output_format]} audio")
    sound_id = uuid.uuid4().hex[:12]
    path = _save(audio, body["text"], output_format, sound_id)
    requested = body.get("duration_seconds")
    length = _audio_length.audio_seconds(audio, FORMATS[output_format])
    result = {
        "success": True, "file_path": path, "media_tag": f"MEDIA:{path}", "sound_id": sound_id,
        "model": model, "output_format": output_format, "loop": body["loop"],
        "length_seconds": length, "requested_duration_seconds": requested, "bytes": len(audio),
    }
    if length is not None and requested is not None and abs(length - requested) >= 0.05:
        result["length_note"] = (f"ElevenLabs returned {length:g} s for the {requested:g} s requested; the file "
                                 f"is {length:g} s long (billing follows the request).")
    if cost := headers.get("character-cost"):
        result["billed_characters"] = cost
    _provenance.record({
        "id": sound_id, "prompt": body["text"], "duration_seconds": requested, "length_seconds": length,
        "loop": body["loop"], "prompt_influence": body.get("prompt_influence"), "model": model,
        "output_format": output_format, "file_name": os.path.basename(path), "bytes": len(audio),
        "billed_characters": result.get("billed_characters"), "session_id": session_id or None,
    })
    return tool_result(result)
