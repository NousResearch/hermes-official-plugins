"""Safety and output shaping for the Camofox browser tools.

* Navigation policy, the checks core ran before a Camofox navigation: URLs carrying a Hermes
  secret, cloud-metadata endpoints and hosts on the user's website blocklist
  (``security.website_blocklist``) are refused before the request is sent. Camofox counted as a
  local browser in core, so private/LAN addresses stay reachable, as before; like core's Camofox
  path, a redirect or in-page navigation is not re-checked.
* ``browser.restrict_evaluate`` / ``browser.allow_unsafe_evaluate``: the opt-in denylist of
  sensitive JavaScript primitives for ``browser_console(expression=...)``.
* Oversized snapshots are cut at a line boundary; the full tree is stored (secrets force-redacted)
  under ``cache/web`` and the cut tells the model how to page through it with ``read_file``.
* Typed text that looks like a secret is masked in what goes back to the model.

The few core helpers used here (URL safety, website policy, secret redaction, the agent-visible
cache path) have no documented plugin seam yet; they are imported where they are used and listed in
the README as the seams this plugin asks core to publish.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import urllib.parse
from typing import Any, Optional

from hermes_cli.config import cfg_get, read_raw_config

logger = logging.getLogger(__name__)

DEFAULT_SNAPSHOT_THRESHOLD = 15000
MIN_SNAPSHOT_THRESHOLD = 1000
MAX_STORED_SNAPSHOT_CHARS = 2_000_000
_REDACT_WALK_MAX_DEPTH = 320
_REDACT_WALK_DEPTH_MARKER = "[redacted: nesting limit]"


def _err(error: str, **extra: Any) -> dict[str, Any]:
    return {"success": False, "error": error, **extra}


def _browser_cfg(key: str, default: Any = None) -> Any:
    try:
        return cfg_get(read_raw_config(), "browser", key, default=default)
    except Exception as exc:
        logger.debug("Could not read browser.%s: %s", key, exc)
        return default


# ---- Redaction ----

def redact_text(text: str) -> str:
    """Force-redact secrets in browser-originated text (tool output is a model boundary)."""
    from agent.redact import redact_sensitive_text
    return redact_sensitive_text(text, force=True)


def redact_output(value: Any, *, _depth: int = 0) -> Any:
    """Force-redact secrets in nested browser-originated data; a page-controlled tree deeper than
    the walk limit is replaced, never returned raw."""
    if _depth >= _REDACT_WALK_MAX_DEPTH:
        return _REDACT_WALK_DEPTH_MARKER
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, (list, tuple)):
        return type(value)(redact_output(item, _depth=_depth + 1) for item in value)
    if isinstance(value, dict):
        return {redact_output(k, _depth=_depth + 1): redact_output(v, _depth=_depth + 1) for k, v in value.items()}
    return value


def redact_typed_text(value: Any, typed_text: Any) -> Any:
    """Replace every occurrence of a secret-looking typed value with its redacted form (servers echo
    the attempted input in errors/metadata)."""
    needle = "" if typed_text is None else str(typed_text)
    redacted = redact_text(needle) if needle else needle
    if redacted == needle:
        return value
    if isinstance(value, str):
        return value.replace(needle, redacted)
    if isinstance(value, dict):
        return {key: redact_typed_text(item, typed_text) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return type(value)(redact_typed_text(item, typed_text) for item in value)
    return value


# ---- Navigation policy ----

def _secret_url_error(url: str) -> Optional[dict]:
    """Refuse URLs embedding an API key/token (raw and URL-decoded): a prompt injection could
    otherwise exfiltrate secrets through the URL."""
    import agent.redact as redact  # module attribute: core rebinds _PREFIX_RE when patterns are added
    if redact._PREFIX_RE.search(url) or redact._PREFIX_RE.search(urllib.parse.unquote(url)):
        return _err("Blocked: URL contains what appears to be an API key or token. Secrets must not be sent in URLs.")
    return None


def navigation_policy_error(url: str) -> tuple[str, Optional[dict]]:
    """``(normalized_url, error_or_None)`` for a ``browser_navigate`` target."""
    from tools.url_safety import is_always_blocked_url, normalize_url_for_request
    from tools.website_policy import check_website_access

    err = _secret_url_error(url)
    if err is not None:
        return url, err
    url = normalize_url_for_request(url)
    err = _secret_url_error(url)
    if err is not None:
        return url, err
    if is_always_blocked_url(url):
        return url, _err("Blocked: URL targets a cloud metadata endpoint")
    blocked = check_website_access(url)
    if blocked:
        return url, _err(blocked["message"], blocked_by_policy={
            "host": blocked["host"], "rule": blocked["rule"], "source": blocked["source"]})
    return url, None


# ---- browser_console(expression=...) denylist (opt-in) ----

_RISKY_EVAL_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bdocument\s*\.\s*cookie\b", re.IGNORECASE), "document.cookie"),
    (re.compile(r"\b(?:localStorage|sessionStorage)\b", re.IGNORECASE), "web storage"),
    (re.compile(r"\bindexedDB\b", re.IGNORECASE), "IndexedDB"),
    (re.compile(r"\bcaches\s*\.\s*(?:open|match|keys)\b", re.IGNORECASE), "Cache Storage"),
    (re.compile(r"\bnavigator\s*\.\s*(?:clipboard|credentials|serviceWorker)\b", re.IGNORECASE), "navigator sensitive API"),
    (re.compile(r"\b(?:fetch|XMLHttpRequest|WebSocket|EventSource)\s*\(", re.IGNORECASE), "network request"),
    (re.compile(r"\bnavigator\s*\.\s*sendBeacon\s*\(", re.IGNORECASE), "network beacon"),
    (re.compile(r"\bdocument\s*\.\s*forms\b.*\bvalue\b", re.IGNORECASE | re.DOTALL), "form value extraction"),
    (re.compile(r"\bquerySelector(?:All)?\s*\([^)]*(?:input|textarea|password)[^)]*\).*\bvalue\b",
                re.IGNORECASE | re.DOTALL), "form value extraction"),
)
_JS_STRING_LITERAL_RE = re.compile(r"""'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\"|`(?:\\.|[^`\\])*`""", re.DOTALL)
_SENSITIVE_EVAL_TOKENS: tuple[tuple[str, str], ...] = (
    ("cookie", "document.cookie"),
    ("localStorage", "web storage"), ("sessionStorage", "web storage"),
    ("indexedDB", "IndexedDB"), ("caches", "Cache Storage"),
    ("clipboard", "navigator sensitive API"), ("credentials", "navigator sensitive API"),
    ("serviceWorker", "navigator sensitive API"),
    ("fetch", "network request"), ("XMLHttpRequest", "network request"),
    ("WebSocket", "network request"), ("EventSource", "network request"),
    ("sendBeacon", "network beacon"),
)


