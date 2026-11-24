"""Read-only dashboard routes of the push plugin.

`GET /sessions/silent` names a profile's scheduled and webhook runs whose final reply was a
silence marker (`[SILENT]`, `NO_REPLY`, …): the runs the scheduler and this plugin did not
deliver or notify about. Hermes persists no flag for that, so the rule is applied here to each
run's last message, with Hermes' own matcher, so the list cannot disagree with what was
suppressed.
"""
import logging
import os
import sqlite3
from pathlib import Path

from fastapi import APIRouter, HTTPException

router = APIRouter()
logger = logging.getLogger(__name__)

# The app lists at most this many sessions per profile, newest first.
MAX_SESSIONS = 1000
# Sources of runs nobody typed into: the scheduler (`cron`, `cron:<job>`, `scheduled`) and
# gateway webhooks. Chats are left out; silence there is a different rule.
_WEBHOOK_SOURCES = ("webhook", "msgraph_webhook")


def is_silent(text):
    """Hermes' rule for an autonomous run's reply that should not be delivered."""
    try:
        from gateway.response_filters import is_autonomous_silence_response
    except ImportError:
        # Older Hermes releases only recognize the original sentinel, as adapter.py does.
        return isinstance(text, str) and text.strip().upper().startswith("[SILENT]")
    return bool(is_autonomous_silence_response(text))


def validate_profile(profile):
    from hermes_cli.profiles import profile_exists, validate_profile_name
    try:
        validate_profile_name(profile)
    except ValueError as error:
        raise HTTPException(400, str(error)) from None
    if not profile_exists(profile):
        raise HTTPException(404, f"Profile '{profile}' does not exist.")
    return profile


def profile_home(profile):
    from hermes_cli.profiles import get_profile_dir
    return Path(get_profile_dir(profile))


def bound_home():
    """The Hermes home this request runs in. Hermes binds a dashboard request to the profile it
    selects, also in a secondary profile's plugin host, which never sees `?profile=`."""
    try:
        from hermes_constants import get_hermes_home
        return Path(get_hermes_home())
    except ImportError:
        return Path(os.getenv("HERMES_HOME", str(Path.home() / ".hermes")))


def requested_home(profile=None):
    """The home of the profile a request asks about: the bound home, unless `profile` names
    another one, as on Hermes releases that leave every request in the launch profile's home."""
    home = bound_home()
    bound = home.name if home.parent.name == "profiles" else "default"
    if not profile or profile == bound:
        return home
    return profile_home(validate_profile(profile))


def silent_sessions(db_path, limit=MAX_SESSIONS):
    """Ids of the newest `limit` automated runs in `db_path` whose last message is a silent reply.

    The last user, assistant or tool row decides: a run that stopped on a tool result, or whose
    final reply says something, is not silent. Opened read-only, so a live store is never locked
    for writing.
    """
    db_path = Path(db_path)
    if not db_path.exists():
        return []
    connection = sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True, timeout=5)
    try:
        session_columns = _columns(connection, "sessions")
        message_columns = _columns(connection, "messages")
        recency = ("COALESCE(last_activity_at, started_at)"
                   if "last_activity_at" in session_columns else "started_at")
        # Rows rewound out of the transcript no longer count.
        active = "AND active = 1" if "active" in message_columns else ""
        rows = connection.execute(
            f"""
            SELECT s.id, m.role, m.content
            FROM (
                SELECT id, {recency} AS recency FROM sessions
                WHERE lower(source) LIKE 'cron%' OR lower(source) LIKE 'schedule%'
                   OR lower(source) IN ({', '.join('?' for _ in _WEBHOOK_SOURCES)})
                ORDER BY recency DESC
                LIMIT ?
            ) AS s
            JOIN messages AS m ON m.id = (
                SELECT MAX(id) FROM messages
                WHERE session_id = s.id AND role IN ('user', 'assistant', 'tool') {active}
            )
            ORDER BY s.recency DESC
            """,
            (*_WEBHOOK_SOURCES, int(limit)),
        ).fetchall()
    finally:
        connection.close()
    return [session_id for session_id, role, content in rows
            if role == "assistant" and is_silent(content or "")]


def _columns(connection, table):
    return {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}


@router.get("/sessions/silent")
def silent(profile: str | None = None):
    home = requested_home(profile)
    try:
        return {"silent": silent_sessions(home / "state.db")}
    except sqlite3.Error:
        # SQLite's text can name paths and schema; it goes to the log, not to the caller.
        logger.warning("Silent sessions: the session store could not be read", exc_info=True)
        raise HTTPException(503, "The session store could not be read; try again.") from None
