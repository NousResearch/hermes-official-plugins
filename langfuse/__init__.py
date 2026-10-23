"""langfuse — Hermes plugin tracing conversations, LLM calls and tool usage to Langfuse.

Official catalog plugin (formerly bundled with Hermes as ``observability/langfuse``).
Activated via ``plugins.enabled``; hooks are inert without the ``langfuse`` SDK
and credentials. Env: HERMES_LANGFUSE_PUBLIC_KEY / SECRET_KEY (required),
BASE_URL, ENV, RELEASE, SAMPLE_RATE, MAX_CHARS (12000), MAX_DEPTH (4), DEBUG, and CAPTURE =
metadata (sizes/ids/usage only) | sanitized (default: secret redaction +
truncation) | full (truncated raw content). See README.md.
"""
from __future__ import annotations

import atexit
import contextlib
import functools
import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

try:
    from langfuse import Langfuse, propagate_attributes
except Exception:  # pragma: no cover - fail-open when optional dep is missing
    Langfuse = None
    propagate_attributes = None


@dataclass
class TraceState:
    trace_id: str
    root_span: Any
    generations: dict[str, Any] = field(default_factory=dict)
    tools: dict[str, Any] = field(default_factory=dict)
    pending_tools_by_name: dict[str, list] = field(default_factory=dict)
    turn_tool_calls: list[dict[str, Any]] = field(default_factory=list)
    # Keyed by child_session_id: subagent_stop carries no child_subagent_id.
    subagents: dict[str, Any] = field(default_factory=dict)
    # Fingerprints of MoA fan-outs already recorded: the client holds its last
    # fan-out until the next one, so tool-loop turns would re-emit advisors.
    moa_emitted: set = field(default_factory=set)
    last_updated_at: float = field(default_factory=time.time)
    # propagate_attributes() kwargs the root was opened with, re-applied to every child.
    propagated: dict[str, Any] = field(default_factory=dict)


_STATE_LOCK = threading.Lock()
_TRACE_STATE: dict[str, TraceState] = {}
# Ceiling on live trace state (per turn_id): turns that never reach _finish_trace
# would leak forever, so over the cap the least-recently-updated are evicted.
# Bounds the leak, not concurrency.
_MAX_TRACE_STATE = 256
_LANGFUSE_CLIENT = None
# Under a multiplexed profile override, one settled client (or _INIT_FAILED) per Hermes home: the
# keys live in each profile's .env, so a single slot would trace profile B into profile A's project
# (or pin B to A's failed init). The slot above stays for the unscoped single-profile path.
_LANGFUSE_CLIENT_BY_HOME: dict[str, Any] = {}
# Separate from _STATE_LOCK (hot path) so the two never nest; serializes the
# first client build so racing callers can't each construct a client.
_LANGFUSE_CLIENT_LOCK = threading.Lock()
# The registering PluginContext: the public seam for the served profile and the running cron job.
_PLUGIN_CTX: Any = None
_READ_FILE_LINE_RE = re.compile(r"^\s*(\d+)\|(.*)$")
_READ_FILE_HEAD_LINES = 25
_READ_FILE_TAIL_LINES = 15
_READ_FILE_META_KEYS = ("total_lines", "file_size", "truncated", "is_binary", "is_image", "hint",
                        "_warning", "mime_type", "dimensions", "similar_files", "error")

# Langfuse-issued keys always carry these prefixes. Anything else is a leftover
# template value: the SDK accepts it at construction time but silently drops
# every trace at flush time (#23823).
_LANGFUSE_KEY_PREFIXES: dict[str, str] = {
    "HERMES_LANGFUSE_PUBLIC_KEY": "pk-lf-",
    "HERMES_LANGFUSE_SECRET_KEY": "sk-lf-",
}
# Right-prefix templates are placeholders too: the docs print the keys as literally
# ``pk-lf-...`` and that paste passes the prefix check (hermes-agent#51399). Matched on the
# lowercased remainder after the prefix: whole-value for these, substring for the fragments.
# Issued remainders are UUID-shaped hex, and every fragment has a non-hex letter.
_PLACEHOLDER_REMAINDERS = frozenset({"", "...", "***"})
_PLACEHOLDER_FRAGMENTS = ("placeholder", "your-", "your_", "changeme", "change-me", "test-key",
                          "example", "unset", "dummy", "xxx")

# (langfuse usage key, CanonicalUsage attribute / summary-dict key, PricingEntry attribute)
_USAGE_FIELDS = (
    ("input", "input_tokens", "input_cost_per_million"),
    ("output", "output_tokens", "output_cost_per_million"),
    ("cache_read_input_tokens", "cache_read_tokens", "cache_read_cost_per_million"),
    ("cache_creation_input_tokens", "cache_write_tokens", "cache_write_cost_per_million"),
    ("reasoning_tokens", "reasoning_tokens", None),
)


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _secret(name: str) -> str:
    """Credential read through the profile secret scope. A scope-less multiplex caller raises
    (``UnscopedSecretError``): that is a spawn-site bug, and reading ``os.environ`` instead would
    ship this profile's traces with the DEFAULT profile's keys."""
    from agent.secret_scope import get_secret

    return (get_secret(name) or "").strip()


def _debug(message: str) -> None:
    if _env("HERMES_LANGFUSE_DEBUG").lower() in {"1", "true", "yes", "on"}:
        logger.info("Langfuse tracing: %s", message)


@contextlib.contextmanager
def _failsafe(label: str):
    """Swallow + debug-log any exception: telemetry must never block the agent turn."""
    try:
        yield
    except Exception as exc:  # pragma: no cover - fail-open
        _debug(f"{label} failed: {exc}")


_CAPTURE_MODES = ("metadata", "sanitized", "full")
_DEFAULT_CAPTURE_MODE = "sanitized"
_warned_invalid_capture = False


def _capture_mode() -> str:
    """Resolve ``metadata | sanitized | full``; read per call so long-lived processes
    can flip modes. Invalid values warn once and fall back to the default (never
    capture more than the operator intended)."""
    global _warned_invalid_capture
    value = _env("HERMES_LANGFUSE_CAPTURE").lower()
    if not value or value in _CAPTURE_MODES:
        return value or _DEFAULT_CAPTURE_MODE
    if not _warned_invalid_capture:
        _warned_invalid_capture = True
        logger.warning(
            "Langfuse plugin: invalid HERMES_LANGFUSE_CAPTURE=%r, falling back "
            "to %r (valid: %s)",
            value, _DEFAULT_CAPTURE_MODE, ", ".join(_CAPTURE_MODES),
        )
    return _DEFAULT_CAPTURE_MODE


def _redact_secrets(value: str) -> str:
    # Core's telemetry-egress scrub: force=True (ignores security.redact_secrets:
    # false, this content leaves the machine), sweeps opaque ``Bearer <token>``
    # residue, and fails CLOSED — a redactor error never ships the raw text.
    # redact_url_credentials: core keeps user:pass@ / ?api_key= so OAuth callbacks
    # survive tool flows; an upload to Langfuse has no such need (hermes-agent#88608).
    from agent.redact import redact_for_egress, redact_sensitive_text
    return redact_for_egress(redact_sensitive_text(value, force=True, redact_url_credentials=True))


