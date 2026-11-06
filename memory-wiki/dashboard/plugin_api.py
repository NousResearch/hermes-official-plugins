"""Memory Wiki dashboard plugin — backend API routes.

Mounted by the Hermes web server at ``/api/plugins/memory-wiki/`` once the plugin is in
``plugins.enabled``. Every route opens the profile's ``state.db`` READ-ONLY through the public
``hermes_state.SessionDB`` API, resolves the profile at call time (the host scopes
``?profile=`` via ``HERMES_HOME``), and never writes memory files.

Routes:
    GET /overview                 subjects + daily logs + recent sessions + persistent-memory notes
    GET /subjects?q=&limit=       subject list, optional substring filter
    GET /subjects/{slug}          one subject (404 when unknown)
    GET /days?limit=              daily logs
    GET /days/{date}              one YYYY-MM-DD log (404 when unknown)
"""

from __future__ import annotations

import importlib.util
import logging
import sys
import threading
from pathlib import Path

from fastapi import APIRouter, HTTPException

_log = logging.getLogger(__name__)
router = APIRouter()

_CORE_MODULE = "hermes_dashboard_plugin_memory_wiki_core"
_CORE_LOAD_LOCK = threading.Lock()


def _core():
    """The sibling ``memory_wiki_core`` module, loaded by path (the host imports this file via
    ``spec_from_file_location`` with no package, so a relative import is not available)."""
    mod = sys.modules.get(_CORE_MODULE)
    if mod is not None:
        return mod
    with _CORE_LOAD_LOCK:  # double-checked: concurrent first requests must not exec twice
        mod = sys.modules.get(_CORE_MODULE)
        if mod is not None:
            return mod
        path = Path(__file__).with_name("memory_wiki_core.py")
        spec = importlib.util.spec_from_file_location(_CORE_MODULE, path)
        if spec is None or spec.loader is None:  # pragma: no cover
            raise ImportError(f"cannot load memory-wiki core module from {path}")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[_CORE_MODULE] = mod
        try:
            spec.loader.exec_module(mod)
        except Exception:
            sys.modules.pop(_CORE_MODULE, None)
            raise
        return mod


def _memory_cache():
    """Process-wide per-session aggregation cache (#49): memoizes each session's
    extraction under a cheap fingerprint so dashboard refreshes stop re-reading the
    whole history. ``MEMORY_WIKI_CACHE=off`` falls back to the uncached path; the
    sidecar lives under ``HERMES_HOME/cache/memory-wiki/`` (profile-scoped)."""
    cache = _core()._cache_module()
    try:
        from hermes_constants import get_hermes_home

        cache_dir = Path(get_hermes_home()) / "cache" / cache.CACHE_DIRNAME
    except (ImportError, OSError, AttributeError):
        _log.debug("memory-wiki cache: no HERMES_HOME, keeping the cache in-process")
        cache_dir = None
    return cache.default_cache(cache_dir)


def _open_db():
    """Read-only handle on the active profile's ``state.db``; ``None`` when it does not exist yet."""
    from hermes_constants import get_hermes_home
    from hermes_state import SessionDB

    db_path = get_hermes_home() / "state.db"
    if not db_path.exists():
        return None
    return SessionDB(db_path=db_path, read_only=True)


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(int(value), high))


async def _with_db(label: str, fn, *, empty):
    """Run ``fn(db)`` against a read-only DB without blocking the event loop: the scan is
    CPU/GIL-bound and large histories would otherwise starve the gateway (memory-wiki #216)."""
    import asyncio

    def _run():
        try:
            db = _open_db()
            if db is None:
                return empty
            try:
                payload = fn(db)
            finally:
                db.close()
            cache = _memory_cache()
            if cache is not None:
                cache.flush()
            return payload
        except HTTPException:
            raise
        except Exception:
            _log.exception("memory-wiki %s failed", label)
            raise HTTPException(status_code=500, detail=f"Memory wiki {label} failed")

    return await asyncio.to_thread(_run)


@router.get("/overview")
async def get_overview(subject_limit: int = 50, day_limit: int = 30):
    core = _core()
    subject_limit = _clamp(subject_limit, 1, 200)
    day_limit = _clamp(day_limit, 1, 120)
    empty = {"subjects": [], "daily_logs": [], "recent_sessions": [], "notes": core.build_memory_notes()}
    return await _with_db(
        "overview",
        lambda db: core.build_memory_overview(
            db, subject_limit=subject_limit, day_limit=day_limit, cache=_memory_cache()
        ),
        empty=empty,
    )


@router.get("/subjects")
async def get_subjects(q: str = "", limit: int = 100):
    core = _core()
    safe_limit = _clamp(limit, 1, 500)
    query = q.strip()
    subjects = await _with_db(
        "subjects",
        lambda db: core.build_memory_subjects(
            db, limit=safe_limit, query=query or None, cache=_memory_cache()
        ),
        empty=[],
    )
    return {"subjects": subjects, "query": query, "limit": safe_limit}


@router.get("/subjects/{slug}")
async def get_subject(slug: str):
    core = _core()
    subject = await _with_db(
        "subject", lambda db: core.get_memory_subject(db, slug, cache=_memory_cache()), empty=None
    )
    if subject is None:
        raise HTTPException(status_code=404, detail="Memory subject not found")
    return {"subject": subject}


@router.get("/days")
async def get_days(limit: int = 60):
    core = _core()
    safe_limit = _clamp(limit, 1, 365)
    daily_logs = await _with_db(
        "days", lambda db: core.build_daily_logs(db, limit_days=safe_limit, cache=_memory_cache()), empty=[]
    )
    return {"daily_logs": daily_logs, "limit": safe_limit}


@router.get("/days/{date}")
async def get_day(date: str):
    core = _core()
    daily_log = await _with_db(
        "day", lambda db: core.get_daily_log(db, date, cache=_memory_cache()), empty=None
    )
    if daily_log is None:
        raise HTTPException(status_code=404, detail="Memory day not found")
    return {"daily_log": daily_log}
