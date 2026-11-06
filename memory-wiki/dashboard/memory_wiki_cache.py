"""Bounded per-session cache for ``memory_wiki_core`` aggregation (#49).

Why: every dashboard request re-read every recent session's messages and
re-runs the deterministic extractors over them — tens of seconds of work and
hundreds of MB of churn per refresh on large histories (#49). That per-session
work depends only on the session's own content, so this module memoizes it as
one JSON-serializable "unit" per session, keyed under a fingerprint of the
cheap sessions-table row. The gateway bumps ``message_count`` /
``tool_call_count`` / ``transcript_revision`` / ``last_active`` on every
message append, rewrite, and rewind, so writing to one session invalidates
exactly that session's unit — no global resets on unrelated writes.

Scope: a process-local dict plus an atomic JSON sidecar, so warm units
survive gateway restarts; one shared lock makes concurrent scans reuse each
other's work, and each ``cache_dir`` gets its own instance, so profiles (and
``HERMES_HOME`` switches) never share units. The sidecar lives under
``HERMES_HOME/cache/memory-wiki/``. The store is bounded by unit count and
serialized size; least-recently-active units are evicted first. Set
``MEMORY_WIKI_CACHE=off`` to fall back to the uncached path.
"""

from __future__ import annotations

from pathlib import Path
import hashlib
import json
import os
import threading
import uuid

CACHE_DIRNAME = "memory-wiki"
CACHE_FILENAME = "session_units-v1.json"
_DEFAULT_MAX_UNITS = 2000
_DEFAULT_MAX_BYTES = 64 * 1024 * 1024

_default_caches: dict[Path | None, SessionUnitCache] = {}
_default_lock = threading.Lock()


def session_fingerprint(session: dict) -> str:
    """Short fingerprint of the cheap sessions-table row backing ``session``.

    Any gateway mutation that can change what the extractors would return
    (title/preview edits, message append, tool-call rewrite, transcript
    rewind) bumps one of these fields, so a fingerprint mismatch is exactly a
    stale unit. Narrow message-level exceptions exist — pure ``active=0``
    deactivations and the recovery-time timestamp repair change what
    ``get_messages`` returns without touching these fields — but they
    self-heal on the session's next counter-bumping write.
    """

    payload = json.dumps(
        (
            str(session.get("id")),
            str(session.get("title")),
            str(session.get("preview")),
            str(session.get("source")),
            float(session.get("started_at") or 0),
            float(session.get("last_active") or 0),
            int(session.get("message_count") or 0),
            int(session.get("tool_call_count") or 0),
            int(session.get("transcript_revision") or 0),
        ),
    )
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