# (types, shape builder) for _describe_content; first match wins (bool handled before).
_CONTENT_SHAPES = (
    ((int, float), lambda v: {"type": "number"}),
    (bytes, lambda v: {"type": "bytes", "length": len(v)}),
    (str, lambda v: {"type": "text", "chars": len(v)}),
    (dict, lambda v: {"type": "object", "keys": [str(k) for k in list(v.keys())[:20]]}),
    ((list, tuple, set), lambda v: {"type": "array", "items": len(v)}),
)


def _describe_content(value: Any) -> Any:
    """Metadata-mode stand-in for content: shape and size, never payload."""
    if value is None or isinstance(value, bool):
        return value
    shape = next((build(value) for types, build in _CONTENT_SHAPES if isinstance(value, types)), None)
    return {"omitted": True, **(shape or {"type": type(value).__name__})}


def _capture_content(value: Any, *, parse_json_strings: bool = False, tool_result_of: Optional[tuple] = None) -> Any:
    """Apply the active capture mode to a CONTENT value.

    Only prompt/response text, tool arguments and tool results are content;
    metadata fields (provider, model, IDs, counts) stay as-is in every mode.
    ``tool_result_of=(tool_name, args)`` marks a tool result: JSON strings are
    parsed first so a read_file payload can be collapsed to a preview keyed by
    the call's ``args``.
    """
    if _capture_mode() == "metadata":
        return _describe_content(value)
    if tool_result_of is not None:
        tool_name, args = tool_result_of
        value = _maybe_parse_json_string(value) if isinstance(value, str) else value
        value, parse_json_strings = _normalize_payload(value, tool_name=tool_name, args=args), True
    return _safe_value(value, parse_json_strings=parse_json_strings)


# Sentinel: "_get_langfuse() has tried and failed". Tests reset by reloading
# the module; runtime callers must restart the process after fixing credentials.
_INIT_FAILED = object()


def _validate_langfuse_key(env_name: str, value: str, *, require_prefix: bool = True) -> Optional[str]:
    """Log-ready error if ``value`` lacks the prefix for ``env_name`` (checked only when
    ``require_prefix``) or is a leftover template; the preview
    exposes placeholders without echoing a real secret pasted into the wrong var."""
    expected = _LANGFUSE_KEY_PREFIXES.get(env_name, "")
    if not expected:
        return None
    preview = "<empty>" if not value else repr(value) if len(value) <= 12 else repr(value[:6] + "...")
    if require_prefix and not value.startswith(expected):
        return f"{env_name}={preview} (expected {expected!r} prefix)"
    remainder = value.removeprefix(expected).lower()
    if remainder in _PLACEHOLDER_REMAINDERS or any(f in remainder for f in _PLACEHOLDER_FRAGMENTS):
        return f"{env_name}={preview} (looks like a template value, not an issued key)"
    return None


def _settled_client() -> Any:
    """The active profile's settled client slot value (client, ``_INIT_FAILED`` or ``None`` = never
    built). Never initializes."""
    from hermes_constants import get_hermes_home_override, hermes_home_key

    if get_hermes_home_override() is None:
        return _LANGFUSE_CLIENT
    return _LANGFUSE_CLIENT_BY_HOME.get(hermes_home_key())


def _settle_client() -> Any:
    """Build once and store for the active profile. Caller holds ``_LANGFUSE_CLIENT_LOCK``."""
    global _LANGFUSE_CLIENT
    from hermes_constants import get_hermes_home_override, hermes_home_key

    client = _build_client()
    settled = _INIT_FAILED if client is None else client
    if get_hermes_home_override() is None:
        _LANGFUSE_CLIENT = settled
    else:
        _LANGFUSE_CLIENT_BY_HOME[hermes_home_key()] = settled
    if client is not None:
        # atexit is LIFO: registering AFTER the SDK's constructor means our
        # finalizer runs first, so root spans ended there still get flushed
        # by the SDK (short-lived processes: kanban workers, chat -q, cron).
        atexit.register(_finalize_all_traces)
    return settled


def _get_langfuse() -> Optional[Langfuse]:
    """Cached Langfuse client, or ``None`` if the SDK/credentials are unavailable.
    The first build is serialized so racing callers can't each construct a client
    and leak the loser's HTTP connection + flush thread."""
    # Fast path — already settled (success or _INIT_FAILED) needs no lock;
    # re-check under it since a racing thread may have finished init.
    settled = _settled_client()
    if settled is None:
        with _LANGFUSE_CLIENT_LOCK:
            settled = _settled_client()
            if settled is None:
                settled = _settle_client()
    return None if settled is _INIT_FAILED else settled


def _build_client() -> Optional[Langfuse]:
    """Construct the SDK client from env, or None (with one warning) when it can't be."""
    if Langfuse is None:
        logger.warning(
            "Langfuse plugin is enabled but the langfuse SDK is unavailable; "
            "tracing is disabled. Reinstall it with `hermes plugins install langfuse --force`."
        )
        return None

    public_key, secret_key = (_secret(f"HERMES_LANGFUSE_{n}") or _secret(f"LANGFUSE_{n}") for n in ("PUBLIC_KEY", "SECRET_KEY"))
    if not (public_key and secret_key):
        # Enabled but half/un-configured used to return with zero log output, so a placeholder
        # public key with the secret unset dropped every trace silently (hermes-agent#98631).
        missing = [name for name, value in (("HERMES_LANGFUSE_PUBLIC_KEY", public_key),
                                            ("HERMES_LANGFUSE_SECRET_KEY", secret_key)) if not value]
        logger.warning(
            "Langfuse plugin: missing credentials (%s), traces will NOT be emitted. Set both "
            "HERMES_LANGFUSE_PUBLIC_KEY and HERMES_LANGFUSE_SECRET_KEY, or disable the plugin "
            "(`hermes plugins disable langfuse`) to silence this warning.",
            ", ".join(missing),
        )
        return None

    # The SDK does not validate keys at construction; placeholder keys
    # would fail silently at flush time (#23823). Warn once here instead.
    # Only Langfuse Cloud guarantees the pk-lf-/sk-lf- format: a custom endpoint may issue
    # operator-defined keys and authenticates them itself (hermes-agent#72484), so there a wrong
    # prefix only warns (self-hosted Langfuse issues pk-lf-/sk-lf- too) and templates still refuse.
    base_url = _secret("HERMES_LANGFUSE_BASE_URL") or _secret("LANGFUSE_BASE_URL") or "https://cloud.langfuse.com"
    host = (urlsplit(base_url).hostname or "").rstrip(".")
    cloud = host == "langfuse.com" or host.endswith(".langfuse.com")
    keys = (("HERMES_LANGFUSE_PUBLIC_KEY", public_key), ("HERMES_LANGFUSE_SECRET_KEY", secret_key))
    placeholder_issues = [issue for name, value in keys if (issue := _validate_langfuse_key(name, value, require_prefix=cloud))]
    if placeholder_issues:
        logger.warning(
            "Langfuse plugin: credentials look like placeholders, traces will "
            "NOT be emitted (%s). Set real Langfuse keys (pk-lf-... / sk-lf-...) "
            "or unset HERMES_LANGFUSE_PUBLIC_KEY / HERMES_LANGFUSE_SECRET_KEY to "
            "silence this warning.",
            "; ".join(placeholder_issues),
        )
        return None
    prefix_issues = [issue for name, value in keys if (issue := _validate_langfuse_key(name, value))]
    if prefix_issues:
        logger.warning(
            "Langfuse plugin: credentials do not look like Langfuse keys (%s). Fine for a "
            "Langfuse-compatible endpoint that issues its own keys; otherwise every trace is "
            "rejected at flush: check for template values or swapped public/secret keys.",
            "; ".join(prefix_issues),
        )

    kwargs: dict[str, Any] = {"public_key": public_key, "secret_key": secret_key}
    for key, name, default in (("base_url", "BASE_URL", "https://cloud.langfuse.com"), ("environment", "ENV", ""),
                               ("release", "RELEASE", "")):
        value = _secret(f"HERMES_LANGFUSE_{name}") or _secret(f"LANGFUSE_{name}") or default
        if value:
            kwargs[key] = value
    sample_rate = _secret("HERMES_LANGFUSE_SAMPLE_RATE")
    if sample_rate:
        try:
            kwargs["sample_rate"] = float(sample_rate)
        except ValueError:
            logger.warning("Invalid HERMES_LANGFUSE_SAMPLE_RATE=%r", sample_rate)

    # The SDK's OTel resource falls back to service.name=unknown_service, so every agent's traces
    # landed unattributed (hermes-agent#58322). Default only: an operator's OTEL_SERVICE_NAME or
    # service.name in OTEL_RESOURCE_ATTRIBUTES still wins. The SDK's TracerProvider reads it once at
    # construction, so it is set only for that call: children (terminal, MCP) must not inherit it.
    default_service = "OTEL_SERVICE_NAME" not in os.environ and "service.name=" not in os.environ.get("OTEL_RESOURCE_ATTRIBUTES", "")
    if default_service:
        os.environ["OTEL_SERVICE_NAME"] = "hermes-agent"
    try:
        return Langfuse(**kwargs)
    except Exception as exc:  # pragma: no cover - fail-open
        logger.warning("Could not initialize Langfuse client: %s", exc)
        return None
    finally:
        if default_service:
            os.environ.pop("OTEL_SERVICE_NAME", None)


