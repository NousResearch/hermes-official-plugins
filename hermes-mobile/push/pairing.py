"""Single relay-settings reader and per-profile pairing store for hermes-mobile.

Precedence everywhere relay settings are read:
  1. Explicit PlatformConfig.extra values (when supplied by the gateway adapter).
  2. Operator-configured environment / Hermes profile secret scope
     (HERMES_MOBILE_PUSH_RELAY_URL, HERMES_MOBILE_PUSH_INSTALL_KEY).
  3. The plugin's own stored pairing in
     <profile_home>/plugin-data/hermes-mobile-push/pairing.json (mode 0600).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

try:
    import fcntl
    msvcrt = None
except ImportError:  # Windows: no fcntl, lock with msvcrt instead.
    fcntl = None
    import msvcrt

logger = logging.getLogger(__name__)

# Legacy data dir name kept so paired installs keep using one push data directory.
LEGACY_DATA_DIR_NAME = "hermes-mobile-push"
PAIRING_FILENAME = "pairing.json"

RELAY_URL_ENV = "HERMES_MOBILE_PUSH_RELAY_URL"
INSTALL_KEY_ENV = "HERMES_MOBILE_PUSH_INSTALL_KEY"

# Plain HTTP only to a relay on this machine (a local test relay): the install key
# rides in every request's Authorization header.
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
RELAY_URL_RULE = "The push relay URL must start with https:// (http:// only for localhost)."

# URL-safe capability token (secrets.token_urlsafe(32) is 43 chars of [A-Za-z0-9_-]).
_INSTALL_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{32,256}\Z")
INSTALL_KEY_RULE = (
    "The install key must be 32 to 256 URL-safe characters (letters, digits, '-' or '_')."
)


class PairingError(Exception):
    """Base error for pairing operations."""


class PairingInvalidError(PairingError):
    """Caller supplied an invalid relay URL or install key (HTTP 400)."""


class PairingConflictError(PairingError):
    """Install is already paired with a different install key (HTTP 409)."""


class PairingStorageError(PairingError):
    """Pairing or preview-key storage could not be read or written (HTTP 503)."""


@dataclass(frozen=True)
class RelaySettings:
    relay_url: str
    raw_relay_url: str
    install_key: str
    url_refused: bool
    source: str | None
    env_has_url: bool
    env_has_key: bool


@contextmanager
def _exclusive_lock(path: str | Path):
    """Hold an exclusive lock on *path* (created if missing) across processes."""
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, "r+b") as lock:
        if fcntl is not None:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield
            return
        while True:
            try:
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)  # type: ignore[union-attr,attr-defined]
                break
            except OSError:
                continue
        try:
            yield
        finally:
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)  # type: ignore[union-attr,attr-defined]


def relay_url_allowed(relay_url) -> bool:
    try:
        parts = urlsplit(str(relay_url or "").strip())
        host = parts.hostname
    except ValueError:
        return False
    return bool(host) and (parts.scheme == "https" or (parts.scheme == "http" and host in _LOOPBACK_HOSTS))


def install_key_valid(install_key) -> bool:
    return isinstance(install_key, str) and bool(_INSTALL_KEY_RE.fullmatch(install_key))


def install_id_for_key(install_key: str) -> str:
    """Lowercase SHA-256 hex digest of *install_key* (matches the relay and privacy.py)."""
    return hashlib.sha256(install_key.encode("utf-8")).hexdigest()


def active_home() -> Path:
    """Active profile's Hermes home, matching push/privacy.py."""
    try:
        from hermes_constants import get_hermes_home
        return Path(get_hermes_home())
    except ImportError:
        return Path(os.getenv("HERMES_HOME", str(Path.home() / ".hermes")))


def pairing_path(home=None) -> Path:
    """Path to the profile's stored pairing file; never creates directories."""
    base = Path(home) if home is not None else active_home()
    return base / "plugin-data" / LEGACY_DATA_DIR_NAME / PAIRING_FILENAME


