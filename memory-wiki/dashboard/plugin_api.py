"""Memory Wiki dashboard plugin — backend API routes.

Mounted by the Hermes web server at ``/api/plugins/memory-wiki/`` once the plugin is in
``plugins.enabled``. Cache misses open the profile's ``state.db`` READ-ONLY through the public
``hermes_state.SessionDB`` API, resolve the home from the request context, and never write
memory files. Profile selection depends on the host supplying a scoped request context.

Routes:
    GET /overview                 subjects + daily logs + recent sessions + persistent-memory notes
    GET /subjects?q=&limit=       subject list, optional substring filter
    GET /subjects/{slug}          one subject (404 when unknown)
    GET /days?limit=              daily logs
    GET /days/{date}              one YYYY-MM-DD log (404 when unknown)
"""

from __future__ import annotations

from collections import OrderedDict
from copy import deepcopy
import importlib.util
import json
import logging
import sys
import threading
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException
from starlette.concurrency import run_in_threadpool

_log = logging.getLogger(__name__)
router = APIRouter()

_CORE_MODULE = "hermes_dashboard_plugin_memory_wiki_core"


def _core():
    """The sibling ``memory_wiki_core`` module, loaded by path (the host imports this file via
    ``spec_from_file_location`` with no package, so a relative import is not available)."""
    mod = sys.modules.get(_CORE_MODULE)
    if mod is not None:
        return mod
    path = Path(__file__).with_name("memory_wiki_core.py")
    spec = importlib.util.spec_from_file_location(_CORE_MODULE, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[_CORE_MODULE] = mod
    try:
        spec.loader.exec_module(mod)
    except Exception:
        sys.modules.pop(_CORE_MODULE, None)
        raise
    return mod


def _open_db(home):
    """Read-only handle on the active profile's ``state.db``; ``None`` when it does not exist yet."""
    from hermes_state import SessionDB

    db_path = home / "state.db"
    if not db_path.exists():
        return None
    return SessionDB(db_path=db_path, read_only=True)


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(int(value), high))


def _with_db(home, label: str, fn, *, empty):
    """Run ``fn(db)`` against a read-only DB, mapping a missing DB to *empty* and any failure
    to a 500 with the failure logged (the dashboard shows the label, never the traceback)."""
    try:
        db = _open_db(home)
        if db is None:
            return empty
        try:
            return fn(db)
        finally:
            db.close()
    except HTTPException:
        raise
    except Exception:
        _log.exception("memory-wiki %s failed", label)
        raise HTTPException(status_code=500, detail=f"Memory wiki {label} failed")


def _request_home():
    # Resolve once on the request task, never from process environment in a worker.
    from hermes_constants import get_hermes_home

    return get_hermes_home().absolute()


# Only derived subjects are retained, never connections. The lock also coalesces cold
# requests. Keep a fixed entry/byte bound; a short TTL covers coarse filesystem clocks.
_SUBJECT_CACHE = OrderedDict()
_CACHE_LOCK = threading.Lock()
_CACHE_TTL = 5.0
_CACHE_BYTES = 16 * 1024 * 1024


def _database_stamp(path):
    stamps = []
    for file in (path, Path(str(path) + "-wal")):
        try:
            stat = file.stat()
            # SQLite can create an empty WAL on the first read-only attach.
            stamps.append(
                None if file != path and stat.st_size == 0 else
                (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
            )
        except FileNotFoundError:
            stamps.append(None)
    return tuple(stamps)


def _subject_index(home, session_limit):
    path = home / "state.db"
    key = (str(path), session_limit)
    try:
        with _CACHE_LOCK:
            before = _database_stamp(path)
            now = time.monotonic()
            # Purge expired entries, including other profiles, without retaining handles.
            for old_key, entry in list(_SUBJECT_CACHE.items()):
                if now - entry[1] >= _CACHE_TTL:
                    del _SUBJECT_CACHE[old_key]
            entry = _SUBJECT_CACHE.get(key)
            if before[0] is not None and entry is not None and entry[0] == before:
                _SUBJECT_CACHE.move_to_end(key)
                return deepcopy(entry[2])
            _SUBJECT_CACHE.pop(key, None)
            results = _with_db(
                home, "subjects",
                lambda db: _core().build_subject_index(db, session_limit=session_limit), empty=[],
            )
            # Do not publish work raced by a write, removal or replacement. The next
            # request rebuilds it. An in-flight response may reflect concurrent activity.
            if before[0] is not None and before == _database_stamp(path):
                size = len(json.dumps(results).encode("utf-8"))
                if size <= _CACHE_BYTES:
                    while _SUBJECT_CACHE and (
                        len(_SUBJECT_CACHE) >= 8
                        or sum(item[3] for item in _SUBJECT_CACHE.values()) + size > _CACHE_BYTES
                    ):
                        _SUBJECT_CACHE.popitem(last=False)
                    _SUBJECT_CACHE[key] = (before, time.monotonic(), results, size)
            return deepcopy(results)
    except HTTPException:
        raise
    except Exception:
        _log.exception("memory-wiki subjects failed")
        raise HTTPException(status_code=500, detail="Memory wiki subjects failed")


@router.get("/overview")
async def get_overview(subject_limit: int = 50, day_limit: int = 30):
    home = _request_home()
    core = _core()
    subject_limit = _clamp(subject_limit, 1, 200)
    day_limit = _clamp(day_limit, 1, 120)
    empty = {
        "subjects": [], "daily_logs": [], "recent_sessions": [],
        "notes": await run_in_threadpool(core.build_memory_notes, home / "memories"),
    }
    return await run_in_threadpool(
        _with_db, home, "overview",
        lambda db: core.build_memory_overview(
            db, subject_limit=subject_limit, day_limit=day_limit, memory_dir=home / "memories",
        ),
        empty=empty,
    )


@router.get("/subjects")
async def get_subjects(q: str = "", limit: int = 100):
    home = _request_home()
    core = _core()
    safe_limit = _clamp(limit, 1, 500)
    query = q.strip()

    def search():
        results = _subject_index(home, max(safe_limit * 5, safe_limit, 100))
        return core.select_subjects(results, limit=safe_limit, query=query or None)

    subjects = await run_in_threadpool(search)
    return {"subjects": subjects, "query": query, "limit": safe_limit}


@router.get("/subjects/{slug}")
async def get_subject(slug: str):
    home = _request_home()
    core = _core()
    subject = await run_in_threadpool(
        _with_db, home, "subject", lambda db: core.get_memory_subject(db, slug), empty=None,
    )
    if subject is None:
        raise HTTPException(status_code=404, detail="Memory subject not found")
    return {"subject": subject}


@router.get("/days")
async def get_days(limit: int = 60):
    home = _request_home()
    core = _core()
    safe_limit = _clamp(limit, 1, 365)
    daily_logs = await run_in_threadpool(
        _with_db, home, "days", lambda db: core.build_daily_logs(db, limit_days=safe_limit), empty=[],
    )
    return {"daily_logs": daily_logs, "limit": safe_limit}


@router.get("/days/{date}")
async def get_day(date: str):
    home = _request_home()
    core = _core()
    daily_log = await run_in_threadpool(
        _with_db, home, "day", lambda db: core.get_daily_log(db, date), empty=None,
    )
    if daily_log is None:
        raise HTTPException(status_code=404, detail="Memory day not found")
    return {"daily_log": daily_log}