def _trace_key(task_id: str, session_id: str, *, turn_id: str = "", api_request_id: str = "") -> str:
    """In-process trace scope key for one agent turn. ``turn_id`` wins over
    ``api_request_id`` so the turn-level post_llm_call hook (no api_request_id)
    resolves to the same key as request-level hooks; a bare ``task_id`` is the
    legacy shape from before turn/request scoping."""
    scope = f"task:{task_id}" if task_id else f"session:{session_id}" if session_id else f"thread:{threading.get_ident()}"
    if turn_id:
        return f"{scope}:turn:{turn_id}"
    if api_request_id:
        return f"{scope}:api:{api_request_id}"
    return task_id or scope


def _state_for_turn(turn_id: str) -> Optional[TraceState]:
    """Live trace state for a turn id alone (caller holds ``_STATE_LOCK``). Subagent
    hooks carry ``parent_turn_id`` but no ``task_id``, so rebuilding the key would
    miss; match on the unique ``:turn:<id>`` suffix instead."""
    if not turn_id:
        return None
    suffix = f":turn:{turn_id}"
    return next((state for key, state in _TRACE_STATE.items() if key.endswith(suffix)), None)


def _truncate_text(value: str, max_chars: int) -> Any:
    # The SDK decodes data:*;base64 strings as media; a truncated one is
    # invalid base64 and logs noisily, so redact the whole URI instead.
    prefix = value[:200].lower()
    if prefix.startswith("data:") and ";base64," in prefix:
        header = value.split(",", 1)[0] if "," in value else "data:"
        media_type = header[5:].split(";", 1)[0] if header.startswith("data:") else ""
        return {"type": "data_uri", "media_type": media_type or None, "omitted": True, "length": len(value)}
    # Redact BEFORE truncating so a secret straddling the cut cannot leak.
    if _capture_mode() == "sanitized":
        value = _redact_secrets(value)
    over = len(value) - max_chars
    return value if over <= 0 else value[:max_chars] + f"... [truncated {over} chars]"


def _maybe_parse_json_string(value: str) -> Any:
    stripped = value.strip()
    if len(stripped) < 2 or stripped[0] not in "{[":
        return value
    try:
        parsed, idx = json.JSONDecoder().raw_decode(stripped)
    except Exception:
        return value
    if not isinstance(parsed, (dict, list)):
        return value

    trailing = stripped[idx:].strip()
    if not trailing:
        return parsed

    hint_key = "_hint" if trailing.startswith("[Hint:") else "_trailing_text"
    if isinstance(parsed, dict):
        return {**parsed, (hint_key if hint_key not in parsed else "_trailing_text"): trailing}
    return {"data": parsed, hint_key: trailing}


def _normalize_payload(value: Any, *, tool_name: str = "", args: Any = None) -> Any:
    """Collapse a read_file result (line-numbered content + file metadata) into a compact preview."""
    is_read_file = (
        isinstance(value, dict)
        and isinstance(value.get("content"), str)
        and all(k in value for k in ("total_lines", "file_size", "is_binary", "is_image"))
        and not value.get("error")
    )
    if not is_read_file:
        return value
    normalized: dict[str, Any] = {}
    if tool_name == "read_file" and isinstance(args, dict):
        if isinstance(args.get("path"), str) and args["path"]:
            normalized["path"] = args["path"]
        normalized.update({key: args[key] for key in ("offset", "limit") if isinstance(args.get(key), int)})

    content = value.get("content", "")
    matches = [_READ_FILE_LINE_RE.match(raw) for raw in content.splitlines()] if isinstance(content, str) and content else []
    lines = [{"line": int(m.group(1)), "text": m.group(2)} for m in matches] if matches and all(matches) else []
    if lines:
        normalized["returned_lines"] = {"start": lines[0]["line"], "end": lines[-1]["line"], "count": len(lines)}
        head, tail = _READ_FILE_HEAD_LINES, _READ_FILE_TAIL_LINES
        normalized["content_preview"] = {"lines": lines} if len(lines) <= head + tail else {
            "head": lines[:head], "tail": lines[-tail:], "omitted_line_count": len(lines) - head - tail,
        }
    elif value.get("content"):
        normalized["content_preview"] = {"text": value.get("content", "")}

    normalized.update({key: value[key] for key in _READ_FILE_META_KEYS if key in value})

    b64 = value.get("base64_content")
    if isinstance(b64, str) and b64:
        normalized["base64_content"] = {"omitted": True, "length": len(b64)}
    return normalized


@functools.lru_cache(maxsize=8)
def _resolve_max_depth(configured_depth: str) -> int:
    """Parse ``HERMES_LANGFUSE_MAX_DEPTH``; an invalid value warns ONCE per distinct value.
    Cached on the raw string (not at import) so a long-lived process still picks up a changed
    env var, while a bad value no longer logs one warning per captured prompt/tool payload."""
    try:
        max_depth = int(configured_depth)
        if max_depth < 0:
            raise ValueError
        return max_depth
    except ValueError:
        logger.warning("Invalid HERMES_LANGFUSE_MAX_DEPTH=%r; use a non-negative integer. Falling back to 4.", configured_depth)
        return 4