def _truthy(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "on"} if value is not None else False


def _decode_js_string_literal(literal: str) -> str:
    """Best-effort decode of a JS string literal (normalizes escapes like ``"co\\x6fkie"``)."""
    if len(literal) < 2:
        return literal
    try:
        return bytes(literal[1:-1], "utf-8").decode("unicode_escape")
    except Exception:
        return literal[1:-1]


def _risky_eval_reason(expression: str) -> Optional[str]:
    """Why a JS expression is risky (direct spellings, or tokens inside decoded string literals that
    catch ``document["coo" + "kie"]``), else None."""
    if not expression:
        return None
    hit = next((reason for pattern, reason in _RISKY_EVAL_PATTERNS if pattern.search(expression)), None)
    if hit:
        return hit
    literals = "".join(_decode_js_string_literal(m.group(0)) for m in _JS_STRING_LITERAL_RE.finditer(expression)).lower()
    return next((reason for token, reason in _SENSITIVE_EVAL_TOKENS
                 if re.search(rf"\b{re.escape(token)}\b", expression, re.IGNORECASE) or token.lower() in literals), None)


def eval_policy_error(expression: str) -> Optional[str]:
    """Refusal text when ``browser.restrict_evaluate`` is on (and ``allow_unsafe_evaluate`` off) and
    the expression uses a sensitive primitive, else None."""
    if not _truthy(_browser_cfg("restrict_evaluate")) or _truthy(_browser_cfg("allow_unsafe_evaluate")):
        return None
    reason = _risky_eval_reason(expression)
    if not reason:
        return None
    return ("Blocked: browser_console(expression=...) tried to use sensitive browser "
            f"JavaScript primitive ({reason}) while browser.restrict_evaluate is "
            "enabled. Use browser_snapshot/browser_get_images/browser_console "
            "without expression for normal inspection, or set "
            "browser.restrict_evaluate: false in config.yaml to allow programmatic evaluation.")