def _scoped_secret(name: str):
    shared = sys.modules.get("gateway.platforms._shared")
    scope_mod = sys.modules.get("agent.secret_scope")
    if shared is not None and hasattr(shared, "get_scoped_secret"):
        bound = getattr(shared, "_scoped_get_secret", None)
        if scope_mod is None or bound is None or bound is getattr(scope_mod, "get_secret", None):
            return shared.get_scoped_secret(name)
    try:
        from agent.secret_scope import get_secret
    except ImportError:
        from gateway.platforms._shared import get_scoped_secret
        return get_scoped_secret(name)
    try:
        from agent.secret_scope import UnscopedSecretError
    except ImportError:
        UnscopedSecretError = ()
    try:
        return get_secret(name)
    except UnscopedSecretError:
        # The default profile reads unscoped under multiplexing: its own environment.
        return os.getenv(name)


def read_secret(name: str, default: str = "") -> str:
    """Read a credential from Hermes' active profile secret scope, else os.environ."""
    try:
        value = _scoped_secret(name)
    except ImportError:  # outside Hermes, or a Hermes part missing: the process environment
        value = os.getenv(name)
    return str(value) if value else default


def read_stored_pairing(home=None, *, strict: bool = False) -> dict[str, str]:
    """Read the profile's pairing.json without creating directories or logging secrets."""
    try:
        path = pairing_path(home)
    except Exception:
        if strict:
            raise
        # No profile home to read from (Hermes names none here): no stored pairing.
        logger.debug("No Hermes home for the stored push pairing", exc_info=True)
        return {}
    if not path.exists():
        return {}
    if not path.is_file():
        if strict:
            raise PairingStorageError("Stored pairing path is not a regular file.")
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        if strict:
            raise PairingStorageError("Stored pairing file could not be read.") from None
        logger.debug("Could not read stored push pairing file")
        return {}
    if not isinstance(raw, dict):
        if strict:
            raise PairingStorageError("Stored pairing file has an unexpected format.")
        logger.debug("Stored push pairing file has an unexpected format")
        return {}
    result: dict[str, str] = {}
    for field in ("relay_url", "install_key"):
        value = raw.get(field)
        if value is None:
            continue
        if not isinstance(value, str):
            if strict:
                raise PairingStorageError("Stored pairing file has an invalid field.")
            continue
        cleaned = value.strip()
        if cleaned:
            result[field] = cleaned
    return result