def _safe_value(value: Any, *, max_chars: Optional[int] = None, depth: int = 0,
                parse_json_strings: bool = False, max_depth: Optional[int] = None) -> Any:
    max_chars = max_chars if max_chars is not None else int(_env("HERMES_LANGFUSE_MAX_CHARS", "12000") or "12000")
    if max_depth is None:
        max_depth = _resolve_max_depth(_env("HERMES_LANGFUSE_MAX_DEPTH", "4") or "4")
    if depth > max_depth:
        return "<max-depth>"
    if value is None or isinstance(value, (int, float, bool)):
        return value
    if isinstance(value, bytes):
        return {"type": "bytes", "len": len(value)}
    recurse = lambda v, d: _safe_value(v, max_chars=max_chars, depth=d, parse_json_strings=parse_json_strings, max_depth=max_depth)
    if isinstance(value, str):
        parsed = _maybe_parse_json_string(value) if parse_json_strings else value
        return recurse(parsed, depth) if parsed is not value else _truncate_text(value, max_chars)
    if isinstance(value, dict):
        normalized = _normalize_payload(value)
        if normalized is not value:
            return recurse(normalized, depth)
        # Parsed JSON loses the key context the text redactor keys on: as text,
        # '"access_token": "<opaque>"' is masked, the bare value is not.
        from agent.redact import is_secret_field_name
        mask = _capture_mode() == "sanitized"
        # Any scalar but bool/None: {"password": 123456} parses to an int.
        return {str(k): "[REDACTED]" if mask and isinstance(v, (str, int, float)) and not isinstance(v, bool)
                and v != "" and is_secret_field_name(k) else recurse(v, depth + 1)
                for k, v in list(value.items())[:50]}
    if isinstance(value, (list, tuple, set)):
        return [recurse(v, depth + 1) for v in list(value)[:50]]
    if hasattr(value, "__dict__"):
        return recurse(vars(value), depth + 1)
    return _truncate_text(repr(value), max_chars)


def _coerce_request_messages(*, request_messages: Any = None, messages: Any = None,
                             conversation_history: Any = None, user_message: Any = None) -> list[dict[str, Any]]:
    for candidate in (request_messages, messages, conversation_history):
        if isinstance(candidate, list):
            return candidate
    return [] if user_message is None else [{"role": "user", "content": user_message}]


def _serialize_system_prompt(system_prompt: Any) -> Optional[dict[str, Any]]:
    """Normalize Anthropic/Bedrock ``system`` param or OpenAI-style system content."""
    if isinstance(system_prompt, str):
        text = system_prompt.strip()
    elif isinstance(system_prompt, list):
        # Anthropic: {"type": "text", "text": ...}; Bedrock Converse: {"text": ...}; or bare strings.
        blocks = ((b.get("text", "") if b.get("type") in ("text", None) and "text" in b else None)
                  if isinstance(b, dict) else b for b in system_prompt)
        text = "\n\n".join(b for b in blocks if isinstance(b, str) and b)
    else:
        return None
    return {"role": "system", "content": _capture_content(text)} if text else None


def _messages_for_langfuse_input(*, request_messages: Any = None, messages: Any = None,
                                 conversation_history: Any = None, user_message: Any = None,
                                 system_prompt: Any = None) -> list[dict[str, Any]]:
    """Generation input, prepending ``system_prompt`` when the provider split it out of messages."""
    raw = _coerce_request_messages(request_messages=request_messages, messages=messages,
                                   conversation_history=conversation_history, user_message=user_message)
    system_msg = None if raw and raw[0].get("role") == "system" else _serialize_system_prompt(system_prompt)
    serialized = _serialize_messages(raw)
    return serialized if system_msg is None else [system_msg, *serialized]


def _serialize_message(message: dict[str, Any]) -> dict[str, Any]:
    # Responses API input items carry type/call_id/name/arguments/output (reasoning: summary), no
    # role/content: the chat shape below would export them as {"role": null, "content": null}.
    # encrypted_content (reasoning/compaction) is issuer-sealed ciphertext, so it is not exported.
    if message.get("type") in ("function_call", "function_call_output", "reasoning", "compaction"):
        return {k: _capture_content(v, parse_json_strings=True) if k in ("arguments", "output", "summary") else v
                for k, v in message.items() if k in ("type", "call_id", "name", "arguments", "output", "summary")}
    role, is_tool = message.get("role"), message.get("role") == "tool"
    return {
        "role": role, "content": _capture_content(message.get("content"), parse_json_strings=is_tool),
        **({"tool_call_id": message["tool_call_id"]} if is_tool and message.get("tool_call_id") else {}),
        **({"name": _safe_value(message["name"])} if is_tool and message.get("name") else {}),
        **({"tool_calls": _capture_content(message["tool_calls"], parse_json_strings=True)} if message.get("tool_calls") else {}),
    }


def _serialize_messages(messages: Any) -> list[dict[str, Any]]:
    return [_serialize_message(m) for m in messages[-12:] if isinstance(m, dict)] if isinstance(messages, list) else []


def _serialize_tool_call(tool_call: Any) -> dict[str, Any]:
    fn = getattr(tool_call, "function", None)
    name, safe_arguments = getattr(fn, "name", None), _capture_content(getattr(fn, "arguments", None))
    return {"id": getattr(tool_call, "id", None), "type": getattr(tool_call, "type", None) or "function",
            "name": name, "arguments": safe_arguments, "function": {"name": name, "arguments": safe_arguments}}


def _serialize_assistant_message(message: Any) -> dict[str, Any]:
    reasoning = next((getattr(message, attr, None) for attr in ("reasoning", "reasoning_content", "reasoning_details")
                      if getattr(message, attr, None) is not None), None)
    return {
        # Normalized provider responses may omit role; this hook always represents the assistant.
        "role": "assistant",
        "content": _capture_content(getattr(message, "content", None)),
        "reasoning": None if reasoning is None else _capture_content(reasoning),
        "tool_calls": [_serialize_tool_call(tc) for tc in getattr(message, "tool_calls", None) or ()],
    }


def _canonical_usage_and_cost(canonical: Any, *, provider: str, model: str,
                              base_url: str) -> tuple[dict[str, int], dict[str, float]]:
    """Translate canonical Hermes usage into Langfuse usage and cost maps."""
    usage_details: dict[str, int] = {
        key: tokens for key, attr, _ in _USAGE_FIELDS
        if (tokens := getattr(canonical, attr)) or key in ("input", "output")
    }
    cost_details: dict[str, float] = {}
    try:
        from agent.usage_pricing import estimate_usage_cost, resolve_billing_route

        # Subscription-included routes: Langfuse treats explicit cost_details
        # (even zeros) as authoritative, so omit them and let it estimate.
        route = resolve_billing_route(model, provider=provider, base_url=base_url)
        if getattr(route, "billing_mode", "") == "subscription_included":
            return usage_details, cost_details
        cost = estimate_usage_cost(model, canonical, provider=provider, base_url=base_url, api_key="")
    except Exception as exc:  # pragma: no cover - fail-open
        _debug(f"usage pricing failed: {exc}")
        return usage_details, cost_details

    # No total (e.g. cache pricing unknown) => export no costs at all, so a
    # partial component subtotal is never mistaken for the request total.
    if cost.amount_usd is None:
        return usage_details, cost_details

    # Langfuse only derives totals from input/output keys, so cache/custom keys
    # need an explicit total (Hermes estimate also includes request pricing).
    # A zero total is not exported: Langfuse would treat it as authoritative.
    if cost.status != "included" and float(cost.amount_usd) > 0:
        cost_details["total"] = float(cost.amount_usd)

    # Per-type breakdown for dashboards; keys mirror usage_details.
    try:
        from decimal import Decimal

        from agent.usage_pricing import get_pricing_entry

        entry = get_pricing_entry(model, provider=provider, base_url=base_url)
        # Above the context tier, estimate_usage_cost bills the WHOLE request at the
        # *_above rates; use them here too or the breakdown disagrees with "total".
        above = bool(entry) and entry.tier_threshold_tokens is not None and canonical.prompt_tokens > entry.tier_threshold_tokens
        for key, attr, rate_attr in _USAGE_FIELDS if entry else ():
            rate_above = getattr(entry, f"{rate_attr}_above", None) if above and rate_attr else None
            rate = rate_above if rate_above is not None else getattr(entry, rate_attr, None) if rate_attr else None
            tokens = getattr(canonical, attr)
            if rate is not None and tokens:
                cost_details[key] = float(Decimal(tokens) * rate / Decimal(1000000))
    except Exception:  # pragma: no cover - canonical total remains usable
        pass

    return usage_details, cost_details


