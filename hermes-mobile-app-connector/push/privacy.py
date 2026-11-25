"""Private notification previews. No plaintext network fallback, ever.

AES-256-GCM key stays in the Hermes home and paired devices' Keychains.
The relay capability is deliberately NOT an encryption key.
"""
import base64
import hashlib
import hmac
import json
import os
import time
from contextlib import contextmanager
from pathlib import Path

try:
    import fcntl
except ImportError:  # Windows: no fcntl, lock with msvcrt instead.
    fcntl = None
    import msvcrt


@contextmanager
def _exclusive_lock(path):
    """Hold an exclusive lock on *path* (created if missing) across processes."""
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, "r+b") as lock:
        if fcntl is not None:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield
            return
        # msvcrt locks a byte range; LK_LOCK gives up after ten seconds, so
        # keep waiting as long as flock would.
        while True:
            try:
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
                break
            except OSError:
                continue
        try:
            yield
        finally:
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)


def _home():
    try:
        from hermes_constants import get_hermes_home
        return Path(get_hermes_home())
    except ImportError:
        return Path(os.getenv("HERMES_HOME", str(Path.home() / ".hermes")))


# Legacy data dir name kept so paired devices keep decrypting notification previews.
LEGACY_DATA_DIR_NAME = "hermes-mobile-push"


def key_path(home=None):
    home = Path(home) if home is not None else _home()
    root = home.parent.parent if home.parent.name == "profiles" else home
    # Preview keys are installation-wide, including multiplexed profile hooks.
    return root / "plugin-data" / LEGACY_DATA_DIR_NAME / "preview_key"


def load_key(create=False, home=None):
    # Verify the runtime before creating a key that setup would consider ready.
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: F401 -- fails early without cryptography
    path = key_path(home)
    legacy = path.parents[2] / "plugins" / LEGACY_DATA_DIR_NAME / "preview_key"
    if create or (not path.exists() and legacy.is_file()):
        path.parent.mkdir(parents=True, exist_ok=True)
        # Serialize concurrent profile gateways / two-device setup. Never rotate
        # an existing key, including a damaged one, behind another device's back.
        with _exclusive_lock(str(path) + ".lock"):
            if not path.exists():
                key_fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                with os.fdopen(key_fd, "w") as output:
                    output.write(legacy.read_text().strip() if legacy.is_file()
                                 else base64.b64encode(os.urandom(32)).decode("ascii"))
                    output.flush()
                    os.fsync(output.fileno())
    key = base64.b64decode(path.read_bytes().strip(), validate=True)
    if len(key) != 32:
        raise ValueError("Invalid notification preview key")
    return key


# Platform and source are short labels ("telegram", "cron", "gui").
_FIELD_LIMITS = {"platform": 64, "source": 64}


def protect(payload, install_key):
    """Return a small encrypted envelope or a content-free notification.

    Only category, source class, whether the user is active at a computer
    (``desktop_active``) and a stored session id remain visible for relay
    filtering/mutes; a gateway session key never does. Profile, platform, request
    ids and the other routing fields the app reads are encrypted; any other metadata is dropped.
    """
    meta = dict(payload.get("hermes") or {})
    kind = str(payload.get("kind") or "delivery")
    if kind not in {"reply", "approval", "clarify", "input", "sudo", "secret",
                    "turn_error", "credits", "delivery", "system"}:
        kind = "delivery"
    public = {}
    # Only a stored session id is opaque enough to show the relay (for mutes). A
    # gateway session key names the chat it routes to (agent:main:signal:dm:<number>),
    # so it stays encrypted; the relay falls back to the keyed thread token.
    session = str(meta.get("session_id") or "")[:256]
    if session:
        public["session_id"] = session
    source = str(meta.get("source") or meta.get("platform") or "").lower()
    # "gui": the unified runtime's (Hermes 0.22.1+) label for app and Desktop sessions alike.
    if source in {"desktop", "gui", "ios", "android", "mobile", "web", "cli", "tui", "subagent"}:
        public["source"] = source
    if meta.get("is_subagent") is True or source == "subagent" or session.startswith("subagent_"):
        public["is_subagent"] = True
    # Whether the user is active at a computer: the relay holds a reply back
    # by it for a device that chose so. Only a Desktop reply carries it: the relay reads it for no other.
    if kind == "reply" and source == "desktop" and isinstance(meta.get("desktop_active"), bool):
        public["desktop_active"] = meta["desktop_active"]
    safe = {"title": "Hermes Agent", "body": "New activity. Open Hermes Agent to view.",
            "kind": kind, "hermes": public}
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        key = load_key()
        if not meta.get("profile"):
            home = _home()
            meta["profile"] = home.name if home.parent.name == "profiles" else "default"
        # Encrypt only routing fields understood by the app, never arbitrary
        # tool output / URLs / credentials supplied as extra metadata. The
        # platform and source let the app drop the surface's name from the
        # subtitle when the user hides where a message came from.
        meta = {k: str(v)[:_FIELD_LIMITS.get(k, 256)] for k, v in meta.items()
                if k in {"profile", "profile_name", "session_id", "session_key", "kind", "request_id", "question_id",
                         "answer_mode", "platform", "source"}}
        private = {"title": str(payload.get("title") or "Hermes Agent")[:100],
                   "subtitle": str(payload.get("subtitle") or "")[:100],
                   "body": str(payload.get("body") or "New activity.")[:600],
                   "thread_id": str(payload.get("thread_id") or "")[:256],
                   "hermes": meta, "issued_at": int(time.time())}
        def encoded():
            return json.dumps(private, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        data = encoded()
        while len(data) > 2000 and private["body"]:
            private["body"] = private["body"][:-40] + "…" if len(private["body"]) > 40 else ""
            data = encoded()
        if len(data) > 2000:
            raise ValueError("Notification metadata too large")
        install_id = hashlib.sha256(install_key.encode()).hexdigest()
        nonce = os.urandom(12)
        encrypted = AESGCM(key).encrypt(nonce, data, ("hermes-preview-v1:" + install_id).encode())
        public["sealed"] = {"v": 1, "data": base64.b64encode(nonce + encrypted).decode("ascii")}
        # Keyed equality token retains relay replay dedupe without exposing text.
        fingerprint = json.dumps({k: v for k, v in private.items() if k != "issued_at"},
                                 sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
        public["event_id"] = hmac.new(key, b"event:" + fingerprint, hashlib.sha256).hexdigest()
        if private["thread_id"]:
            safe["thread_id"] = hmac.new(key, b"thread:" + private["thread_id"].encode(), hashlib.sha256).hexdigest()
    except Exception:
        # Includes missing cryptography, absent/corrupt keys and encoding errors.
        # Never retry with the input payload, nor log plaintext or key material.
        public["private_preview_unavailable"] = True
    return safe
