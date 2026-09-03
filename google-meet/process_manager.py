"""Subprocess lifecycle manager for the google_meet bot.

Single active meeting at a time, recorded in
``$HERMES_HOME/workspace/meetings/.active.json`` so tool calls across turns
(and ``on_session_end``) can find the bot. The bot is a detached subprocess:
we hold no fds on it and communicate via files only, so the agent loop can't
block on it.

Layout under ``workspace/meetings/``::

    .active.json          {"pid", "meeting_id", "out_dir", "url", "started_at",
                           "session_id", "log_path", "mode"}
    <meeting-id>/status.json     live bot state (written by the bot)
    <meeting-id>/transcript.txt  scraped captions
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

from hermes_constants import get_hermes_home

from plugins.google_meet._jsonfile import read_json, write_json_atomic


def _root() -> Path:
    return Path(get_hermes_home()) / "workspace" / "meetings"


def _read_active() -> Optional[Dict[str, Any]]:
    return read_json(_root() / ".active.json")


def _write_active(data: Dict[str, Any]) -> None:
    write_json_atomic(_root() / ".active.json", data)


def _pid_alive(pid: int) -> bool:
    # Not ``os.kill(pid, 0)``: on Windows that routes through
    # GenerateConsoleCtrlEvent and can kill the target (bpo-14484).
    from gateway.status import _pid_exists
    return bool(pid) and _pid_exists(pid)


def _active_pid() -> int:
    active = _read_active()
    return int(active.get("pid", 0)) if active else 0


def _kill(pid: int, sig) -> None:
    try:
        os.kill(pid, sig)
    except ProcessLookupError:
        pass


_NO_ACTIVE = {"ok": False, "reason": "no active meeting"}


def start(
    url: str,
    *,
    out_dir: Optional[Path] = None,
    headed: bool = False,
    auth_state: Optional[str] = None,
    guest_name: str = "Hermes Agent",
    duration: Optional[str] = None,
    session_id: Optional[str] = None,
    mode: str = "transcribe",
    realtime_model: Optional[str] = None,
    realtime_voice: Optional[str] = None,
    realtime_instructions: Optional[str] = None,
    realtime_api_key: Optional[str] = None) -> Dict[str, Any]:
    """Spawn the meet_bot subprocess for *url*, stopping any running bot first
    (single-active-meeting semantics). Returns a dict summarizing the bot."""
    from plugins.google_meet.meet_bot import _is_safe_meet_url, _meeting_id_from_url

    if not _is_safe_meet_url(url):
        return {"ok": False, "error": "refusing: only https://meet.google.com/ URLs are allowed. got: " + repr(url)}

    if _pid_alive(_active_pid()):
        stop(reason="replaced by new meet_join")

    meeting_id = _meeting_id_from_url(url)
    out = out_dir or (_root() / meeting_id)
    out.mkdir(parents=True, exist_ok=True)

    # Wipe stale files from a previous run of this meeting id so polling isn't confused.
    for name in ("transcript.txt", "status.json"):
        try:
            (out / name).unlink()
        except OSError:
            pass

    env = {**os.environ, "HERMES_MEET_URL": url, "HERMES_MEET_OUT_DIR": str(out),
           "HERMES_MEET_GUEST_NAME": guest_name}
    for value, var in (
        (headed and "1", "HERMES_MEET_HEADED"),
        (auth_state, "HERMES_MEET_AUTH_STATE"),
        (duration, "HERMES_MEET_DURATION"),
        (mode, "HERMES_MEET_MODE"),  # bot defaults to transcribe when unset (v1 behavior)
        (realtime_model, "HERMES_MEET_REALTIME_MODEL"),
        (realtime_voice, "HERMES_MEET_REALTIME_VOICE"),
        (realtime_instructions, "HERMES_MEET_REALTIME_INSTRUCTIONS")):
        if value:
            env[var] = value
    # Resolve the realtime key at SPAWN time, in the parent, where the profile
    # secret scope (a contextvar) is installed. The detached child inherits the
    # environment, not the scope — under a multiplexed gateway an in-child
    # os.environ read could see another profile's key (or nothing).
    if not realtime_api_key:
        from agent.secret_scope import get_secret

        realtime_api_key = get_secret("HERMES_MEET_REALTIME_KEY") or get_secret("OPENAI_API_KEY")
    if realtime_api_key:
        env["HERMES_MEET_REALTIME_KEY"] = realtime_api_key

    log_path = out / "bot.log"
    # Detach: stdin=devnull, stdout/stderr → log file, new session so parent
    # signals don't propagate. The child owns the log fd after Popen.
    with open(log_path, "ab", buffering=0) as log_fh:
        proc = subprocess.Popen(
            [sys.executable, "-m", "plugins.google_meet.meet_bot"],
            stdin=subprocess.DEVNULL, stdout=log_fh, stderr=subprocess.STDOUT,
            env=env, start_new_session=True, close_fds=True)

    record = {
        "pid": proc.pid, "meeting_id": meeting_id, "out_dir": str(out), "url": url,
        "started_at": time.time(), "session_id": session_id, "log_path": str(log_path), "mode": mode,
    }
    _write_active(record)
    return {"ok": True, **record}


def status() -> Dict[str, Any]:
    """Return the current meeting state, or ``{"ok": False, "reason": ...}``."""
    active = _read_active()
    if not active:
        return dict(_NO_ACTIVE)
    pid = int(active.get("pid", 0))
    return {
        "ok": True,
        "alive": _pid_alive(pid),
        "pid": pid,
        "meetingId": active.get("meeting_id"),
        "url": active.get("url"),
        "startedAt": active.get("started_at"),
        "outDir": active.get("out_dir"),
        **(read_json(Path(active.get("out_dir", "")) / "status.json") or {})}


def transcript(last: Optional[int] = None) -> Dict[str, Any]:
    """Read the current transcript file (empty result if the bot hasn't written one yet)."""
    active = _read_active()
    if not active:
        return dict(_NO_ACTIVE)

    tp = Path(active.get("out_dir", "")) / "transcript.txt"
    text = tp.read_text(encoding="utf-8", errors="replace") if tp.is_file() else ""
    all_lines = [ln for ln in text.splitlines() if ln.strip()]
    return {
        "ok": True,
        "meetingId": active.get("meeting_id"),
        "lines": all_lines[-last:] if last else all_lines,
        "total": len(all_lines),
        "path": str(tp)}


def enqueue_say(text: str) -> Dict[str, Any]:
    """Append a ``say`` request to ``<out_dir>/say_queue.jsonl`` for the bot's speaker thread.

    Refused when no meeting is active or the active bot is in transcribe-only mode.
    """
    import uuid

    text = (text or "").strip()
    if not text:
        return {"ok": False, "reason": "text is required"}

    active = _read_active()
    if not active:
        return dict(_NO_ACTIVE)
    if active.get("mode") != "realtime":
        return {"ok": False, "reason": "active meeting is in transcribe mode — pass mode='realtime' "
                                       "to meet_join to enable agent speech"}

    out_dir = Path(active.get("out_dir", ""))
    if not out_dir.is_dir():
        return {"ok": False, "reason": f"out_dir missing: {out_dir}"}

    queue_path = out_dir / "say_queue.jsonl"
    entry = {"id": uuid.uuid4().hex[:12], "text": text}
    with queue_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")
    return {"ok": True, "meetingId": active.get("meeting_id"), "enqueued_id": entry["id"],
            "queue_path": str(queue_path)}


def stop(*, reason: str = "requested") -> Dict[str, Any]:
    """SIGTERM the active bot (SIGKILL after 10s), then clear the active pointer."""
    active = _read_active()
    if not active:
        return dict(_NO_ACTIVE)

    pid = int(active.get("pid", 0))
    out_dir = active.get("out_dir")

    if _pid_alive(pid):
        _kill(pid, signal.SIGTERM)
        for _ in range(20):
            if not _pid_alive(pid):
                break
            time.sleep(0.5)
        if _pid_alive(pid):
            _kill(pid, signal.SIGKILL)  # windows-footgun: ok — POSIX-only plugin (google_meet registers no-op on Windows; see __init__.py)

    try:
        (_root() / ".active.json").unlink()
    except FileNotFoundError:
        pass
    return {
        "ok": True,
        "reason": reason,
        "meetingId": active.get("meeting_id"),
        "transcriptPath": str(Path(out_dir) / "transcript.txt") if out_dir else None}