def _usage_and_cost(response: Any, *, provider: str, model: str, base_url: str, api_mode: str = "",
                    usage: Optional[dict] = None) -> tuple[dict[str, int], dict[str, float]]:
    """Langfuse usage/cost maps from ``response.usage`` (post_llm_call) or, when ``usage``
    is given (post_api_request), from that pre-built CanonicalUsage summary dict."""
    raw_usage = getattr(response, "usage", None)
    if usage is None and not raw_usage:
        return {}, {}
    try:
        from agent.usage_pricing import CanonicalUsage, normalize_usage

        canonical = normalize_usage(raw_usage, provider=provider, api_mode=api_mode) if usage is None else CanonicalUsage(
            output_tokens=usage.get("output_tokens", 0) or usage.get("completion_tokens", 0),
            request_count=usage.get("request_count", 1),
            **{attr: usage.get(attr, 0) for attr in ("input_tokens", "cache_read_tokens", "cache_write_tokens", "reasoning_tokens")},
        )
        return _canonical_usage_and_cost(canonical, provider=provider, model=model, base_url=base_url)
    except Exception as exc:  # pragma: no cover - fail-open
        if usage is None:
            _debug(f"usage normalization failed: {exc}")
        return {}, {}


def _trace_tags(platform: str) -> list[str]:
    """Stable, content-free root tags so trigger type is filterable (hermes-agent#88502):
    ``platform:<name>`` and ``trigger:cron|interactive``. A cron run also gets ``cron-job:<id>``:
    each run opens a fresh session (``cron_<job-id>_<timestamp>``), so the job id is the only stable
    handle across runs. Read from the scheduler's own ``ctx.current_cron_execution()`` rather than
    parsed out of the session id, so legacy job ids and look-alike sessions need no special case."""
    platform = (platform or "").strip().lower()
    cron = _PLUGIN_CTX.current_cron_execution() if _PLUGIN_CTX is not None else None
    return ["hermes", "langfuse", *([f"platform:{platform}"] if platform else []),
            f"trigger:{'cron' if platform == 'cron' else 'interactive'}", *([f"cron-job:{cron.job_id}"] if cron else [])]


def _start_root_trace(task_key: str, *, task_id: str, session_id: str, platform: str, provider: str, model: str,
                      api_mode: str, messages: Any, client: Langfuse,
                      turn_id: str = "", api_request_id: str = "") -> TraceState:
    # Session IDs deliberately group Langfuse traces, but cannot also identify
    # a trace: gateway and hosted-room turns retain one session across many
    # replies.  Prefer the unique turn ID (then request ID for hook callers
    # without a turn boundary) so every emitted root has a distinct Cloud ID.
    trace_scope = turn_id or api_request_id or task_id or task_key
    trace_id = client.create_trace_id(seed=f"{session_id or 'sessionless'}::{trace_scope}")
    last_user = next((m for m in reversed(messages) if isinstance(m, dict) and m.get("role") == "user"), None) \
        if isinstance(messages, list) else None
    trace_input = None if last_user is None else {"role": "user", "content": _capture_content(last_user.get("content"))}
    # The profile the turn runs FOR (ctx.profile_name follows the routed home under multiplexing,
    # not the launch profile), so traces from several profiles in one project can be told apart.
    profile = _PLUGIN_CTX.profile_name if _PLUGIN_CTX is not None else ""
    # A dispatcher-spawned kanban worker gets its board task / run id in its env, so its traces can
    # be joined back to the card without guessing from session names. Omitted outside kanban.
    kanban = {k: v for k, v in (("kanban_task_id", _env("HERMES_KANBAN_TASK")),
                                ("kanban_run_id", _env("HERMES_KANBAN_RUN_ID"))) if v}
    metadata = {
        "source": "hermes", "task_id": task_id, "turn_id": turn_id, "api_request_id": api_request_id,
        "platform": platform, "provider": provider, "model": model, "api_mode": api_mode,
        "capture_mode": _capture_mode(), "profile": profile, **kanban,
    }
    # session_id must be in trace_context for Langfuse session grouping.
    trace_ctx: dict[str, Any] = {"trace_id": trace_id, **({"session_id": session_id} if session_id else {})}

    # Detached start_observation(), not start_as_current_observation(): the "current" variant pushes an OTEL
    # contextvars token that must be popped in the same context it was pushed in, but the root span is ended by
    # a later hook on another worker thread, so every turn logged "Failed to detach context" (hermes-agent#95057). A span
    # created inside propagate_attributes still carries its session / trace name / tags.
    def open_root():
        return client.start_observation(trace_context=trace_ctx, name="Hermes turn", as_type="chain",
                                        input=trace_input, metadata=metadata)

    root_span = None
    propagated = {"session_id": session_id or task_key, "trace_name": "Hermes turn", "tags": _trace_tags(platform)}
    if profile or kanban:
        propagated["metadata"] = {**({"profile": profile} if profile else {}), **kanban}
    if propagate_attributes is not None:
        try:
            with propagate_attributes(**propagated):
                root_span = open_root()
        except Exception:
            root_span = None
    if root_span is None:
        root_span = open_root()

    # Trace-level I/O: set_trace_io() in the pinned SDK v4 (v3's update_trace() is gone, and
    # calling it failed open on every turn, leaving traces without input/output: hermes-agent#94710).
    with _failsafe("set_trace_io(input)"):
        root_span.set_trace_io(input=trace_input)

    _debug(f"started trace {trace_id} for {task_key}")
    return TraceState(trace_id=trace_id, root_span=root_span, propagated=propagated)


def _start_child_observation(state: TraceState, *, name: str, as_type: str, input_value: Any,
                             metadata: Optional[dict] = None, model: Optional[str] = None,
                             model_parameters: Optional[dict] = None) -> Any:
    # propagate_attributes() only stamps observations created inside its scope, and children are
    # created by later hooks (often on another thread), so re-enter it per child; otherwise every
    # generation/tool span carries no session_id/tags and observation-level analytics bucket all
    # tokens and cost under a null session (hermes-agent#81730).
    with propagate_attributes(**state.propagated) if propagate_attributes and state.propagated \
            else contextlib.nullcontext():
        return state.root_span.start_observation(name=name, as_type=as_type, input=input_value,
                                                 metadata=metadata or {}, model=model,
                                                 model_parameters=model_parameters)


def _end_observation(observation: Any, *, output: Any = None, metadata: Optional[dict] = None,
                     usage_details: Optional[dict] = None, cost_details: Optional[dict] = None,
                     level: Optional[str] = None, status_message: Optional[str] = None) -> None:
    if observation is None:
        return
    with _failsafe("end observation"):
        update_kwargs = {**({} if output is None else {"output": output}),
                         **{k: v for k, v in (("metadata", metadata), ("usage_details", usage_details),
                                              ("cost_details", cost_details), ("level", level),
                                              ("status_message", status_message)) if v}}
        if update_kwargs:
            observation.update(**update_kwargs)
        observation.end()