# ---- Snapshot truncation ----

def snapshot_threshold() -> int:
    """``browser.snapshot_threshold`` (floored at MIN_SNAPSHOT_THRESHOLD)."""
    try:
        val = _browser_cfg("snapshot_threshold")
        return DEFAULT_SNAPSHOT_THRESHOLD if val is None else max(int(val), MIN_SNAPSHOT_THRESHOLD)
    except (TypeError, ValueError):
        return DEFAULT_SNAPSHOT_THRESHOLD


def _write_cache_file(path: str, content: str) -> None:
    """Replace ``path`` with ``content`` without following a planted symlink."""
    try:
        if os.path.islink(path) or os.path.exists(path):
            os.unlink(path)
    except FileNotFoundError:
        pass
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o644)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(content)


def _store_full_snapshot(snapshot_text: str) -> Optional[str]:
    """Write the force-redacted full snapshot to ``cache/web`` (content-hash name) and return the
    path the agent's file tools see, or None on failure."""
    try:
        from hermes_constants import get_hermes_dir
        from tools.credential_files import to_agent_visible_cache_path

        content = redact_text(snapshot_text)
        if len(content) > MAX_STORED_SNAPSHOT_CHARS:
            content = (content[:MAX_STORED_SNAPSHOT_CHARS]
                       + f"\n\n[... stored copy truncated at {MAX_STORED_SNAPSHOT_CHARS:,} chars of {len(content):,} ...]")
        cache_dir = get_hermes_dir("cache/web", "web_cache")
        if cache_dir.is_symlink():
            raise OSError(f"refusing to write through a symlinked {cache_dir}")
        cache_dir.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:10]
        path = str(cache_dir / f"browser-snapshot-{digest}.txt")
        _write_cache_file(path, content)
        return to_agent_visible_cache_path(path)
    except Exception as exc:
        logger.debug("Failed to store full browser snapshot: %s", exc)
        return None


def truncate_snapshot(snapshot_text: str, max_chars: Optional[int] = None) -> str:
    """Cut a snapshot at a line boundary to ``max_chars`` (default ``browser.snapshot_threshold``);
    the note points at the stored full tree so refs past the cut are not lost."""
    if max_chars is None:
        max_chars = snapshot_threshold()
    if len(snapshot_text) <= max_chars:
        return snapshot_text
    stored_path = _store_full_snapshot(snapshot_text)
    lines = snapshot_text.split("\n")
    result: list[str] = []
    chars = 0
    reserve = min(110 + len(stored_path or ""), max_chars // 2)
    for line in lines:
        if chars + len(line) + 1 > max_chars - reserve:
            break
        result.append(line)
        chars += len(line) + 1
    remaining = len(lines) - len(result)
    if remaining > 0:
        if stored_path:
            result.append(f'\n[... {remaining} more lines truncated — full snapshot: '
                          f'read_file path="{stored_path}" offset={len(result) + 1} limit=200]')
        else:
            result.append(f"\n[... {remaining} more lines truncated, use browser_snapshot for full content]")
    return "\n".join(result)
