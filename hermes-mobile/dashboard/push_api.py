"""Read-only dashboard routes of the push plugin.

`GET /sessions/silent` names a profile's scheduled and webhook runs whose final reply was a
silence marker (`[SILENT]`, `NO_REPLY`, …): the runs the scheduler and this plugin did not
deliver or notify about. Hermes persists no flag for that, so the rule is applied here to each
run's last message, with Hermes' own matcher, so the list cannot disagree with what was
suppressed.
"""
import logging
import os
import re
from pathlib import Path

from fastapi import APIRouter, HTTPException

router = APIRouter()
logger = logging.getLogger(__name__)

# The app lists at most this many sessions per profile, newest first.
MAX_SESSIONS = 1000
# Sources of runs nobody typed into: the scheduler (`cron`) and gateway webhooks.
# Chats are left out; silence there is a different rule.
_AUTOMATED_SOURCES = ("cron", "webhook", "msgraph_webhook")
_CONVERSATION_ROLES = frozenset({"user", "assistant", "tool"})
_TRANSCRIPT_PAGE_SIZE = 4
_PROFILE_ID_FALLBACK_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_RESERVED_PROFILE_NAMES = frozenset({"hermes", "default", "test", "tmp", "root", "sudo"})


def is_silent(text):
    """Hermes' rule for an autonomous run's reply that should not be delivered."""
    try:
        from gateway.response_filters import is_autonomous_silence_response
    except ImportError:
        # Older Hermes releases only recognize the original sentinel, as adapter.py does.
        return isinstance(text, str) and text.strip().upper().startswith("[SILENT]")
    return bool(is_autonomous_silence_response(text))


def _profile_id_pattern():
    try:
        from hermes_constants import PROFILE_ID_RE
        return PROFILE_ID_RE
    except ImportError:
        return _PROFILE_ID_FALLBACK_RE


def _default_root():
    try:
        from hermes_constants import get_default_hermes_root
        return Path(get_default_hermes_root())
    except ImportError:
        home = bound_home()
        return home.parent.parent if home.parent.name == "profiles" else home


def validate_profile(profile):
    if profile == "default":
        return profile
    if (not isinstance(profile, str) or "\n" in profile or "\r" in profile
            or not _profile_id_pattern().fullmatch(profile)):
        raise HTTPException(400, f"{profile!r} is not a valid profile name.")
    if profile in _RESERVED_PROFILE_NAMES:
        raise HTTPException(400, f"Profile name {profile!r} is reserved.")
    target = profile_home(profile)
    try:
        from hermes_constants import named_profile_is_live
        exists = bool(named_profile_is_live(target))
    except ImportError:
        exists = target.is_dir()
    if not exists:
        raise HTTPException(404, f"Profile '{profile}' does not exist.")
    return profile


def profile_home(profile):
    try:
        from persistence_assembly import profile_scope
        return Path(profile_scope(profile).profile_dir)
    except ImportError:
        root = _default_root()
        return root if profile == "default" else root / "profiles" / profile


def bound_home():
    """The Hermes home this request runs in. Hermes binds a dashboard request to the profile it
    selects, also in a secondary profile's plugin host, which never sees `?profile=`."""
    try:
        from hermes_constants import get_hermes_home
        return Path(get_hermes_home())
    except ImportError:
        return Path(os.getenv("HERMES_HOME", str(Path.home() / ".hermes")))


def _bound_profile_name(home):
    try:
        from hermes_constants import named_profile_home, profile_name_for_home
        name = profile_name_for_home(home)
        if name:
            return name
        named = named_profile_home(home)
        if named is not None:
            return named.name
    except ImportError:
        pass
    return home.name if home.parent.name == "profiles" else "default"


def requested_home(profile=None):
    """The home of the profile a request asks about: the bound home, unless `profile` names
    another one, as on Hermes releases that leave every request in the launch profile's home."""
    home = bound_home()
    bound = _bound_profile_name(home)
    if not profile or profile == bound:
        return home
    return profile_home(validate_profile(profile))


def silent_sessions(home, limit=MAX_SESSIONS):
    """Ids of the newest `limit` automated runs in `home` whose last message is a silent reply.

    The last user, assistant or tool row decides: a run that stopped on a tool result, or whose
    final reply says something, is not silent. Opened read-only through the published storage
    interface, so a live store is never created, migrated or locked for writing.
    """
    from persistence.conversation.messages import TranscriptRead
    from persistence.conversation.sessions import SessionWindow
    from persistence_assembly import open_persistence_reader, profile_scope, store_exists

    home = Path(home)
    if not store_exists(profile_scope(home=home)):
        return []
    silent_ids = []
    with open_persistence_reader(home) as reader:
        entries = reader.sessions.search_sessions(
            sources=_AUTOMATED_SOURCES,
            window=SessionWindow(limit=int(limit)),
        )
        for entry in entries:
            session_id = entry.session.session_id
            offset, row = 0, None
            while row is None:
                page = reader.messages.read_transcript(
                    session_id,
                    TranscriptRead(limit=_TRANSCRIPT_PAGE_SIZE, offset=offset, latest=True),
                )
                row = next((msg for msg in reversed(page) if msg.role in _CONVERSATION_ROLES), None)
                if row is None and len(page) < _TRANSCRIPT_PAGE_SIZE:
                    break
                offset += _TRANSCRIPT_PAGE_SIZE
            if row is not None and row.role == "assistant" and is_silent(row.content or ""):
                silent_ids.append(session_id)
    return silent_ids


@router.get("/sessions/silent")
def silent(profile: str | None = None):
    from persistence import PersistenceError

    home = requested_home(profile)
    try:
        return {"silent": silent_sessions(home)}
    except PersistenceError:
        # Store errors can name paths and schema; they go to the log, not to the caller.
        logger.warning("Silent sessions: the session store could not be read", exc_info=True)
        raise HTTPException(503, "The session store could not be read; try again.") from None