def _end_children(state: TraceState, *, include_subagents: bool = False) -> None:
    pending = [obs for queue in state.pending_tools_by_name.values() for obs in queue]
    # post_api_request / post_tool_call pop their entry on a normal finish, so a
    # generation or tool still here never got its response/result (interrupt,
    # exhausted retries, disconnect): mark it instead of closing it as a silent success.
    for observation in (*state.generations.values(), *state.tools.values(), *pending):
        _end_observation(observation, level="ERROR",
                         status_message="Turn ended before this call's response/result was recorded.")
    for observation in (state.subagents.values() if include_subagents else ()):
        _end_observation(observation)


def _end_root(state: TraceState, label: str) -> None:
    """End the root span; never raises."""
    with _failsafe(label):
        state.root_span.end()


def _finalize_all_traces() -> None:
    """atexit: end every open root span. Short-lived processes (kanban workers,
    ``chat -q``, cron) exit with tool calls queued; children export via the SDK
    flush but an un-ended root leaves an anonymous trace. Registered after the
    client is built so (LIFO) it runs before the SDK's shutdown hook."""
    with _STATE_LOCK:
        states = list(_TRACE_STATE.items())
        _TRACE_STATE.clear()
    for key, state in states:
        with _failsafe(f"atexit finalize for {key}"):  # _end_root never raises
            _end_children(state, include_subagents=True)
            _end_root(state, f"atexit finalize for {key}")
    if states:
        # atexit runs with NO profile scope, so it must never build a client (a credential read
        # here raises UnscopedSecretError under multiplex and would skip every flush). Flush only
        # the clients that settled during the run — the launch profile's slot plus one per home.
        for client in (_LANGFUSE_CLIENT, *_LANGFUSE_CLIENT_BY_HOME.values()):
            if client is not None and client is not _INIT_FAILED:
                _flush(client)


def _flush(client: Any) -> None:
    if client is not None:
        with contextlib.suppress(Exception):
            client.flush()


def _finish_trace(task_key: str, *, output: Any = None) -> None:
    """End spans locally; the SDK's background exporter owns delivery.

    A synchronous flush here waits for network I/O, so an unavailable
    observability backend can hold up a completed agent turn.
    """
    client = _get_langfuse()
    with _STATE_LOCK:
        state = _TRACE_STATE.pop(task_key, None) if client is not None else None
    if state is None:
        return

    try:
        _end_children(state)
        final_output = output
        if state.turn_tool_calls:
            final_output = dict(output) if isinstance(output, dict) else {"content": output}
            final_output["tool_calls"] = list(state.turn_tool_calls)
        if final_output is not None:
            # set_trace_io sets TRACE-level I/O (SDK v4); root I/O via update().
            # Neither may prevent end(), else children export without a root.
            for method, label in (("set_trace_io", "set_trace_io(output)"), ("update", "root update(output)")):
                with _failsafe(label):
                    getattr(state.root_span, method)(output=final_output)
        _end_root(state, "root end()")
    except Exception as exc:  # pragma: no cover - fail-open
        _debug(f"finish trace failed: {exc}")
        with contextlib.suppress(Exception):  # last-chance end so the root still exports
            state.root_span.end()


def _request_key(api_call_count: Any) -> str:
    return str(api_call_count or 0)


def _client_and_key(task_id: str, session_id: str, turn_id: str, api_request_id: str) -> tuple[Any, str]:
    """(client, trace key) for a hook; client is None when tracing is unavailable."""
    client = _get_langfuse()
    if client is None:
        return None, ""
    return client, _trace_key(task_id, session_id, turn_id=turn_id, api_request_id=api_request_id)


def _duration_meta(api_duration: Any) -> dict[str, Any]:
    return {"api_duration_s": round(api_duration, 3)} if api_duration and api_duration > 0 else {}


def _pop_generation(task_key: str, api_call_count: Any) -> tuple[Optional[TraceState], Any]:
    """Detach the open generation for one API call. Returns (state, generation); either may be None."""
    with _STATE_LOCK:
        state = _TRACE_STATE.get(task_key)
        return state, state.generations.pop(_request_key(api_call_count), None) if state else None


def _get_or_start_state_locked(task_key: str, **root_kwargs: Any) -> TraceState:
    """Caller must hold ``_STATE_LOCK``. Starts a root trace if the key is new, first
    evicting least-recently-updated state down to ``_MAX_TRACE_STATE - 1`` (evicted
    roots are ended so they don't dangle on the Langfuse side)."""
    state = _TRACE_STATE.get(task_key)
    if state is None:
        state = _start_root_trace(task_key, **root_kwargs)
        over = len(_TRACE_STATE) - (_MAX_TRACE_STATE - 1)
        for key, stale in sorted(_TRACE_STATE.items(), key=lambda kv: kv[1].last_updated_at)[:max(over, 0)]:
            _TRACE_STATE.pop(key, None)
            _end_root(stale, "evict stale trace")
        _TRACE_STATE[task_key] = state
    state.last_updated_at = time.time()
    return state


def on_pre_llm_call(*, task_id: str = "", session_id: str = "", platform: str = "", model: str = "",
                    provider: str = "", api_mode: str = "", messages: Any = None,
                    turn_id: str = "", api_request_id: str = "", **_: Any) -> None:
    # Only legacy request-shaped calls carry an API ``messages`` list; the
    # turn-scoped pre_llm_call would otherwise open an orphan root trace.
    if not isinstance(messages, list):
        return
    client, task_key = _client_and_key(task_id, session_id, turn_id, api_request_id)
    if client is None:
        return
    with _STATE_LOCK:
        _get_or_start_state_locked(task_key, task_id=task_id, session_id=session_id, platform=platform, provider=provider, model=model,
                                   api_mode=api_mode, messages=messages, client=client, turn_id=turn_id, api_request_id=api_request_id)


def _emit_moa_reference_generations(state: TraceState, *, client: Langfuse, references: Any) -> None:
    """Record each MoA advisor as its own generation: advisors routinely run on a
    different provider/model, so otherwise the fan-out would collapse into one
    generation priced at the aggregator's rate."""
    if not isinstance(references, list) or not references:
        return
    fingerprint = json.dumps(
        [[r.get("label"), r.get("model"), (r.get("usage") or {}).get("output_tokens")]
         for r in references if isinstance(r, dict)],
        sort_keys=True, default=str,
    )
    with _STATE_LOCK:
        if fingerprint in state.moa_emitted:
            return
        state.moa_emitted.add(fingerprint)

    for ref in references:
        if not isinstance(ref, dict):
            continue
        usage = ref.get("usage") or {}
        usage_details = {key: usage[attr] for key, attr, _ in _USAGE_FIELDS if usage.get(attr)} if isinstance(usage, dict) else {}
        cost_usd = ref.get("cost_usd")
        cost_details = {"total": float(cost_usd)} if isinstance(cost_usd, (int, float)) else {}

        label = ref.get("label") or "advisor"
        metadata = {"moa_role": "reference", "label": label,
                    **{k: ref[k] for k in ("provider", "cost_status", "cost_source", "temperature") if ref.get(k) is not None}}

        observation = _start_child_observation(state, name=f"MoA advisor: {label}", as_type="generation", input_value=None,
                                               metadata=metadata, model=ref.get("model"))
        _end_observation(observation, output=_capture_content(ref.get("output")), usage_details=usage_details,
                         cost_details=cost_details, metadata=metadata)