class SessionUnitCache:
    """Memoizes one aggregation unit per session.

    Units are plain dicts produced by ``memory_wiki_core._build_session_unit``;
    the cache only requires each unit to carry ``session.id`` and
    ``fingerprint``. With ``path=None`` the cache is purely in-process.
    """

    def __init__(
        self,
        path: str | Path | None = None,
        *,
        max_units: int = _DEFAULT_MAX_UNITS,
        max_bytes: int = _DEFAULT_MAX_BYTES,
    ):
        self._path = Path(path) if path is not None else None
        self._max_units = max(1, int(max_units))
        # A bound below one serialized unit must not evict everything, so the
        # floor also doubles as the smallest testable bound.
        self._max_bytes = max(1024, int(max_bytes))
        self._lock = threading.Lock()
        self._units: dict[str, dict] = {}
        self._sizes: dict[str, int] = {}  # per-unit serialized size, for the byte bound
        self._bytes = 0
        self._dirty = False
        self._version = 0  # bumped on every store; guards flush bookkeeping
        self._load()

    def lookup(self, session: dict) -> dict | None:
        """Return the cached unit for ``session`` when its fingerprint still matches."""

        with self._lock:
            unit = self._units.get(str(session.get("id")))
        if unit is None or unit.get("fingerprint") != session_fingerprint(session):
            return None
        return unit

    def store(self, unit: dict) -> None:
        """Memoize ``unit``; persistence is deferred until :meth:`flush`."""

        with self._lock:
            key = str(unit["session"]["id"])
            self._bytes -= self._sizes.pop(key, 0)
            self._units[key] = unit
            self._sizes[key] = len(json.dumps(unit).encode("utf-8"))
            self._bytes += self._sizes[key]
            self._dirty = True
            self._version += 1
            self._evict_locked()

    def flush(self) -> None:
        """Persist pending stores with an atomic replace; tolerates read-only homes.

        The payload is serialized under the lock but written outside it, to a
        unique tmp path, so concurrent flushes can never interleave into one
        half-written file. ``_dirty`` survives a failed write (read-only home)
        so the next flush retries, and it is only cleared when no new store
        happened while the bytes were being written.
        """

        if self._path is None:
            return
        with self._lock:
            if not self._dirty:
                return
            payload = self._serialize_locked()
            version = self._version
        tmp = self._path.with_name(f"{self._path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(payload, encoding="utf-8")
            os.replace(tmp, self._path)
        except OSError:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            return  # keep _dirty: the next flush retries
        with self._lock:
            if self._version == version:
                self._dirty = False

    def stats(self) -> dict:
        with self._lock:
            return {
                "units": len(self._units),
                "bytes": self._bytes,
                "dirty": self._dirty,
                "path": str(self._path),
            }

    def _serialize_locked(self) -> str:
        return json.dumps({"units": self._units}, sort_keys=True)

    def _evict_locked(self) -> None:
        """Drop least-recently-active units until the count/size bounds hold.

        Sizes are tracked incrementally — one ``json.dumps`` per stored unit,
        never a re-encode of the whole store — and victims come from a single
        sort, so a cold build over hundreds of sessions stays linear in total
        unit size. The guard keeps at least one unit even when it alone exceeds
        the byte bound: one pathological session can pin an oversized unit, but
        eviction can never empty the store.
        """

        def _last_active(unit: dict) -> float:
            return float(unit.get("session", {}).get("last_active") or 0)

        if len(self._units) <= self._max_units and self._bytes <= self._max_bytes:
            return
        order = sorted(self._units, key=lambda key: _last_active(self._units[key]))
        for key in order:
            over_count = len(self._units) > self._max_units
            over_bytes = self._bytes > self._max_bytes and len(self._units) > 1
            if not over_count and not over_bytes:
                break
            self._drop_locked(key)

    def _drop_locked(self, key: str) -> None:
        self._bytes -= self._sizes.pop(key, 0)
        del self._units[key]
        self._dirty = True

    def _load(self) -> None:
        if self._path is None:
            return
        try:
            raw = self._path.read_text(encoding="utf-8")
            units = json.loads(raw).get("units", {})
        except (OSError, ValueError, AttributeError):
            return  # missing, corrupt, or future-format sidecar: rebuild from scratch
        if not isinstance(units, dict):
            return
        for unit in units.values():
            if isinstance(unit, dict) and isinstance(unit.get("session"), dict):
                key = str(unit["session"].get("id"))
                self._units[key] = unit
                self._sizes[key] = len(json.dumps(unit).encode("utf-8"))
                self._bytes += self._sizes[key]


def default_cache(cache_dir: str | Path | None = None) -> SessionUnitCache | None:
    """Process-wide cache per ``cache_dir``; ``None`` (caching off) wins via env.

    ``MEMORY_WIKI_CACHE=off`` disables caching entirely. Each resolved
    directory owns one stable instance under a shared lock, so a fresh
    ``HERMES_HOME`` (tests, profile switch) never swaps out another profile's
    cache with dirty units pending — every owner keeps flushing its own
    sidecar, and profiles never share units.
    """

    flag = os.environ.get("MEMORY_WIKI_CACHE", "").strip().lower()
    if flag in {"off", "0", "false", "no"}:
        return None

    resolved = Path(cache_dir) if cache_dir is not None else None
    sidecar = resolved / CACHE_FILENAME if resolved is not None else None
    with _default_lock:
        cache = _default_caches.get(resolved)
        if cache is None:
            cache = _default_caches[resolved] = SessionUnitCache(sidecar)
        return cache
