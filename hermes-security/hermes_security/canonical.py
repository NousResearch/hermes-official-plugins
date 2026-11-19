"""Canonical JSON, hashing, stable identifiers, and atomic file writes.

Every module that hashes, identifies, or persists canonical data goes through here so
digests and IDs stay byte-for-byte reproducible.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from .fsutil import is_link, set_private


def _reject_non_finite(obj: Any) -> None:
    if isinstance(obj, float) and not math.isfinite(obj):
        raise ValueError("non-finite numbers are not allowed in canonical JSON")
    if isinstance(obj, dict):
        for value in obj.values():
            _reject_non_finite(value)
    elif isinstance(obj, (list, tuple)):
        for value in obj:
            _reject_non_finite(value)


def canonical_json(obj: Any) -> str:
    """Serialize deterministically: sorted keys, compact separators, UTF-8 text, no NaN/Inf."""
    _reject_non_finite(obj)
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def sha256_hex(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def stable_id(prefix: str, *parts: Any) -> str:
    """``<prefix>_<24 hex>`` derived from the canonical JSON of ``parts``."""
    return f"{prefix}_{sha256_hex(canonical_json(list(parts)))[:24]}"


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def secret_fingerprint(value: str) -> str:
    """Short, non-reversible identifier for a secret value; the value itself is never stored."""
    return "sha256:" + sha256_hex(value)[:16]


def atomic_write(path: Path, data: bytes | str, mode: int = 0o600) -> None:
    """Write via a temp file in the same directory, fsync, then rename over ``path``.

    Refuses to replace a symlink so a hostile link cannot redirect the write.
    """
    path = Path(path)
    if is_link(path):
        raise OSError(f"refusing to write through symlink: {path}")
    if isinstance(data, str):
        data = data.encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        set_private(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