def on_pre_llm_request(*, task_id: str = "", session_id: str = "", platform: str = "", model: str = "",
                       provider: str = "", base_url: str = "", api_mode: str = "", api_call_count: int = 0,
                       request_messages: Any = None, messages: Any = None, message_count: int = 0,
                       approx_input_tokens: int = 0, conversation_history: Any = None,
                       user_message: Any = None, turn_id: str = "", api_request_id: str = "",
                       request: Any = None, system_prompt: Any = None, **_: Any) -> None:
    client, task_key = _client_and_key(task_id, session_id, turn_id, api_request_id)
    if client is None:
        return

    # The request body carries the model actually dispatched (mid-session
    # switch, fallback, middleware rewrite) — prefer it over the agent attribute.
    body_model = request["body"].get("model") if isinstance(request, dict) and isinstance(request.get("body"), dict) else None
    if isinstance(body_model, str) and body_model:
        model = body_model

    input_messages = _coerce_request_messages(request_messages=request_messages, messages=messages,
                                              conversation_history=conversation_history, user_message=user_message)
    langfuse_input = _messages_for_langfuse_input(request_messages=input_messages, system_prompt=system_prompt)
    has_system = bool(langfuse_input) and langfuse_input[0].get("role") == "system"
    system_chars = len(str(langfuse_input[0].get("content") or "")) if has_system else 0
    req_key = _request_key(api_call_count)
    # Metadata skips content capture, but a proxy base_url can embed user:pass@ or ?api_key=.
    from agent.redact import redact_sensitive_text
    safe_base_url = redact_sensitive_text(base_url, force=True, redact_url_credentials=True)

    with _STATE_LOCK:
        state = _get_or_start_state_locked(
            task_key, task_id=task_id, session_id=session_id, platform=platform, provider=provider, model=model,
            api_mode=api_mode, messages=input_messages, client=client, turn_id=turn_id, api_request_id=api_request_id)
        previous = state.generations.pop(req_key, None)
        if previous is not None:
            _end_observation(previous, level="WARNING",
                             status_message="Retried: no response was recorded for this attempt.")
        gen_metadata = {
            "provider": provider, "platform": platform, "api_mode": api_mode, "base_url": safe_base_url,
            "message_count": message_count, "approx_input_tokens": approx_input_tokens,
            **({"system_prompt_chars": system_chars} if system_chars else {}),
        }
        state.generations[req_key] = _start_child_observation(
            state, name=f"LLM call {api_call_count}", as_type="generation",
            input_value=langfuse_input, metadata=gen_metadata, model=model,
            model_parameters={"api_mode": api_mode, "provider": provider},
        )


def on_post_llm_call(*, task_id: str = "", session_id: str = "", provider: str = "", base_url: str = "",
                     api_mode: str = "", model: str = "", api_call_count: int = 0, assistant_message: Any = None,
                     response: Any = None, api_duration: float = 0.0, finish_reason: str = "", usage: Any = None,
                     assistant_content_chars: int = 0, assistant_tool_call_count: int = 0,
                     assistant_response: Any = None, turn_id: str = "", api_request_id: str = "",
                     response_model: Any = None, moa_references: Any = None, **_: Any) -> None:
    client, task_key = _client_and_key(task_id, session_id, turn_id, api_request_id)
    if client is None:
        return

    # The response echoes the model that actually served the request.
    if isinstance(response_model, str) and response_model:
        model = response_model

    state, generation = _pop_generation(task_key, api_call_count)
    if state is None or generation is None:
        return

    if moa_references:
        _emit_moa_reference_generations(state, client=client, references=moa_references)

    # Two call shapes: post_llm_call passes assistant_message / assistant_response
    # objects; post_api_request passes summary counts + a usage dict.
    if assistant_message is not None:
        output = _serialize_assistant_message(assistant_message)
    elif assistant_response is not None:
        output = {"role": "assistant", "content": _capture_content(assistant_response), "reasoning": None, "tool_calls": []}
    else:
        output = {"role": "assistant", "content": f"[{assistant_content_chars} chars]" if assistant_content_chars else None, "reasoning": None,
                  "tool_calls": [{"id": f"tc_{i}"} for i in range(assistant_tool_call_count or 0)]}

    if output.get("tool_calls"):
        state.turn_tool_calls.extend(output["tool_calls"])

    # post_api_request's ``response`` is a sanitized dict with no ``.usage``;
    # gate on the attribute so the usage-dict fallback is actually reached.
    if getattr(response, "usage", None) is not None:
        usage_details, cost_details = _usage_and_cost(response, provider=provider, api_mode=api_mode, model=model, base_url=base_url)
    elif isinstance(usage, dict) and usage:
        usage_details, cost_details = _usage_and_cost(None, provider=provider, model=model, base_url=base_url, usage=usage)
    else:
        usage_details, cost_details = {}, {}

    gen_metadata = {"tool_call_count": len(output.get("tool_calls", [])) or assistant_tool_call_count,
                    **_duration_meta(api_duration), **({"finish_reason": finish_reason} if finish_reason else {})}
    _end_observation(generation, output=output, usage_details=usage_details, cost_details=cost_details, metadata=gen_metadata)

    has_tools = bool(getattr(assistant_message, "tool_calls", None)) if assistant_message else assistant_tool_call_count > 0
    if not has_tools and output.get("content"):
        _finish_trace(task_key, output=output)


def on_pre_tool_call(*, tool_name: str = "", args: Any = None, task_id: str = "",
                     session_id: str = "", tool_call_id: str = "",
                     turn_id: str = "", api_request_id: str = "", **_: Any) -> None:
    client, task_key = _client_and_key(task_id, session_id, turn_id, api_request_id)
    if client is None:
        return
    with _STATE_LOCK:
        state = _TRACE_STATE.get(task_key)
        if state is None:
            return
        observation = _start_child_observation(state, name=f"Tool: {tool_name}", as_type="tool", input_value=_capture_content(args),
                                               metadata={"tool_name": tool_name, "tool_call_id": tool_call_id})
        if tool_call_id:
            state.tools[tool_call_id] = observation
        else:
            state.pending_tools_by_name.setdefault(tool_name, []).append(observation)


def on_post_tool_call(*, tool_name: str = "", args: Any = None, result: Any = None,
                      task_id: str = "", session_id: str = "", tool_call_id: str = "",
                      turn_id: str = "", api_request_id: str = "", status: str = "",
                      error_type: Any = None, **_: Any) -> None:
    task_key = _trace_key(task_id, session_id, turn_id=turn_id, api_request_id=api_request_id)
    with _STATE_LOCK:
        state = _TRACE_STATE.get(task_key)
        if state is None:
            return
        observation = state.tools.pop(tool_call_id, None) if tool_call_id else None
        queue = state.pending_tools_by_name.get(tool_name) if observation is None else None
        if queue:
            observation = queue.pop(0)
            if not queue:
                state.pending_tools_by_name.pop(tool_name, None)
    if observation is None:
        return

    safe_result_value = _capture_content(result, tool_result_of=(tool_name, args))

    # Backfill so the generation's tool_call record carries the result alongside arguments.
    if tool_call_id:
        with _STATE_LOCK:
            state = _TRACE_STATE.get(task_key)
            calls = state.turn_tool_calls if state is not None else []
            tool_call = next((tc for tc in reversed(calls) if tc.get("id") == tool_call_id), None)
            for target in (tool_call, tool_call.get("function")) if tool_call is not None else ():
                if isinstance(target, dict):
                    target["output"] = safe_result_value

    # Hermes derives status ok/error (explicit blocked/cancelled) for observers; without a
    # level a failed tool call lands at DEFAULT, indistinguishable from a successful one.
    # status_message stays the error type: the message can echo result content, which the
    # capture mode already governs via output.
    level = {"error": "ERROR", "blocked": "WARNING", "cancelled": "WARNING"}.get(status)
    _end_observation(observation, output=safe_result_value,
                     metadata={"tool_name": tool_name, "args": _capture_content(args, parse_json_strings=True)},
                     level=level, status_message=str(error_type or status)[:200] if level else None)


