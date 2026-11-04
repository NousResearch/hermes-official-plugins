"""Background jobs: every billed ElevenLabs call runs on a worker thread with a record on disk.

A song can take a minute or more. The tool starts the job, waits for it inline as long as the surface
allows (a few seconds in a chat app, several minutes elsewhere) and returns the finished result; when
the wait runs out it returns the job id instead, and ``elevenlabs_music_job`` reports or waits for the
rest. In a chat app the agent replies "started" and comes back on a background timer, so the chat is
never held for the whole generation.

Records live in ``<plugin data dir>/jobs/<id>.json``, resolved on every call (multiplexed profiles),
written by atomic replace. The worker runs in a copy of the caller's context, so it keeps the calling
profile's home and secret scope. A job whose process went away (a restart mid-generation) is reported
as lost once it is older than any request could take; ElevenLabs may still have billed it.
"""

from __future__ import annotations

import contextvars
import datetime
import json
import os
import re
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import provenance as _provenance
from .elevenlabs_api import NO_RETRY, ToolFailure

JOB_ID_RE = re.compile(r"[0-9a-f]{12}")
# Longer than the longest request deadline (video scoring) plus saving: a running record older than
# this whose worker is not in this process is lost.
LOST_AFTER_SECONDS = 1200
_KEEP_SECONDS = 7 * 24 * 3600
_PROCESS = uuid.uuid4().hex
_live: dict[str, threading.Event] = {}
_live_lock = threading.Lock()


def _jobs_dir() -> Path:
    return _provenance.data_dir() / "jobs"


def _now() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")


def _write(record: dict[str, Any]) -> None:
    directory = _jobs_dir()
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{record['id']}.json"
    temp = directory / f".{record['id']}.{uuid.uuid4().hex[:6]}.tmp"
    temp.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
    os.replace(temp, target)


def read(job_id: str) -> dict[str, Any] | None:
    if not isinstance(job_id, str) or not JOB_ID_RE.fullmatch(job_id):
        return None
    try:
        record = json.loads((_jobs_dir() / f"{job_id}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) and record.get("id") == job_id else None


def _prune() -> None:
    """Drop finished job records older than a week (the songs and their library rows stay)."""
    cutoff = time.time() - _KEEP_SECONDS
    try:
        entries = list(_jobs_dir().glob("*.json"))
    except OSError:
        return
    for path in entries:
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:
            pass


def start(kind: str, label: str, est_seconds: float, work: Callable[[], dict[str, Any]],
          session_id: str = "") -> str:
    """Record a running job and start ``work`` on a worker thread; returns the job id. ``work``
    returns the tool's result dict, or raises ``ToolFailure`` with the model-readable error."""
    _prune()
    job_id = uuid.uuid4().hex[:12]
    record = {"id": job_id, "kind": kind, "label": label[:200], "status": "running", "created_at": _now(),
              "started": time.time(), "est_seconds": round(est_seconds), "process": _PROCESS,
              "session_id": session_id or None, "result": None, "error": None}
    _write(record)
    done = threading.Event()
    with _live_lock:
        _live[job_id] = done

    def run() -> None:
        try:
            result = work()
            record.update(status="done", result=result)
        except ToolFailure as exc:
            record.update(status="failed", error=str(exc))
        except Exception as exc:
            record.update(status="failed", error=(
                f"unexpected {type(exc).__name__} after the request was sent ({str(exc)[:300]}). ElevenLabs may "
                f"already have billed it. {NO_RETRY}"))
        record["finished_at"] = _now()
        try:
            _write(record)
        finally:
            done.set()
            with _live_lock:
                _live.pop(job_id, None)

    context = contextvars.copy_context()
    threading.Thread(target=context.run, args=(run,), name=f"elevenlabs-music-{job_id}", daemon=True).start()
    return job_id


def wait(job_id: str, seconds: float) -> dict[str, Any] | None:
    """The job's record after waiting up to ``seconds`` for it to finish. A job running in this
    process is waited on directly; one from another process (or a restart) is re-read from disk."""
    with _live_lock:
        done = _live.get(job_id)
    if done is not None:
        done.wait(max(0.0, seconds))
        return read(job_id)
    deadline = time.monotonic() + max(0.0, seconds)
    record = read(job_id)
    while record is not None and record.get("status") == "running" and not is_lost(record):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(1.0, remaining))
        record = read(job_id)
    return record


def is_lost(record: dict[str, Any]) -> bool:
    if record.get("status") != "running":
        return False
    with _live_lock:
        if record.get("id") in _live:
            return False
    started = record.get("started")
    return isinstance(started, (int, float)) and time.time() - started > LOST_AFTER_SECONDS


def elapsed(record: dict[str, Any]) -> float:
    started = record.get("started")
    return max(0.0, time.time() - started) if isinstance(started, (int, float)) else 0.0