def _write_json_atomic(path: Path, data: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(6)}.tmp")
    fd = os.open(tmp_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        try:
            os.fchmod(fd, 0o600)
        except (AttributeError, OSError):
            pass
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(data, output, sort_keys=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def resolve_relay_settings(extra=None, home=None) -> RelaySettings:
    """Resolve relay URL and install key from extra, env/secret scope, then pairing.json."""
    extra = extra or {}
    extra_url = str(extra.get("relay_url") or "").strip()
    extra_key = str(extra.get("install_key") or "").strip()
    # An explicit value wins outright: read the secret scope only for what extra lacks.
    env_url = "" if extra_url else read_secret(RELAY_URL_ENV).strip()
    env_key = "" if extra_key else read_secret(INSTALL_KEY_ENV).strip()

    stored: dict[str, str] = {}
    if not ((extra_url or env_url) and (extra_key or env_key)):
        stored = read_stored_pairing(home)
    stored_url = stored.get("relay_url", "")
    stored_key = stored.get("install_key", "")

    raw_relay_url = (extra_url or env_url or stored_url).rstrip("/")
    install_key = extra_key or env_key or stored_key
    url_refused = bool(raw_relay_url) and not relay_url_allowed(raw_relay_url)
    relay_url = "" if url_refused else raw_relay_url

    if env_url or env_key:
        source: str | None = "environment"
    elif stored_url or stored_key:
        source = "plugin"
    else:
        source = None

    return RelaySettings(
        relay_url=relay_url,
        raw_relay_url=raw_relay_url,
        install_key=install_key,
        url_refused=url_refused,
        source=source,
        env_has_url=bool(env_url),
        env_has_key=bool(env_key),
    )


def relay_settings(extra=None, home=None) -> tuple[str, str]:
    """Return ``(relay_url, install_key)`` with invalid URLs filtered out."""
    resolved = resolve_relay_settings(extra=extra, home=home)
    return resolved.relay_url, resolved.install_key


def pairing_status(home=None) -> dict:
    """Public pairing status for GET /pair and DELETE /pair (never includes secrets)."""
    resolved = resolve_relay_settings(home=home)
    ready = bool(resolved.relay_url and resolved.install_key)
    return {
        "paired": ready,
        "install_id": install_id_for_key(resolved.install_key) if resolved.install_key else None,
        "relay_url": resolved.relay_url or None,
        "source": resolved.source,
        "notifications_ready": ready,
    }


def pair_install(
    relay_url: str,
    install_key: str | None = None,
    *,
    home=None,
    load_preview_key=None,
) -> dict:
    """Pair this profile with the push relay, minting or adopting an install key as needed."""
    requested_url = str(relay_url or "").strip().rstrip("/")
    if not requested_url or not relay_url_allowed(requested_url):
        raise PairingInvalidError(RELAY_URL_RULE)

    provided_key: str | None = None
    if install_key is not None:
        if not isinstance(install_key, str) or not install_key:
            raise PairingInvalidError(INSTALL_KEY_RULE)
        provided_key = install_key

    target_home = Path(home) if home is not None else active_home()
    env_url = read_secret(RELAY_URL_ENV).strip().rstrip("/")
    env_key = read_secret(INSTALL_KEY_ENV).strip()

    def _ensure_preview_key() -> bytes:
        if load_preview_key is None:
            raise PairingStorageError("Preview key loader unavailable.")
        try:
            return load_preview_key(create=True, home=target_home)
        except Exception:
            logger.warning("Preview key unavailable during pairing")
            raise PairingStorageError(
                "Notification preview key could not be created or read; check cryptography and key file permissions."
            ) from None

    if env_url and env_key:
        if provided_key is not None and not hmac.compare_digest(provided_key, env_key):
            raise PairingConflictError("This install is already paired with another install key.")
        preview_bytes = _ensure_preview_key()
        final_url = env_url if relay_url_allowed(env_url) else ""
        return {
            "install_key": env_key,
            "install_id": install_id_for_key(env_key),
            "preview_key": base64.b64encode(preview_bytes).decode("ascii"),
            "relay_url": final_url,
            "source": "environment",
            "notifications_ready": bool(final_url and env_key),
        }

    path = pairing_path(target_home)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _exclusive_lock(str(path) + ".lock"):
        stored = read_stored_pairing(target_home, strict=True)
        current_key = env_key or stored.get("install_key") or ""
        if provided_key is not None:
            if current_key:
                if not hmac.compare_digest(provided_key, current_key):
                    raise PairingConflictError("This install is already paired with another install key.")
                final_key = current_key
            else:
                if not install_key_valid(provided_key):
                    raise PairingInvalidError(INSTALL_KEY_RULE)
                final_key = provided_key
        else:
            final_key = current_key or secrets.token_urlsafe(32)

        preview_bytes = _ensure_preview_key()

        to_write: dict[str, str] = {}
        if not env_url:
            to_write["relay_url"] = requested_url
        if not env_key:
            to_write["install_key"] = final_key
        if to_write:
            try:
                _write_json_atomic(path, to_write)
            except OSError:
                logger.warning("Could not write push pairing file")
                raise PairingStorageError("Could not save pairing state.") from None

    final_url = (env_url if relay_url_allowed(env_url) else "") if env_url else requested_url
    source = "environment" if (env_url or env_key) else "plugin"
    return {
        "install_key": final_key,
        "install_id": install_id_for_key(final_key),
        "preview_key": base64.b64encode(preview_bytes).decode("ascii"),
        "relay_url": final_url,
        "source": source,
        "notifications_ready": bool(final_url and final_key),
    }


def unpair_install(home=None) -> dict:
    """Remove only the plugin's stored pairing.json; never touch env values or the preview key."""
    target_home = Path(home) if home is not None else active_home()
    path = pairing_path(target_home)
    if path.parent.is_dir():
        with _exclusive_lock(str(path) + ".lock"):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                logger.warning("Could not remove push pairing file")
                raise PairingStorageError("Could not remove stored pairing.") from None
    return pairing_status(target_home)