def on_api_request_error(*, task_id: str = "", session_id: str = "", api_call_count: int = 0,
                         api_duration: float = 0.0, status_code: Any = None, retry_count: Any = None,
                         max_retries: Any = None, retryable: Any = None, reason: Any = None, error: Any = None,
                         turn_id: str = "", api_request_id: str = "", **_: Any) -> None:
    """Close (as ERROR) the open generation for a failed API request so the turn
    doesn't look hung until eviction; a non-retryable failure also finishes the
    turn, since the agent loop is about to unwind."""
    client, task_key = _client_and_key(task_id, session_id, turn_id, api_request_id)
    if client is None:
        return
    state, generation = _pop_generation(task_key, api_call_count)
    if state is None:
        return

    error = error if isinstance(error, dict) else {}
    error_type, error_message = str(error.get("type") or ""), str(error.get("message") or "")

    # Error messages can embed request fragments (URLs w/ keys, prompt echoes) — capture-pipeline them.
    error_metadata: dict[str, Any] = {
        "error": True, "error_type": error_type, "error_message": _capture_content(error_message),
        **{k: v for k, v in (("status_code", status_code), ("retry_count", retry_count), ("max_retries", max_retries),
                             ("retryable", retryable), ("reason", str(reason) if reason else None)) if v is not None},
        **_duration_meta(api_duration),
    }

    if generation is not None:
        with _failsafe("error-level update"):
            generation.update(level="ERROR", status_message=(error_type or "api_request_error")[:200])
        _end_observation(generation, metadata=error_metadata)

    # A retryable failure is followed by another pre_api_request on the same
    # trace; keep the turn open. A terminal failure ends the turn.
    if retryable is False:
        _finish_trace(task_key, output={"error": error_metadata})
    else:
        state.last_updated_at = time.time()


def on_session_finalize(*, session_id: str = "", reason: str = "", **_: Any) -> None:
    """Session-end boundary: close still-open traces. A turn ending on a
    tool-only or empty final response never reaches ``_finish_trace``; its root
    would dangle until eviction. Only process shutdown waits for export."""
    # Never lazily initialize a client here — if init never happened there are no traces.
    client = _settled_client()
    if client is None or client is _INIT_FAILED or not hasattr(client, "flush"):
        return

    # This session's traces (all, when no session_id). Keys carry the session as
    # "session:<id>" or "task:<id>" (gateway: task_id == session_id) or bare legacy id.
    fragments = (f"session:{session_id}", f"task:{session_id}")
    with _STATE_LOCK:
        keys = [k for k in _TRACE_STATE if not session_id or k == session_id or any(f in k for f in fragments)]
    for key in keys:
        _finish_trace(key)

    # /new, expiry and other session boundaries must not wait for the backend.
    # The SDK keeps exporting ended spans in its bounded background queue.
    if reason != "shutdown":
        return
    with _failsafe("finalize flush"):
        client.flush()

    # Shut down only at true process exit (not /new, /reset, session expiry: the
    # cached client must keep exporting). Doing it while modules are intact keeps
    # the SDK's atexit handler off torn-down opentelemetry globals (TypeError on quit).
    if callable(getattr(client, "shutdown", None)):
        with _failsafe("langfuse shutdown"):
            client.shutdown()


def on_subagent_start(*, parent_turn_id: str = "", parent_subagent_id: Any = None,
                      child_session_id: Any = None, child_subagent_id: Any = None,
                      child_role: str = "", child_goal: Any = None, **_: Any) -> None:
    client = _get_langfuse()
    if client is None or not child_session_id:
        return

    with _STATE_LOCK:
        state = _state_for_turn(parent_turn_id)
        if state is None:
            return
        metadata = {"child_session_id": child_session_id, "child_subagent_id": child_subagent_id, "child_role": child_role,
                    **({"parent_subagent_id": parent_subagent_id} if parent_subagent_id else {})}
        state.subagents[str(child_session_id)] = _start_child_observation(
            state, name=f"Subagent: {child_role or 'delegate'}", as_type="span",
            input_value=_capture_content(child_goal), metadata=metadata)


def on_subagent_stop(*, parent_turn_id: str = "", child_session_id: Any = None, child_role: str = "",
                     child_summary: Any = None, child_status: Any = None,
                     tool_call_history: Any = None, duration_ms: Any = None, **_: Any) -> None:
    if not child_session_id:
        return

    with _STATE_LOCK:
        state = _state_for_turn(parent_turn_id)
        if state is None:
            return
        observation = state.subagents.pop(str(child_session_id), None)
    if observation is None:
        return

    metadata = {"child_role": child_role, **{k: v for k, v in (("status", child_status), ("duration_ms", duration_ms)) if v},
                **({"tool_call_count": len(tool_call_history), "tool_calls": _capture_content(tool_call_history)}
                   if isinstance(tool_call_history, list) else {})}
    _end_observation(observation, output=_capture_content(child_summary), metadata=metadata)


def _core_ships_langfuse() -> bool:
    """True on a Hermes core that still bundles this plugin as ``plugins/observability/langfuse``.

    That copy also answers to ``langfuse`` in ``plugins.enabled``, so registering here too would send
    every trace twice. Looks only inside the imported ``plugins`` package's own ``__path__``."""
    try:
        import plugins as core_plugins
    except ImportError:
        return False
    return any((Path(p) / "observability" / "langfuse" / "plugin.yaml").is_file()
               for p in getattr(core_plugins, "__path__", None) or ())


def register(ctx) -> None:
    if _core_ships_langfuse():
        logger.info("langfuse: this Hermes core still ships Langfuse built in; "
                    "the plugin stays inactive until core drops it")
        return
    global _PLUGIN_CTX
    _PLUGIN_CTX = ctx
    # Both hook-name variants so the plugin works across Hermes versions:
    # *_api_request fire per API call (preferred); *_llm_call once per turn.
    hooks = (
        ("pre_api_request", on_pre_llm_request), ("post_api_request", on_post_llm_call),
        ("api_request_error", on_api_request_error), ("pre_llm_call", on_pre_llm_call),
        ("post_llm_call", on_post_llm_call), ("pre_tool_call", on_pre_tool_call),
        ("post_tool_call", on_post_tool_call), ("on_session_finalize", on_session_finalize),
        ("on_session_end", on_session_finalize), ("subagent_start", on_subagent_start),
        ("subagent_stop", on_subagent_stop),
    )
    for name, fn in hooks:
        ctx.register_hook(name, fn)
